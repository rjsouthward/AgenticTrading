"""
fbrain knowledge-base MCP server.

Register once:
    claude mcp add memory -- python -m blind_spot.fbrain.mcp_server

Tools
-----
put_page     upsert a page; slug auto-derived from title
get_page     retrieve page + outbound link slugs
search       full-text search within a namespace
create_link  directed page→page edge
list_pages   enumerate pages (exhaustive; used by seed-projection ETL)
cypher       raw Cypher passthrough for dev exploration

Neo4j schema
------------
(:Page {slug, namespace, version, title, body, kind, tags[],
        source, created_at, updated_at})
(:Page)-[:LINKS_TO {created_at}]->(:Page)

Constraints / indexes initialised on first connect:
  UNIQUE (slug, namespace)
  FULLTEXT on [title, body]
"""

import os
import re
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from neo4j import AsyncGraphDatabase, AsyncDriver

# ---------------------------------------------------------------------------
# Config from environment (all set in .env)
# ---------------------------------------------------------------------------

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# ---------------------------------------------------------------------------
# Driver lifecycle
# ---------------------------------------------------------------------------

_driver: Optional[AsyncDriver] = None


async def _get_driver() -> AsyncDriver:
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        await _init_schema(_driver)
    return _driver


async def _init_schema(driver: AsyncDriver) -> None:
    async with driver.session(database=NEO4J_DATABASE) as s:
        # Composite uniqueness: same slug can exist in different namespaces
        await s.run(
            "CREATE CONSTRAINT page_slug_ns IF NOT EXISTS "
            "FOR (p:Page) REQUIRE (p.slug, p.namespace) IS NODE KEY"
        )
        # Full-text index for search tool
        await s.run(
            "CREATE FULLTEXT INDEX page_ft IF NOT EXISTS "
            "FOR (n:Page) ON EACH [n.title, n.body]"
        )


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    await _get_driver()
    yield
    if _driver is not None:
        await _driver.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(title: str) -> str:
    """Deterministic slug from a title (max 80 chars)."""
    s = title.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:80]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("fbrain", lifespan=_lifespan)


@mcp.tool(description=(
    "Upsert a page into the fbrain knowledge base. "
    "slug is derived from title. kind must be 'strategy' or 'knowledge'. "
    "Returns {slug, version, namespace}."
))
async def put_page(
    title: str,
    body: str,
    namespace: str = "default",
    kind: str = "knowledge",
    tags: Optional[list[str]] = None,
    source: Optional[str] = None,
) -> dict:
    if kind not in ("strategy", "knowledge"):
        return {"error": f"kind must be 'strategy' or 'knowledge', got '{kind}'"}

    slug = _slugify(title)
    now  = _now()
    tags = tags or []
    d    = await _get_driver()

    async with d.session(database=NEO4J_DATABASE) as s:
        result = await s.run(
            """
            MERGE (p:Page {slug: $slug, namespace: $ns})
            ON CREATE SET
                p.version    = 1,
                p.title      = $title,
                p.body       = $body,
                p.kind       = $kind,
                p.tags       = $tags,
                p.source     = $source,
                p.created_at = $now,
                p.updated_at = $now
            ON MATCH SET
                p.version    = p.version + 1,
                p.title      = $title,
                p.body       = $body,
                p.kind       = $kind,
                p.tags       = $tags,
                p.source     = $source,
                p.updated_at = $now
            RETURN p.slug AS slug, p.version AS version
            """,
            slug=slug, ns=namespace, title=title, body=body,
            kind=kind, tags=tags, source=source, now=now,
        )
        row = await result.single()

    return {"slug": row["slug"], "version": row["version"], "namespace": namespace}


@mcp.tool(description=(
    "Retrieve a page by slug plus the slugs of all pages it links to. "
    "Returns {page: {...}, links: [slug, ...]} or {error: ...}."
))
async def get_page(slug: str, namespace: str = "default") -> dict:
    d = await _get_driver()
    async with d.session(database=NEO4J_DATABASE) as s:
        result = await s.run(
            """
            MATCH (p:Page {slug: $slug, namespace: $ns})
            OPTIONAL MATCH (p)-[:LINKS_TO]->(t:Page)
            RETURN p, collect(t.slug) AS links
            """,
            slug=slug, ns=namespace,
        )
        row = await result.single()

    if row is None:
        return {"error": f"page '{slug}' not found in namespace '{namespace}'"}

    return {"page": dict(row["p"]), "links": row["links"]}


