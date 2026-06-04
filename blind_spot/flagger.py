"""
Expansion + Flagger for Blind Spot v0.5 — Task 6.

Two stages:

  1. expand_entity(seed_ids, driver, d_e, database)
       BFS from seed canonical IDs over the :Security entity graph
       (:COMPETES_WITH, :VERTICAL, :SUPPLIES, :COMOVES_WITH) to depth d_e.
       Returns U_analyst — the per-analyst structural neighborhood.

  2. flag_blind_spots(candidates, a_final, seeds, driver, k, d_e, database)
       Complement within U_analyst:
           market-lit  ∧  canonical_id in U_analyst  ∧  not in a_final  ∧  coverage=True
       Ranks by confidence (salience, since thesis frontier is always False in v0.5).
       Returns top-k Flag objects with entity_path and a human-readable reason.

Seeds
-----
A seed is a (:Page) node in the fbrain Neo4j graph that the analyst has engaged
with across sessions. pull_seeds_from_fbrain() extracts seeds by:
  1. Pulling (:Page) nodes with updated_at < T0 (point-in-time snapshot).
  2. Scanning page tags for ticker-like strings (1–5 uppercase letters).
  3. Resolving each ticker to a :Security canonical_id via entity_resolution.
  4. Weighting by recency and in-link engagement.

Convention: tag pages with the primary ticker (e.g. ["NVDA", "semiconductors"])
so that they resolve to the correct :Security node.

Explainability hierarchy (BUILD.md §4):
  named SUPPLIES edge (source_span)  >  COMPETES_WITH/VERTICAL  >  COMOVES_WITH
  When a flag has a named-dyad edge, reason leads with it.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from neo4j import Driver

from blind_spot.entity_resolution import resolve_batch

log = logging.getLogger(__name__)

CanonicalId = str

_TICKER_RE = re.compile(r'^[A-Z]{1,5}$')   # rough ticker filter applied to page tags

# Entity graph relationship types (undirected matching in expand Cypher)
_ENTITY_RELS = "COMPETES_WITH|VERTICAL|SUPPLIES|COMOVES_WITH"

# Seed recency decay half-life in days
_SEED_HALFLIFE_DAYS = 180


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class SeedRecord(NamedTuple):
    canonical_id: CanonicalId
    weight: float        # recency × engagement, in (0, 1]; higher = fresher/more-engaged
    slug: str            # fbrain page slug (for debugging)


@dataclass(frozen=True)
class Flag:
    canonical_id: CanonicalId
    salience: float
    on_entity_frontier: bool
    on_thesis_frontier: bool          # always False in v0.5
    entity_path: list[str] | None     # node canonical IDs from a seed to the flagged name
    thesis_path: list[str] | None     # None in v0.5
    reason: str                       # human-readable; leads with named-dyad when available


# ---------------------------------------------------------------------------
# Seed extraction from fbrain
# ---------------------------------------------------------------------------

def pull_seeds_from_fbrain(
    driver: "Driver",
    as_of: date,
    wrds_conn,
    namespace: str = "default",
    database: str | None = None,
) -> list[SeedRecord]:
    """
    Pull analyst seeds from the fbrain :Page graph as of strictly before T0.

    Extracts ticker-like tags from pages, resolves to permno canonical IDs via
    WRDS stocknames, and weights by recency × engagement (in_degree).

    Point-in-time: only pages with updated_at strictly before T0 are included.
    Writing back today's session before snapshotting would be a lookahead leak.

    Parameters
    ----------
    driver     : Neo4j driver (same instance as the :Security graph)
    as_of      : session T0; pages updated on or after this date are excluded
    wrds_conn  : active wrds.Connection for ticker → permno resolution
    namespace  : fbrain namespace (default "default")
    database   : Neo4j database

    Returns
    -------
    list[SeedRecord] sorted by weight descending.
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")
    t0_str = as_of.isoformat()

    # 1. Pull pages updated before T0 with their in_degree
    with driver.session(database=db) as s:
        result = s.run(
            """
            MATCH (p:Page)
            WHERE p.namespace = $ns
              AND p.updated_at < $t0
            OPTIONAL MATCH (q:Page)-[:LINKS_TO]->(p)
            WITH p, count(q) AS in_degree
            RETURN p.slug       AS slug,
                   p.title      AS title,
                   p.tags       AS tags,
                   p.updated_at AS updated_at,
                   in_degree
            ORDER BY p.updated_at DESC
            """,
            ns=namespace, t0=t0_str,
        )
        pages = [dict(row) for row in result]

    if not pages:
        log.info("flagger: no fbrain pages found before %s in namespace '%s'", t0_str, namespace)
        return []

    log.info("flagger: %d fbrain pages retrieved before T0=%s", len(pages), t0_str)

    # 2. Extract ticker-like tags from each page
    ticker_to_slugs: dict[str, list[str]] = {}
    page_meta: dict[str, dict] = {}
    for page in pages:
        slug  = page.get("slug", "")
        tags  = page.get("tags") or []
        updated_at = page.get("updated_at", "")
        in_degree  = page.get("in_degree", 0) or 0

        page_meta[slug] = {"updated_at": updated_at, "in_degree": int(in_degree)}

        for tag in tags:
            if isinstance(tag, str) and _TICKER_RE.match(tag.strip()):
                ticker_to_slugs.setdefault(tag.strip(), []).append(slug)

    if not ticker_to_slugs:
        log.info("flagger: no ticker-like tags found on fbrain pages")
        return []

    log.info("flagger: %d distinct ticker-like tags found in fbrain pages", len(ticker_to_slugs))

    # 3. Resolve tickers to permno canonical IDs via WRDS
    tickers = list(ticker_to_slugs.keys())
    # resolve_batch takes list[tuple[ident, id_type]], as_of, conn
    _resolved_raw = resolve_batch([(t, "ticker") for t in tickers], as_of, wrds_conn)
    resolved: dict[str, CanonicalId | None] = {t: _resolved_raw.get((t, "ticker")) for t in tickers}

    # 4. Build SeedRecord list, one record per (canonical_id, slug) pair
    now_date = as_of
    seeds_by_cid: dict[CanonicalId, SeedRecord] = {}

    for ticker, canonical_id in resolved.items():
        if canonical_id is None:
            continue
        for slug in ticker_to_slugs[ticker]:
            meta = page_meta.get(slug, {})
            updated_str = meta.get("updated_at", "")
            in_degree   = meta.get("in_degree", 0)

            # Recency weight: exp decay with half-life of ~6 months
            try:
                updated_dt = datetime.fromisoformat(updated_str).date()
                days_old = (now_date - updated_dt).days
            except (ValueError, TypeError):
                days_old = 365
            recency = math.exp(-days_old * math.log(2) / _SEED_HALFLIFE_DAYS)

            # Engagement weight: log-scaled in_degree
            engagement = math.log1p(in_degree + 1)

            weight = recency * engagement

            existing = seeds_by_cid.get(canonical_id)
            if existing is None or weight > existing.weight:
                seeds_by_cid[canonical_id] = SeedRecord(
                    canonical_id=canonical_id,
                    weight=weight,
                    slug=slug,
                )

    result_list = sorted(seeds_by_cid.values(), key=lambda s: -s.weight)
    log.info(
        "flagger: %d seeds resolved from %d tickers (%d unresolved)",
        len(result_list), len(tickers), len(tickers) - len(seeds_by_cid),
    )
    return result_list


