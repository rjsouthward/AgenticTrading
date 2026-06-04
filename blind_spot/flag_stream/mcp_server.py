"""
Blind Spot flag-stream MCP server (stdio).

Register once:
    claude mcp add blind-spot-flags -- python -m blind_spot.flag_stream.mcp_server

Tools
-----
run_session      One-shot: runs the Blind Spot pipeline end-to-end, persists the
                 flagged tickers as a FlagSession in Neo4j, and returns them.
get_flags        Read back a previously-run session by session_id.
list_sessions    Enumerate all FlagSession ids currently persisted.
render_artifact  Return the React (TSX) artifact source for a session, with the
                 flag payload inlined — a static snapshot. Re-call after another
                 run to refresh.
delete_session   Drop a persisted FlagSession and its flag items.

Backing store: the same Neo4j the rest of the pipeline already writes to
(NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE from .env).
WRDS auth is read non-interactively from ~/.pgpass (see persistent memory).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from mcp.server.fastmcp import FastMCP
from neo4j import Driver, GraphDatabase

from blind_spot.flag_stream.persistence import (
    delete_session as delete_session_db,
    list_sessions as list_sessions_db,
    load_flags,
    persist_flags,
)

log = logging.getLogger(__name__)

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")
WRDS_USERNAME  = os.getenv("WRDS_USERNAME",  "rjsouthward")
WRDS_HOST      = "wrds-pgdata.wharton.upenn.edu"

_ARTIFACT_PATH = Path(__file__).parent / "artifact.tsx"
_DATA_TOKEN    = "__FLAG_DATA__"

_driver: Optional[Driver] = None


def _get_driver() -> Driver:
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
    return _driver


def _wrds_password() -> str:
    """Non-interactive WRDS password lookup from ~/.pgpass (see memory)."""
    pgpass = Path("~/.pgpass").expanduser()
    if not pgpass.exists():
        raise FileNotFoundError(
            f"~/.pgpass not found — required for non-interactive WRDS auth"
        )
    for line in pgpass.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 5:
            continue
        host, _port, _db, user, pw = parts
        if user in ("*", WRDS_USERNAME) and host in ("*", WRDS_HOST):
            return pw
    raise ValueError(f"No pgpass entry for WRDS user '{WRDS_USERNAME}'")


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    _get_driver()
    try:
        yield
    finally:
        global _driver
        if _driver is not None:
            _driver.close()
            _driver = None


mcp = FastMCP("blind-spot-flags", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Pipeline runner — sync, executed in a thread so the event loop stays free
# ---------------------------------------------------------------------------

def _run_pipeline_sync(
    session_id: str,
    as_of_d: date,
    a_final: set[str],
    k: int,
    d_e: int,
    universe: list[str] | None,
    window_days: int,
) -> int:
    # Deferred imports — wrds/pandas are optional deps (pyproject `pipeline` extra)
    import wrds
    from blind_spot.candidate_generator import generate
    from blind_spot.flagger import flag_blind_spots, pull_seeds_from_fbrain

    driver = _get_driver()
    conn = wrds.Connection(
        wrds_username=WRDS_USERNAME, wrds_password=_wrds_password()
    )
    try:
        if universe is None:
            with driver.session(database=NEO4J_DATABASE) as s:
                universe = [
                    row["cid"]
                    for row in s.run(
                        "MATCH (n:Security) RETURN n.canonical_id AS cid"
                    )
                    if row["cid"]
                ]
            log.info("run_session: pulled %d :Security ids from graph", len(universe))

        candidates = generate(
            universe=universe,
            as_of=as_of_d,
            wrds_conn=conn,
            window_days=window_days,
        )
        seeds = pull_seeds_from_fbrain(
            driver, as_of=as_of_d, wrds_conn=conn, database=NEO4J_DATABASE
        )
        flags = flag_blind_spots(
            candidates=candidates,
            a_final=a_final,
            seeds=seeds,
            driver=driver,
            k=k,
            d_e=d_e,
            database=NEO4J_DATABASE,
        )
    finally:
        conn.close()

    return persist_flags(
        driver,
        session_id=session_id,
        as_of=as_of_d,
        flags=flags,
        k=k,
        d_e=d_e,
        database=NEO4J_DATABASE,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Run the Blind Spot v0.5 pipeline end-to-end for the given session: "
    "Lane B candidate generation → seed pull from fbrain → entity expansion → "
    "complement-against-a_final flagging. Persists the result as a FlagSession "
    "in Neo4j (overwriting any prior session with the same session_id) and "
    "returns the flag list. Blocking — runs until the pipeline completes. "
    "Args: session_id (unique key), as_of (ISO date), a_final (canonical_ids "
    "the analyst already named), k (max flags, default 20), d_e (expansion "
    "depth, default 2), universe (optional list of canonical_ids; defaults to "
    "all :Security nodes), window_days (IV history for Lane B, default 252). "
    "Returns the same shape as get_flags."
))
async def run_session(
    session_id: str,
    as_of: str,
    a_final: list[str],
    k: int = 20,
    d_e: int = 2,
    universe: Optional[list[str]] = None,
    window_days: int = 252,
) -> dict:
    try:
        as_of_d = date.fromisoformat(as_of)
    except ValueError as e:
        return {"error": f"invalid as_of '{as_of}': {e}"}

    try:
        n = await asyncio.to_thread(
            _run_pipeline_sync,
            session_id, as_of_d, set(a_final), k, d_e, universe, window_days,
        )
    except Exception as e:
        log.exception("run_session failed for %s", session_id)
        return {"error": f"pipeline failed: {type(e).__name__}: {e}"}

    payload = load_flags(_get_driver(), session_id, database=NEO4J_DATABASE)
    if payload is None:
        return {"session_id": session_id, "n_flags": n, "flags": []}
    return payload


@mcp.tool(description=(
    "Read back a previously-run flag session by session_id. Returns "
    "{session_id, created_at, as_of, k, d_e, n_flags, flags: [...]} or "
    "{error} if no session matches."
))
def get_flags(session_id: str) -> dict:
    payload = load_flags(_get_driver(), session_id, database=NEO4J_DATABASE)
    if payload is None:
        return {"error": f"no FlagSession with session_id '{session_id}'"}
    return payload


@mcp.tool(description=(
    "List all FlagSession ids currently persisted, newest first. "
    "Returns [{session_id, created_at, as_of, n_flags}, ...]."
))
def list_sessions() -> list[dict]:
    return list_sessions_db(_get_driver(), database=NEO4J_DATABASE)


@mcp.tool(description=(
    "Return a self-contained React (TSX) artifact source rendering the given "
    "session's flagged tickers as a sortable, filterable table with expandable "
    "entity-path rows. The flag data is inlined as JSON, so the artifact is a "
    "static snapshot — re-call this tool after another run_session to refresh. "
    "Returns {session_id, n_flags, tsx} or {error}."
))
def render_artifact(session_id: str) -> dict:
    payload = load_flags(_get_driver(), session_id, database=NEO4J_DATABASE)
    if payload is None:
        return {"error": f"no FlagSession with session_id '{session_id}'"}

    template = _ARTIFACT_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(payload, indent=2, default=str)
    inlined = template.replace(_DATA_TOKEN, data_json, 1)

    return {
        "session_id": session_id,
        "n_flags":    payload.get("n_flags", 0),
        "tsx":        inlined,
    }


@mcp.tool(description=(
    "Delete a persisted FlagSession and its flag items. "
    "Returns {deleted: true/false}."
))
def delete_session(session_id: str) -> dict:
    return {"deleted": delete_session_db(
        _get_driver(), session_id, database=NEO4J_DATABASE
    )}


if __name__ == "__main__":
    mcp.run()