@mcp.tool(description=(
    "Full-text search within a namespace. "
    "Optionally filter by kind ('strategy' or 'knowledge'). "
    "Returns [{score, slug, title, kind}, ...] ranked best-first."
))
async def search(
    query: str,
    namespace: str = "default",
    kind: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    d = await _get_driver()
    async with d.session(database=NEO4J_DATABASE) as s:
        if kind:
            result = await s.run(
                """
                CALL db.index.fulltext.queryNodes("page_ft", $query)
                YIELD node, score
                WHERE node.namespace = $ns AND node.kind = $kind
                RETURN score, node.slug AS slug, node.title AS title,
                       node.kind AS kind
                ORDER BY score DESC LIMIT $limit
                """,
                query=query, ns=namespace, kind=kind, limit=limit,
            )
        else:
            result = await s.run(
                """
                CALL db.index.fulltext.queryNodes("page_ft", $query)
                YIELD node, score
                WHERE node.namespace = $ns
                RETURN score, node.slug AS slug, node.title AS title,
                       node.kind AS kind
                ORDER BY score DESC LIMIT $limit
                """,
                query=query, ns=namespace, limit=limit,
            )
        return [dict(r) async for r in result]


@mcp.tool(description=(
    "Create a directed link from from_slug to to_slug within a namespace. "
    "Both pages must exist. Returns {linked: 'from -> to'} or {error: ...}."
))
async def create_link(
    from_slug: str,
    to_slug: str,
    namespace: str = "default",
) -> dict:
    now = _now()
    d   = await _get_driver()
    async with d.session(database=NEO4J_DATABASE) as s:
        result = await s.run(
            """
            MATCH (a:Page {slug: $from_slug, namespace: $ns})
            MATCH (b:Page {slug: $to_slug,   namespace: $ns})
            MERGE (a)-[r:LINKS_TO]->(b)
            ON CREATE SET r.created_at = $now
            RETURN a.slug AS from_slug, b.slug AS to_slug
            """,
            from_slug=from_slug, to_slug=to_slug, ns=namespace, now=now,
        )
        row = await result.single()

    if row is None:
        return {"error": f"one or both pages not found: '{from_slug}', '{to_slug}'"}
    return {"linked": f"{from_slug} -> {to_slug}"}


@mcp.tool(description=(
    "Enumerate all pages in a namespace, optionally bounded by ISO-8601 "
    "created_at timestamps. Used by the seed-projection ETL for exhaustive "
    "listing — unlike search, this is not ranked and returns every page. "
    "Returns [{slug, title, kind, tags, created_at, updated_at, in_degree}, ...]."
))
async def list_pages(
    namespace: str = "default",
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict]:
    d = await _get_driver()

    # Build WHERE clause dynamically
    conditions = ["p.namespace = $ns"]
    params: dict = {"ns": namespace, "limit": limit, "offset": offset}

    if since:
        conditions.append("p.created_at >= $since")
        params["since"] = since
    if until:
        conditions.append("p.created_at < $until")
        params["until"] = until

    where = " AND ".join(conditions)

    async with d.session(database=NEO4J_DATABASE) as s:
        result = await s.run(
            f"""
            MATCH (p:Page)
            WHERE {where}
            OPTIONAL MATCH (q:Page)-[:LINKS_TO]->(p)
            WITH p, count(q) AS in_degree
            RETURN p.slug       AS slug,
                   p.title      AS title,
                   p.kind       AS kind,
                   p.tags       AS tags,
                   p.created_at AS created_at,
                   p.updated_at AS updated_at,
                   in_degree
            ORDER BY p.created_at
            SKIP $offset LIMIT $limit
            """,
            **params,
        )
        return [dict(r) async for r in result]


@mcp.tool(description=(
    "Run raw Cypher against the fbrain database. "
    "For development and exploration only — not for use in the pipeline. "
    "params is an optional dict of query parameters."
))
async def cypher(
    query: str,
    params: Optional[dict] = None,
) -> list[dict]:
    d = await _get_driver()
    async with d.session(database=NEO4J_DATABASE) as s:
        result = await s.run(query, **(params or {}))
        return [dict(r) async for r in result]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