# ---------------------------------------------------------------------------
# Entity graph expansion
# ---------------------------------------------------------------------------

def expand_entity(
    seed_ids: list[CanonicalId],
    driver: "Driver",
    d_e: int = 2,
    database: str | None = None,
) -> set[CanonicalId]:
    """
    BFS over the :Security entity graph from seed_ids to depth d_e.

    Traverses :COMPETES_WITH, :VERTICAL, :SUPPLIES, :COMOVES_WITH edges in
    both directions (undirected matching) so the analyst's neighbourhood includes
    both upstream suppliers and downstream customers.

    Returns the set of all reachable :Security canonical IDs (includes seeds).

    Parameters
    ----------
    seed_ids : canonical IDs ("permno:XXXXX") of the analyst's seeds
    driver   : Neo4j driver
    d_e      : expansion depth (default 2; 1 = direct peers only)
    database : Neo4j database
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")
    if not seed_ids:
        return set()

    with driver.session(database=db) as s:
        # Variable-length undirected path — Cypher handles cycles automatically
        result = s.run(
            f"""
            MATCH (seed:Security)
            WHERE seed.canonical_id IN $seed_ids
            MATCH (seed)-[:{_ENTITY_RELS}*1..{d_e}]-(reached:Security)
            RETURN DISTINCT reached.canonical_id AS cid
            """,
            seed_ids=list(seed_ids),
        )
        universe = {row["cid"] for row in result if row["cid"]}

    # Seeds are always in their own neighbourhood
    universe.update(seed_ids)
    log.info(
        "flagger: entity expansion (d_e=%d) from %d seeds → %d nodes in U_analyst",
        d_e, len(seed_ids), len(universe),
    )
    return universe


def _find_entity_paths(
    seed_ids: list[CanonicalId],
    target_ids: list[CanonicalId],
    driver: "Driver",
    d_e: int,
    database: str,
) -> dict[CanonicalId, tuple[list[str], list[dict]]]:
    """
    For each target in target_ids, find the shortest path from any seed in seed_ids.

    Returns {target_cid: (node_path, edge_list)} where:
      node_path  : list of canonical IDs along the path (seed → ... → target)
      edge_list  : list of {kind, source_span} dicts for each relationship
    """
    if not seed_ids or not target_ids:
        return {}

    with driver.session(database=database) as s:
        result = s.run(
            f"""
            UNWIND $target_ids AS target_cid
            MATCH (target:Security {{canonical_id: target_cid}})
            MATCH (seed:Security)
            WHERE seed.canonical_id IN $seed_ids
            MATCH path = shortestPath(
                (seed)-[:{_ENTITY_RELS}*1..{d_e + 1}]-(target)
            )
            WITH target_cid, path
            ORDER BY target_cid, length(path) ASC
            WITH target_cid, collect(path)[0] AS shortest
            RETURN
                target_cid,
                [n IN nodes(shortest) | n.canonical_id] AS node_path,
                [r IN relationships(shortest) | {{
                    kind:        type(r),
                    source_span: r.source_span,
                    weight:      r.weight
                }}] AS edges
            """,
            seed_ids=list(seed_ids),
            target_ids=list(target_ids),
        )
        paths: dict[CanonicalId, tuple[list[str], list[dict]]] = {}
        for row in result:
            cid = row["target_cid"]
            if cid:
                paths[cid] = (row["node_path"] or [], row["edges"] or [])

    return paths


def _build_reason(
    node_path: list[str],
    edges: list[dict],
    seed_ids: set[CanonicalId],
) -> str:
    """
    Build a human-readable reason string from the entity path.

    Explainability hierarchy: SUPPLIES (named dyad) > COMPETES_WITH/VERTICAL > COMOVES_WITH.
    """
    if not edges:
        return "structurally connected"

    # Find the most explainable edge type
    edge_kinds = {e.get("kind", "") for e in edges}
    supply_edges = [e for e in edges if e.get("kind") == "SUPPLIES"]

    if supply_edges:
        # Named dyad: use source_span if available
        span = next((e.get("source_span") for e in supply_edges if e.get("source_span")), None)
        if span:
            return f"named customer-supplier relationship: {span}"
        return "customer-supplier relationship in supply chain"

    if "COMPETES_WITH" in edge_kinds or "VERTICAL" in edge_kinds:
        seed_on_path = next((n for n in node_path if n in seed_ids), node_path[0] if node_path else "seed")
        hops = len(node_path) - 1
        if hops == 1:
            return f"direct product-market peer of {seed_on_path}"
        return f"product-market peer within {hops} hops of {seed_on_path}"

    if "COMOVES_WITH" in edge_kinds:
        seed_on_path = next((n for n in node_path if n in seed_ids), node_path[0] if node_path else "seed")
        return f"co-moving with {seed_on_path} (return-based structural link)"

    return "structurally connected in entity graph"


# ---------------------------------------------------------------------------
# Flagger — public entry point
# ---------------------------------------------------------------------------

def flag_blind_spots(
    candidates: list,            # list[Candidate] from candidate_generator
    a_final: set[CanonicalId],
    seeds: list[SeedRecord],
    driver: "Driver",
    k: int = 20,
    d_e: int = 2,
    database: str | None = None,
) -> list[Flag]:
    """
    Surface the complement: market-lit ∧ in U_analyst ∧ absent from a_final.

    Parameters
    ----------
    candidates : ranked Candidate list from generate()
    a_final    : resolved set of canonical IDs the analyst has named today
    seeds      : list[SeedRecord] from pull_seeds_from_fbrain() (or explicit seeds)
    driver     : Neo4j driver
    k          : maximum flags to return
    d_e        : entity expansion depth (default 2)
    database   : Neo4j database

    Returns
    -------
    list[Flag] sorted descending by confidence (salience), at most k entries.
    on_thesis_frontier is always False in v0.5.
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")

    if not seeds:
        log.warning("flagger: no seeds provided — U_analyst is empty, returning no flags")
        return []

    seed_ids = [s.canonical_id for s in seeds]

    # 1. Expand entity graph to get U_analyst
    universe = expand_entity(seed_ids, driver, d_e=d_e, database=db)

    if not universe:
        log.warning("flagger: entity expansion returned empty universe")
        return []

    # 2. Filter candidates: in universe ∧ not in a_final ∧ coverage=True
    absent = [
        c for c in candidates
        if c.canonical_id in universe
        and c.canonical_id not in a_final
        and c.coverage
    ]

    log.info(
        "flagger: %d/%d candidates are absent from a_final within U_analyst (%d in universe)",
        len(absent), len(candidates), len(universe),
    )

    if not absent:
        return []

    # 3. Confidence ranking: salience is the primary signal (thesis always False in v0.5)
    absent_sorted = sorted(absent, key=lambda c: -c.salience)
    top = absent_sorted[:k]

    # 4. Find entity paths from seeds to each flagged candidate
    target_ids = [c.canonical_id for c in top]
    try:
        paths = _find_entity_paths(seed_ids, target_ids, driver, d_e, db)
    except Exception as exc:
        log.warning("flagger: entity path query failed (%s) — flags returned without paths", exc)
        paths = {}

    seed_id_set = set(seed_ids)

    # 5. Build Flag objects
    flags: list[Flag] = []
    for c in top:
        path_info = paths.get(c.canonical_id)
        if path_info:
            node_path, edges = path_info
        else:
            node_path, edges = None, []

        reason = _build_reason(node_path or [], edges, seed_id_set)
        on_entity = node_path is not None and len(node_path) > 0

        flags.append(Flag(
            canonical_id      = c.canonical_id,
            salience          = c.salience,
            on_entity_frontier = on_entity,
            on_thesis_frontier = False,   # v0.5: always False
            entity_path       = node_path,
            thesis_path       = None,
            reason            = reason,
        ))

    log.info("flagger: %d flags generated", len(flags))
    return flags
