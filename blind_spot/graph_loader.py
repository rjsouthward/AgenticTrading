"""
Graph loader for Blind Spot v0.5 — Task 2.

Loads Hoberg-Phillips TNIC and VTNIC flat files into Blind Spot's Neo4j graph
as :COMPETES_WITH and :VERTICAL edges on :Security nodes keyed by permno.

File format (Hoberg-Phillips standard):
  Tab- or space-delimited, with columns gvkey1, gvkey2, score (or vscore), and
  optionally year. Download from https://hobergphillips.tuck.dartmouth.edu/

Point-in-time rule: TNIC vintage Y is treated as publicly available on July 1
of year Y+1 (conservative; the actual release is typically in mid-summer). Use
tnic_vintage_for(as_of) to select the right vintage before loading.

Usage:
    from neo4j import GraphDatabase
    from blind_spot.graph_loader import ensure_schema, tnic_vintage_for, load_tnic, load_vtnic

    driver = GraphDatabase.driver(uri, auth=(user, password))
    ensure_schema(driver)

    vintage = tnic_vintage_for(as_of=date.today())
    stats = load_tnic("data/tnic3_data.txt", vintage, as_of, wrds_conn, driver)
    stats = load_vtnic("data/vtnic_data.txt", vintage, as_of, wrds_conn, driver)
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from blind_spot.entity_resolution import resolve_gvkeys_batch

if TYPE_CHECKING:
    import wrds
    from neo4j import Driver

log = logging.getLogger(__name__)

# Conservative availability lag: vintage Y is public after this month/day of Y+1.
# Derived from the typical Hoberg-Phillips release cadence (~July each year).
_VINTAGE_AVAIL_MONTH = 7
_VINTAGE_AVAIL_DAY = 1


# ---------------------------------------------------------------------------
# Neo4j connection helper
# ---------------------------------------------------------------------------

def get_driver(
    uri: str | None = None,
    user: str | None = None,
    password: str | None = None,
) -> "Driver":
    """
    Create a synchronous Neo4j driver from args or env vars.

    Env vars: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
    Caller is responsible for calling driver.close() when done.
    """
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        uri      or os.getenv("NEO4J_URI",      "bolt://localhost:7687"),
        auth=(
            user     or os.getenv("NEO4J_USER",     "neo4j"),
            password or os.getenv("NEO4J_PASSWORD", ""),
        ),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema(driver: "Driver", database: str | None = None) -> None:
    """
    Create the :Security unique constraint and supporting index.

    Safe to call repeatedly (uses IF NOT EXISTS). Run once before any load.
    The :Security subgraph is kept separate from the fbrain :Page DAG by
    using a distinct node label and relationship types.
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")
    with driver.session(database=db) as s:
        s.run(
            "CREATE CONSTRAINT security_id IF NOT EXISTS "
            "FOR (n:Security) REQUIRE n.canonical_id IS UNIQUE"
        )
        s.run(
            "CREATE INDEX security_id_idx IF NOT EXISTS "
            "FOR (n:Security) ON (n.canonical_id)"
        )
    log.info("graph_loader: schema ensured (database=%s)", db)


# ---------------------------------------------------------------------------
# Point-in-time vintage selection
# ---------------------------------------------------------------------------

def tnic_vintage_for(as_of: date) -> int:
    """
    Return the latest TNIC/VTNIC vintage year whose data was public before as_of.

    Conservative rule: vintage Y is available on July 1 of year Y+1.
    If as_of is before that availability date, step back one more year.

    Examples (with July 1 availability):
        as_of=2021-01-15 → vintage 2019  (2020 vintage not out until 2021-07-01)
        as_of=2021-08-01 → vintage 2020  (2020 vintage available 2021-07-01)
        as_of=2022-06-30 → vintage 2020  (2021 vintage not out until 2022-07-01)
    """
    candidate = as_of.year - 1
    avail = date(as_of.year, _VINTAGE_AVAIL_MONTH, _VINTAGE_AVAIL_DAY)
    if as_of < avail:
        candidate -= 1
    return candidate


# ---------------------------------------------------------------------------
# Internal loader
# ---------------------------------------------------------------------------

