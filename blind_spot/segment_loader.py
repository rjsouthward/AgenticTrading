"""
Segment loader for Blind Spot v0.5 — Task 3.

Loads Compustat Historical Segments (customer disclosures) into Neo4j as
directed :SUPPLIES edges. Each edge is stamped to the 10-K filing date
(not datadate) per the point-in-time-discipline skill.

Point-in-time invariant
-----------------------
Compustat records are stamped to datadate (fiscal year end), but the
disclosure is not public until the 10-K filing date — weeks to months
later. This loader:
  1. Looks up the filing date for each supplier's 10-K via wrdssec_all.
  2. Stamps each edge as_of = filing_date.
  3. Falls back to datadate + 90 days if no filing date is found, and
     sets lag_estimated = True on those edges.

Usage
-----
    from blind_spot.segment_loader import load_segments
    stats = load_segments(
        fiscal_year=2022,
        as_of=date(2023, 6, 30),   # edges filed before this date are loaded
        wrds_conn=conn,
        driver=driver,
    )
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from blind_spot.entity_resolution import resolve_gvkeys_batch

if TYPE_CHECKING:
    import wrds
    from neo4j import Driver

log = logging.getLogger(__name__)

# WRDS table constants — confirmed against live schema 2026-06-02
_SEG_CUSTOMER = "comp_segments_hist_daily.seg_customer"
_SEG_LINK     = "wrdsapps_link_supplychain.seglink"
_FUNDA        = "comp.funda"
_COMPANY      = "comp.company"
_SEC_FORMS    = "wrdssec_all.wrds_forms"

# Conservative filing lag when SEC date is unavailable
_FILING_LAG_DAYS = 90

# 10-K form types to match
_TENK_FORMS = ("10-K", "10-K405", "10-KSB", "10-K/A", "20-F")


# ---------------------------------------------------------------------------
# Internal WRDS pulls
# ---------------------------------------------------------------------------

def _pull_customer_segments(fiscal_year: int, conn: "wrds.Connection") -> pd.DataFrame:
    """
    Pull all named-company customer segment disclosures for a fiscal year.

    Returns columns: gvkey, cid, sid, cnms, salecs, datadate.
    """
    sql = f"""
        SELECT gvkey, cid, sid, cnms, salecs, datadate
        FROM {_SEG_CUSTOMER}
        WHERE ctype = 'COMPANY'
          AND EXTRACT(YEAR FROM datadate) = %(year)s
          AND salecs > 0
          AND cnms IS NOT NULL
        ORDER BY gvkey, datadate
    """
    df = conn.raw_sql(sql, params={"year": fiscal_year})
    log.info("segment_loader: pulled %d customer segment rows for FY%d", len(df), fiscal_year)
    return df


def _pull_company_sales(gvkeys: list[str], fiscal_year: int, conn: "wrds.Connection") -> dict[str, float]:
    """
    Pull total annual sales from Compustat funda to use as revenue denominator.

    Returns {gvkey: sale}.
    """
    if not gvkeys:
        return {}
    sql = f"""
        SELECT gvkey, sale
        FROM {_FUNDA}
        WHERE gvkey = ANY(%(gvkeys)s)
          AND EXTRACT(YEAR FROM datadate) = %(year)s
          AND indfmt = 'INDL' AND datafmt = 'STD'
          AND popsrc = 'D' AND consol = 'C'
          AND sale > 0
        ORDER BY gvkey, datadate DESC
    """
    df = conn.raw_sql(sql, params={"gvkeys": gvkeys, "year": fiscal_year})
    # Keep first (most recent) row per gvkey
    return {row["gvkey"]: float(row["sale"]) for _, row in df.drop_duplicates("gvkey").iterrows()}


def _pull_customer_gvkeys(
    gvkeys: list[str], fiscal_year: int, conn: "wrds.Connection"
) -> dict[tuple[str, int, int], str]:
    """
    Look up resolved customer gvkeys from the WRDS supply chain link table.

    Returns {(supplier_gvkey, cid, sid): customer_gvkey}.
    """
    if not gvkeys:
        return {}
    sql = f"""
        SELECT DISTINCT ON (gvkey, cid, sid)
               gvkey, cid, sid, cgvkey
        FROM {_SEG_LINK}
        WHERE gvkey = ANY(%(gvkeys)s)
          AND cgvkey IS NOT NULL
          AND EXTRACT(YEAR FROM srcdate) = %(year)s
        ORDER BY gvkey, cid, sid, srcdate DESC
    """
    df = conn.raw_sql(sql, params={"gvkeys": gvkeys, "year": fiscal_year})
    result = {}
    for _, row in df.iterrows():
        key = (str(row["gvkey"]).zfill(6), int(row["cid"]), int(row["sid"]))
        result[key] = str(row["cgvkey"]).zfill(6)
    log.info(
        "segment_loader: seglink returned %d resolved customer gvkeys for FY%d",
        len(result), fiscal_year,
    )
    return result


def _pull_filing_dates(
    gvkeys: list[str], fiscal_year: int, conn: "wrds.Connection"
) -> dict[str, date]:
    """
    Look up each supplier's 10-K filing date for the given fiscal year.

    Returns {gvkey: filing_date}. Missing entries use the lag-estimated fallback.
    """
    if not gvkeys:
        return {}
    sql = f"""
        SELECT c.gvkey, MIN(f.fdate) AS fdate
        FROM {_COMPANY} c
        JOIN {_SEC_FORMS} f ON f.cik = c.cik
        WHERE c.gvkey = ANY(%(gvkeys)s)
          AND f.form = ANY(%(forms)s)
          AND EXTRACT(YEAR FROM f.rdate) = %(year)s
        GROUP BY c.gvkey
    """
    df = conn.raw_sql(sql, params={"gvkeys": gvkeys, "forms": list(_TENK_FORMS), "year": fiscal_year})
    result = {}
    for _, row in df.iterrows():
        if row["fdate"] is not None and str(row["fdate"]) not in ("NaT", "None", ""):
            fdate = pd.to_datetime(row["fdate"]).date()
            result[str(row["gvkey"]).zfill(6)] = fdate
    log.info(
        "segment_loader: found %d/%d filing dates for FY%d",
        len(result), len(gvkeys), fiscal_year,
    )
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_segments(
    fiscal_year: int,
    as_of: date,
    wrds_conn: "wrds.Connection",
    driver: "Driver",
    min_revenue_frac: float = 0.0,
    batch_size: int = 200,
    database: str | None = None,
) -> dict[str, int]:
    """
    Load Compustat customer segment disclosures into Neo4j as :SUPPLIES edges.

    Parameters
    ----------
    fiscal_year      : the Compustat fiscal year to load (datadate year)
    as_of            : session T0; only edges with filing_date < as_of are loaded
    wrds_conn        : active wrds.Connection
    driver           : Neo4j driver
    min_revenue_frac : drop edges below this revenue fraction (default 0.0 = keep all)
    batch_size       : rows per Neo4j UNWIND batch
    database         : Neo4j database (falls back to NEO4J_DATABASE env var)

    Returns stats dict with edges_written, edges_skipped_frac,
    edges_skipped_postdated, edges_skipped_unresolved,
    lag_estimated_count, nodes_merged.
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")

    # 1. Pull customer segments
    segs = _pull_customer_segments(fiscal_year, wrds_conn)
    if segs.empty:
        log.warning("segment_loader: no customer segments found for FY%d", fiscal_year)
        return _empty_stats()

    supplier_gvkeys = segs["gvkey"].str.zfill(6).unique().tolist()

    # 2. Total company sales for revenue fraction
    company_sales = _pull_company_sales(supplier_gvkeys, fiscal_year, wrds_conn)

    # 3. Resolved customer gvkeys from seglink
    segs["gvkey"] = segs["gvkey"].str.zfill(6)
    customer_gvkey_map = _pull_customer_gvkeys(supplier_gvkeys, fiscal_year, wrds_conn)

    # 4. Filing dates (point-in-time stamp)
    filing_dates = _pull_filing_dates(supplier_gvkeys, fiscal_year, wrds_conn)

    # 5. Resolve all gvkeys (supplier + customer) to permno in one WRDS round-trip
    all_customer_gvkeys = list(set(customer_gvkey_map.values()))
    supplier_permno = resolve_gvkeys_batch(supplier_gvkeys, as_of, wrds_conn)
    customer_permno = resolve_gvkeys_batch(all_customer_gvkeys, as_of, wrds_conn) if all_customer_gvkeys else {}

    # 6. Build edge records
    edges = []
    n_skipped_frac = 0
    n_skipped_postdated = 0
    n_skipped_unresolved_supplier = 0
    n_lag_estimated = 0
    n_null_customer = 0

    for _, row in segs.iterrows():
        sup_gvkey = str(row["gvkey"]).zfill(6)
        cid = int(row["cid"])
        sid = int(row["sid"])

        # Revenue fraction
        total_sale = company_sales.get(sup_gvkey)
        if total_sale and total_sale > 0:
            weight = float(row["salecs"]) / total_sale
        else:
            weight = None  # can't normalise; keep edge but flag it

        if weight is not None and weight < min_revenue_frac:
            n_skipped_frac += 1
            continue

        # Filing date (point-in-time stamp)
        filing_date = filing_dates.get(sup_gvkey)
        lag_estimated = False
        if filing_date is None:
            # Fallback: fiscal year end + 90 days
            filing_date = pd.to_datetime(row["datadate"]).date() + timedelta(days=_FILING_LAG_DAYS)
            lag_estimated = True
            n_lag_estimated += 1

        # Point-in-time guard: skip if not yet public at as_of
        if filing_date >= as_of:
            n_skipped_postdated += 1
            continue

        # Resolve supplier
        src_id = supplier_permno.get(sup_gvkey)
        if src_id is None:
            n_skipped_unresolved_supplier += 1
            continue

        # Resolve customer
        cust_gvkey = customer_gvkey_map.get((sup_gvkey, cid, sid))
        dst_id = customer_permno.get(cust_gvkey) if cust_gvkey else None

        if dst_id is None:
            # Unresolved customer: log the named disclosure but skip the graph edge
            log.debug(
                "segment_loader: unresolved customer cnms=%r supplier=%s FY%d",
                row["cnms"], sup_gvkey, fiscal_year,
            )
            n_null_customer += 1
            continue

        source_span = (
            f"{row['cnms']} "
            f"({weight:.1%} of FY{fiscal_year} revenues)"
            if weight is not None
            else f"{row['cnms']} (FY{fiscal_year}, revenue fraction unavailable)"
        )

        edges.append({
            "src":           src_id,
            "dst":           dst_id,
            "weight":        weight,
            "as_of":         filing_date.isoformat(),
            "datadate":      str(row["datadate"])[:10],
            "source_span":   source_span,
            "provenance":    "compustat_segment",
            "fiscal_year":   fiscal_year,
            "lag_estimated": lag_estimated,
        })

    if not edges:
        log.warning("segment_loader: no valid edges to write for FY%d", fiscal_year)
        return {
            "edges_written": 0,
            "edges_skipped_frac": n_skipped_frac,
            "edges_skipped_postdated": n_skipped_postdated,
            "edges_skipped_unresolved_supplier": n_skipped_unresolved_supplier,
            "edges_null_customer": n_null_customer,
            "lag_estimated_count": n_lag_estimated,
            "nodes_merged": 0,
        }

    # 7. Write to Neo4j in batches
    cypher = """
        UNWIND $batch AS row
        MERGE (a:Security {canonical_id: row.src})
        MERGE (b:Security {canonical_id: row.dst})
        MERGE (a)-[r:SUPPLIES {src: row.src, dst: row.dst,
                               fiscal_year: row.fiscal_year,
                               provenance: row.provenance}]->(b)
        SET r.weight        = row.weight,
            r.as_of         = row.as_of,
            r.datadate      = row.datadate,
            r.source_span   = row.source_span,
            r.lag_estimated = row.lag_estimated
    """

    node_ids: set[str] = set()
    n_written = 0
    with driver.session(database=db) as s:
        for i in range(0, len(edges), batch_size):
            batch = edges[i : i + batch_size]
            s.run(cypher, batch=batch)
            n_written += len(batch)
            for e in batch:
                node_ids.add(e["src"])
                node_ids.add(e["dst"])

    log.info(
        "segment_loader: FY%d — %d edges written, %d null-customer, "
        "%d postdated, %d frac-filtered, %d lag-estimated, %d nodes touched",
        fiscal_year, n_written, n_null_customer,
        n_skipped_postdated, n_skipped_frac, n_lag_estimated, len(node_ids),
    )
    return {
        "edges_written": n_written,
        "edges_skipped_frac": n_skipped_frac,
        "edges_skipped_postdated": n_skipped_postdated,
        "edges_skipped_unresolved_supplier": n_skipped_unresolved_supplier,
        "edges_null_customer": n_null_customer,
        "lag_estimated_count": n_lag_estimated,
        "nodes_merged": len(node_ids),
    }


def _empty_stats() -> dict[str, int]:
    return {
        "edges_written": 0,
        "edges_skipped_frac": 0,
        "edges_skipped_postdated": 0,
        "edges_skipped_unresolved_supplier": 0,
        "edges_null_customer": 0,
        "lag_estimated_count": 0,
        "nodes_merged": 0,
    }
