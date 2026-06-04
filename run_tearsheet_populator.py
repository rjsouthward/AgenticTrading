"""
Populate tearsheets for a FlagSession.

For each FlagItem in the session:
  1. Resolve all canonical_ids (flags + entity-path nodes) to tickers via WRDS CRSP
  2. Fetch company metadata + prev-close price from Polygon /v3/reference/tickers
     and /v2/aggs/ticker/{ticker}/prev
  3. Fetch latest 5 news headlines from Polygon /v2/reference/news
  4. persist_tearsheet to Neo4j for each flag
  5. Store a full ticker_lookup map on the FlagSession node for path-label rendering
  6. Re-render (and optionally open) the preview HTML

Usage:
    source .venv/bin/activate
    python run_tearsheet_populator.py --session_id 2026-06-01-ryan [--open]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from blind_spot.flag_stream.persistence import load_flags, persist_tearsheet
from blind_spot.market_data import build_permno_ticker_map

POLYGON_KEY = os.environ["POLYGON_API_KEY"]
_POLYGON_BASE = "https://api.polygon.io"


# ---------------------------------------------------------------------------
# Polygon helpers
# ---------------------------------------------------------------------------

def _pg_get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    url = _POLYGON_BASE + path
    p = {"apiKey": POLYGON_KEY, **(params or {})}
    for attempt in range(retries):
        try:
            r = requests.get(url, params=p, timeout=15)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            log.warning("Polygon %s → HTTP %d", path, r.status_code)
            return None
        except requests.RequestException as exc:
            log.warning("Polygon %s → %s (attempt %d)", path, exc, attempt + 1)
            time.sleep(1)
    return None


def fetch_ticker_details(ticker: str) -> dict | None:
    """Return dict with name, description, market_cap, sic_description."""
    data = _pg_get(f"/v3/reference/tickers/{ticker}")
    if data and data.get("status") == "OK":
        r = data.get("results", {})
        return {
            "name":        r.get("name", ticker),
            "sector":      r.get("sic_description") or r.get("market", ""),
            "market_cap":  r.get("market_cap") or 0.0,
            "description": r.get("description", ""),
        }
    return None


def fetch_prev_close(ticker: str) -> dict | None:
    """Return dict with price, change_abs, change_pct from prev trading session."""
    data = _pg_get(f"/v2/aggs/ticker/{ticker}/prev", {"adjusted": "true"})
    if data and data.get("resultsCount", 0) > 0:
        bar = data["results"][0]
        close = bar.get("c", 0.0)
        prev_open = bar.get("o", close)
        chg_abs = close - prev_open
        chg_pct = (chg_abs / prev_open * 100) if prev_open else 0.0
        return {"price": close, "change_abs": chg_abs, "change_pct": chg_pct}
    return None


def fetch_news(ticker: str, limit: int = 5) -> list[dict]:
    """Return list of headline dicts for the ticker."""
    data = _pg_get(
        "/v2/reference/news",
        {"ticker": ticker, "limit": limit, "sort": "published_utc", "order": "desc"},
    )
    if not data or not data.get("results"):
        return []
    out = []
    for i, art in enumerate(data["results"], start=1):
        out.append({
            "rank":         i,
            "published_at": art.get("published_utc", ""),
            "title":        art.get("title", ""),
            "source":       art.get("publisher", {}).get("name", ""),
            "url":          art.get("article_url", ""),
            "summary":      art.get("description") or None,
        })
    return out


# ---------------------------------------------------------------------------
# CRSP permno → ticker lookup
# ---------------------------------------------------------------------------

def resolve_permnos_to_tickers(
    permnos: list[int],
    cached_map_path: Path = Path("data/permno_ticker_map.json"),
) -> dict[int, str]:
    """
    Resolve permno ints to ticker strings.

    First checks the cached JSON map.  Any permnos not found are fetched live
    from WRDS CRSP dsenames.  The cache is updated with the new entries.
    """
    # Load cache
    cached: dict[int, str] = {}
    if cached_map_path.exists():
        raw = json.loads(cached_map_path.read_text())
        cached = {int(k): v for k, v in raw.items()}

    missing = [p for p in permnos if p not in cached]
    if not missing:
        return {p: cached[p] for p in permnos if p in cached}

    log.info("Resolving %d permnos from WRDS CRSP…", len(missing))
    try:
        import wrds
        conn = wrds.Connection(wrds_username=os.getenv("WRDS_USERNAME", "rjsouthward"))
        crsp_max_row = conn.raw_sql("SELECT MAX(date) AS d FROM crsp_a_stock.dsf")
        crsp_max = crsp_max_row["d"].iloc[0]
        from datetime import date as date_
        as_of = crsp_max if isinstance(crsp_max, date_) else date_.fromisoformat(str(crsp_max)[:10])
        new_map = build_permno_ticker_map(missing, as_of, conn)
        conn.close()
        cached.update(new_map)
        cached_map_path.parent.mkdir(parents=True, exist_ok=True)
        cached_map_path.write_text(json.dumps({str(k): v for k, v in cached.items()}))
        log.info("Cache updated with %d new entries", len(new_map))
    except Exception as exc:
        log.warning("WRDS lookup failed (%s); using cache only for missing permnos", exc)

    return {p: cached[p] for p in permnos if p in cached}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def populate(session_id: str, open_preview: bool = False) -> None:
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        payload = load_flags(driver, session_id, database=os.environ["NEO4J_DATABASE"])
    finally:
        driver.close()

    if payload is None:
        raise SystemExit(f"No FlagSession with session_id '{session_id}'")

    flags = payload["flags"]

    # Collect ALL permno IDs: flagged nodes + every entity-path node
    all_cids: set[str] = set()
    for f in flags:
        all_cids.add(f["canonical_id"])
        for node in f.get("entity_path") or []:
            all_cids.add(node)

    all_permnos = []
    for cid in all_cids:
        if cid.startswith("permno:"):
            try:
                all_permnos.append(int(cid.split(":")[1]))
            except ValueError:
                pass

    log.info("Resolving %d unique permnos to tickers…", len(all_permnos))
    permno_to_ticker = resolve_permnos_to_tickers(all_permnos)

    def cid_to_ticker(cid: str) -> str | None:
        if cid.startswith("permno:"):
            try:
                return permno_to_ticker.get(int(cid.split(":")[1]))
            except ValueError:
                pass
        return None

    # Build lookup map for ALL path nodes (stored on FlagSession)
    ticker_lookup: dict[str, str] = {}
    for cid in all_cids:
        t = cid_to_ticker(cid)
        if t:
            ticker_lookup[cid] = t

    # Persist tearsheets and store ticker_lookup on session
    driver = GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        for f in flags:
            ticker = cid_to_ticker(f["canonical_id"])
            if not ticker:
                log.warning("No ticker for %s — skipping tearsheet", f["canonical_id"])
                continue

            log.info("Building tearsheet for %s (%s)…", f["canonical_id"], ticker)

            details  = fetch_ticker_details(ticker) or {}
            price_d  = fetch_prev_close(ticker) or {}
            headlines = fetch_news(ticker)

            now = datetime.now(timezone.utc).isoformat()
            overview = {
                "ticker":      ticker,
                "name":        details.get("name", ticker),
                "sector":      details.get("sector", ""),
                "market_cap":  details.get("market_cap", 0.0),
                "price":       price_d.get("price", 0.0),
                "change_abs":  price_d.get("change_abs", 0.0),
                "change_pct":  price_d.get("change_pct", 0.0),
                "summary":     details.get("description", ""),
                "fetched_at":  now,
            }
            persist_tearsheet(
                driver,
                session_id=session_id,
                canonical_id=f["canonical_id"],
                overview=overview,
                headlines=headlines,
                database=os.environ["NEO4J_DATABASE"],
            )
            log.info("  %s: %d headlines", ticker, len(headlines))

        # Store ticker_lookup on FlagSession node
        with driver.session(database=os.environ["NEO4J_DATABASE"]) as s:
            s.run(
                "MATCH (sess:FlagSession {session_id: $sid}) "
                "SET sess.ticker_lookup = $tl",
                sid=session_id,
                tl=json.dumps(ticker_lookup),
            )
        log.info("Stored ticker_lookup (%d entries) on FlagSession", len(ticker_lookup))
    finally:
        driver.close()

    # Re-render preview
    from blind_spot.flag_stream.preview import render
    html = render(session_id)
    out = Path("/tmp/blind_spot_preview.html")
    out.write_text(html, encoding="utf-8")
    log.info("Preview written to %s", out)
    if open_preview:
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--session_id", required=True)
    p.add_argument("--open", action="store_true", dest="open_preview")
    args = p.parse_args()
    populate(args.session_id, open_preview=args.open_preview)