def _load_edges(
    filepath: str | Path,
    vintage_year: int,
    as_of: date,
    wrds_conn: "wrds.Connection",
    driver: "Driver",
    rel_type: str,
    provenance: str,
    score_col: str,
    min_score: float,
    batch_size: int,
    database: str | None,
) -> dict[str, int]:
    """
    Core loader shared by load_tnic and load_vtnic.

    Steps:
      1. Read the flat file; normalise column names.
      2. Filter to the requested vintage year (if a 'year' column is present).
      3. Apply min_score threshold.
      4. Batch-resolve all gvkeys → permno in one WRDS round-trip.
      5. Drop edges where either endpoint is unresolved.
      6. Canonicalize direction: lower permno as src to avoid duplicate edges.
      7. Deduplicate.
      8. Write to Neo4j in batches via UNWIND MERGE.

    Returns {'edges_written', 'edges_skipped_score', 'edges_skipped_unresolved',
             'gvkeys_unresolved', 'nodes_merged'}.
    """
    fp = Path(filepath)
    if not fp.exists():
        raise FileNotFoundError(
            f"graph_loader: file not found: {fp}\n"
            "Download TNIC/VTNIC data from https://hobergphillips.tuck.dartmouth.edu/"
        )

    df = pd.read_csv(fp, sep=r"\s+", dtype=str)
    df.columns = [c.lower() for c in df.columns]

    # Rename score column to a canonical name
    if score_col in df.columns and score_col != "weight":
        df = df.rename(columns={score_col: "weight"})
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")

    # Zero-pad gvkeys to 6 characters to match CCM storage format ("001011" not "1011")
    df["gvkey1"] = df["gvkey1"].str.zfill(6)
    df["gvkey2"] = df["gvkey2"].str.zfill(6)

    # Filter to the requested vintage year if the file has a year column
    if "year" in df.columns:
        df = df[df["year"].astype(str) == str(vintage_year)]
        if df.empty:
            # Show available years from the full file for a helpful error
            all_years = sorted(pd.read_csv(fp, sep=r"\s+", usecols=["year"])["year"].dropna().unique().astype(int))
            raise ValueError(
                f"graph_loader: no rows for vintage_year={vintage_year} in {fp}. "
                f"Available years: {all_years}"
            )

    n_before_score = len(df)
    df = df[df["weight"] >= min_score].dropna(subset=["weight"])
    n_score_skipped = n_before_score - len(df)

    if df.empty:
        log.warning(
            "graph_loader: no edges remain after min_score=%.4f filter for %s vintage=%d",
            min_score, provenance, vintage_year,
        )
        return {
            "edges_written": 0,
            "edges_skipped_score": n_score_skipped,
            "edges_skipped_unresolved": 0,
            "gvkeys_unresolved": 0,
            "nodes_merged": 0,
        }

    # Resolve all gvkeys in one WRDS round-trip
    all_gvkeys = list(set(df["gvkey1"].tolist() + df["gvkey2"].tolist()))
    log.info(
        "graph_loader: resolving %d unique gvkeys for %s vintage=%d as_of=%s",
        len(all_gvkeys), provenance, vintage_year, as_of,
    )
    gvkey_map = resolve_gvkeys_batch(all_gvkeys, as_of, wrds_conn)
    n_unresolved_gvkeys = sum(1 for v in gvkey_map.values() if v is None)

    df["src"] = df["gvkey1"].map(gvkey_map)
    df["dst"] = df["gvkey2"].map(gvkey_map)
    resolved = df.dropna(subset=["src", "dst"])
    n_unresolved_edges = len(df) - len(resolved)

    if resolved.empty:
        log.warning("graph_loader: no edges remain after gvkey resolution")
        return {
            "edges_written": 0,
            "edges_skipped_score": n_score_skipped,
            "edges_skipped_unresolved": n_unresolved_edges,
            "gvkeys_unresolved": n_unresolved_gvkeys,
            "nodes_merged": 0,
        }

    # Canonicalize direction: extract permno int for ordering, then reformat
    def _permno_int(cid: str) -> int:
        return int(cid.split(":")[1])

    mask = resolved["src"].apply(_permno_int) > resolved["dst"].apply(_permno_int)
    resolved = resolved.copy()
    resolved.loc[mask, ["src", "dst"]] = resolved.loc[mask, ["dst", "src"]].values

    # Deduplicate (keep max weight for duplicate pairs)
    resolved = (
        resolved.groupby(["src", "dst"], as_index=False)["weight"]
        .max()
    )

    as_of_str = as_of.isoformat()
    rows = [
        {
            "src": r["src"],
            "dst": r["dst"],
            "weight": float(r["weight"]),
            "vintage": vintage_year,
            "as_of": as_of_str,
            "provenance": provenance,
        }
        for _, r in resolved.iterrows()
    ]

    db = database or os.getenv("NEO4J_DATABASE", "neo4j")
    cypher = f"""
        UNWIND $batch AS row
        MERGE (a:Security {{canonical_id: row.src}})
        MERGE (b:Security {{canonical_id: row.dst}})
        MERGE (a)-[r:{rel_type} {{src: row.src, dst: row.dst, provenance: row.provenance, vintage: row.vintage}}]->(b)
        SET r.weight = row.weight,
            r.as_of  = row.as_of
    """

    n_written = 0
    node_ids: set[str] = set()
    with driver.session(database=db) as s:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            s.run(cypher, batch=batch)
            n_written += len(batch)
            for r in batch:
                node_ids.add(r["src"])
                node_ids.add(r["dst"])
            log.debug(
                "graph_loader: wrote batch %d/%d (%s vintage=%d)",
                i // batch_size + 1, -(-len(rows) // batch_size),
                provenance, vintage_year,
            )

    log.info(
        "graph_loader: %s vintage=%d — %d edges written, %d score-filtered, "
        "%d unresolved-endpoint, %d gvkeys unresolved, %d nodes touched",
        provenance, vintage_year, n_written, n_score_skipped,
        n_unresolved_edges, n_unresolved_gvkeys, len(node_ids),
    )
    return {
        "edges_written": n_written,
        "edges_skipped_score": n_score_skipped,
        "edges_skipped_unresolved": n_unresolved_edges,
        "gvkeys_unresolved": n_unresolved_gvkeys,
        "nodes_merged": len(node_ids),
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def load_tnic(
    filepath: str | Path,
    vintage_year: int,
    as_of: date,
    wrds_conn: "wrds.Connection",
    driver: "Driver",
    min_score: float = 0.01,
    batch_size: int = 500,
    database: str | None = None,
) -> dict[str, int]:
    """
    Load a Hoberg-Phillips TNIC3 flat file into Neo4j as :COMPETES_WITH edges.

    Parameters
    ----------
    filepath     : path to the TNIC data file (space/tab-delimited)
    vintage_year : the year of the TNIC vintage to load (use tnic_vintage_for)
    as_of        : session T0; gvkey→permno resolution is point-in-time at this date
    wrds_conn    : active wrds.Connection (caller owns lifecycle)
    driver       : Neo4j driver (caller owns lifecycle)
    min_score    : edges below this TNIC cosine similarity are dropped (default 0.01)
    batch_size   : rows per Neo4j UNWIND batch
    database     : Neo4j database name (falls back to NEO4J_DATABASE env var)

    Returns stats dict: edges_written, edges_skipped_score,
    edges_skipped_unresolved, gvkeys_unresolved, nodes_merged.
    """
    return _load_edges(
        filepath=filepath,
        vintage_year=vintage_year,
        as_of=as_of,
        wrds_conn=wrds_conn,
        driver=driver,
        rel_type="COMPETES_WITH",
        provenance="tnic",
        score_col="score",
        min_score=min_score,
        batch_size=batch_size,
        database=database,
    )


def load_vtnic(
    filepath: str | Path,
    vintage_year: int,
    as_of: date,
    wrds_conn: "wrds.Connection",
    driver: "Driver",
    min_score: float = 0.01,
    batch_size: int = 500,
    database: str | None = None,
) -> dict[str, int]:
    """
    Load a Hoberg-Phillips VTNIC flat file into Neo4j as :VERTICAL edges.

    Same contract as load_tnic. The VTNIC score column is named 'vscore'
    in the Hoberg-Phillips files; this function handles the rename automatically.
    """
    return _load_edges(
        filepath=filepath,
        vintage_year=vintage_year,
        as_of=as_of,
        wrds_conn=wrds_conn,
        driver=driver,
        rel_type="VERTICAL",
        provenance="vtnic",
        score_col="vscore",
        min_score=min_score,
        batch_size=batch_size,
        database=database,
    )
