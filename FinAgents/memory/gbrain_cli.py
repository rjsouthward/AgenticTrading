#!/usr/bin/env python3
"""
gbrain CLI — basic commands to test the Finance GBrain MemoryAgent (Layer 1).

A thin terminal wrapper over the same UnifiedDatabaseManager methods the MCP tools
use (put_page / get_page / search_pages / link_pages), so you can poke at the brain
without an MCP client.

Usage:
  python gbrain_cli.py put "<title>" "<body>" [--ns NS] [--kind KIND] [--tags a,b] [--slug S]
  python gbrain_cli.py get <slug> [--ns NS]
  python gbrain_cli.py search "<query>" [--ns NS] [--limit N] [--kind KIND]
  python gbrain_cli.py link <from_slug> <to_slug> [--ns NS]
  python gbrain_cli.py list [--ns NS]
  python gbrain_cli.py clear [--ns NS]

NS defaults to "default". Config (DigitalOcean + Neo4j) is read from the project .env.
"""
import argparse
import asyncio
import json
import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # allow sibling imports when run from anywhere

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, "..", "..", ".env"))

# Keep CLI output clean — silence the chatty INFO logs / Neo4j notifications.
logging.basicConfig(level=logging.WARNING)
logging.getLogger("neo4j").setLevel(logging.ERROR)

from unified_database_manager import create_database_manager


def _cfg():
    return {
        "uri": os.getenv("NEO4J_URI"),
        "username": os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER"),
        "password": os.getenv("NEO4J_PASSWORD"),
        "database": os.getenv("NEO4J_DATABASE"),
    }


async def run(args) -> int:
    mgr = create_database_manager(_cfg())
    if not await mgr.connect():
        print("ERROR: could not connect to Neo4j (check .env)")
        return 1
    try:
        if args.cmd == "put":
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]
            r = await mgr.put_page(title=args.title, body=args.body, namespace=args.ns,
                                   slug=(args.slug or None), tags=tags, kind=args.kind)
            print(json.dumps(r, indent=2))

        elif args.cmd == "get":
            r = await mgr.get_page(args.slug, namespace=args.ns)
            print(json.dumps(r, indent=2) if r else f"(not found: {args.ns}::{args.slug})")

        elif args.cmd == "search":
            qv = mgr.indexer.create_text_embedding(args.query)
            res = await mgr.search_pages(qv, namespace=args.ns, limit=args.limit,
                                         kind=(args.kind or None))
            for x in res:
                print(f"  {x['similarity_score']:.3f}  {x['slug']:<28} [{x['kind']}]  {x['title']}")
            if not res:
                print(f"(no results in namespace '{args.ns}')")

        elif args.cmd == "link":
            print(json.dumps(await mgr.link_pages(args.from_slug, args.to_slug, namespace=args.ns)))

        elif args.cmd == "list":
            with mgr.driver.session(database=mgr.database) as s:
                rows = list(s.run(
                    "MATCH (p:Page {namespace:$ns}) "
                    "RETURN p.slug AS slug, p.kind AS kind, p.title AS title, p.version AS v "
                    "ORDER BY p.updated_at DESC", {"ns": args.ns}))
            for r in rows:
                print(f"  {r['slug']:<28} v{r['v']}  [{r['kind']}]  {r['title']}")
            if not rows:
                print(f"(no pages in namespace '{args.ns}')")

        elif args.cmd == "clear":
            with mgr.driver.session(database=mgr.database) as s:
                n = s.run("MATCH (p:Page {namespace:$ns}) DETACH DELETE p RETURN count(*) AS c",
                          {"ns": args.ns}).single()["c"]
            print(f"deleted {n} pages from namespace '{args.ns}'")

        elif args.cmd == "ingest":
            with open(args.path, encoding="utf-8") as fh:
                body = fh.read()
            # title = first markdown H1, else the filename without extension
            title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")),
                         os.path.splitext(os.path.basename(args.path))[0])
            r = await mgr.put_page(title=title, body=body, namespace=args.ns,
                                   slug=(args.slug or None), kind=args.kind,
                                   source=os.path.abspath(args.path))
            print(json.dumps(r, indent=2))
    finally:
        await mgr.close()
    return 0


def main():
    ap = argparse.ArgumentParser(prog="gbrain", description="Finance GBrain CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("put", help="create/update a page")
    p.add_argument("title"); p.add_argument("body")
    p.add_argument("--ns", default="default"); p.add_argument("--kind", default="knowledge")
    p.add_argument("--tags", default=""); p.add_argument("--slug", default="")

    g = sub.add_parser("get", help="fetch a page by slug")
    g.add_argument("slug"); g.add_argument("--ns", default="default")

    s = sub.add_parser("search", help="semantic search within a namespace")
    s.add_argument("query"); s.add_argument("--ns", default="default")
    s.add_argument("--limit", type=int, default=5); s.add_argument("--kind", default="")

    l = sub.add_parser("link", help="link two pages (from -> to)")
    l.add_argument("from_slug"); l.add_argument("to_slug"); l.add_argument("--ns", default="default")

    ls = sub.add_parser("list", help="list pages in a namespace")
    ls.add_argument("--ns", default="default")

    cl = sub.add_parser("clear", help="delete all pages in a namespace")
    cl.add_argument("--ns", default="default")

    ig = sub.add_parser("ingest", help="add a markdown/text file as a page")
    ig.add_argument("path")
    ig.add_argument("--ns", default="default")
    ig.add_argument("--kind", default="knowledge")
    ig.add_argument("--slug", default="")

    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
