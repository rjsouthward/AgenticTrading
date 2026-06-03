"""
Co-movement loader for Blind Spot v0.5 — Task 4.

Computes trailing partial-correlation edges between :Security nodes already in
the graph and writes them as undirected :COMOVES_WITH relationships.

Why partial correlation: raw return correlations just rediscover sector betas
(everything moves with the market). Stripping the VW market factor via OLS
residuals isolates firm-pair-specific co-movement — the only layer that can
catch an emerging structural connection before it shows up in TNIC or filings.

Point-in-time rule
------------------
The return window must end strictly before session T0. We use
    window_end   = as_of - 1 calendar day
    window_start = window_end - (window_days * 1.55) calendar days  [generous]
and then keep only the last `window_days` trading-day returns that appear in
the pulled data. `min_obs` enforces a data-sufficiency floor per stock.

Usage
-----
    from blind_spot.comovement_loader import load_comovement
    stats = load_comovement(
        as_of=date(2023, 12, 31),
        wrds_conn=conn,
        driver=driver,
    )
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import wrds
    from neo4j import Driver

log = logging.getLogger(__name__)

_DSF = "crsp_a_stock.dsf"
_DSI = "crsp_a_stock.dsi"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_graph_permnos(driver: "Driver", database: str) -> list[int]:
    """Return permno integers for all :Security nodes in the graph."""
    with driver.session(database=database) as s:
        result = s.run("MATCH (n:Security) RETURN n.canonical_id AS cid")
        permnos = []
        for row in result:
            cid = row["cid"]
            if cid and cid.startswith("permno:"):
                permnos.append(int(cid.split(":")[1]))
    log.info("comovement_loader: %d Security nodes in graph", len(permnos))
    return permnos


def _pull_returns(
    permnos: list[int],
    window_start: date,
    window_end: date,
    conn: "wrds.Connection",
) -> pd.DataFrame:
    """
    Pull daily returns from CRSP for the given permnos and date range.

    Returns a DataFrame indexed by date, columns = permno (int).
    Negative CRSP return codes (e.g. -99) are treated as missing.
    """
    sql = f"""
        SELECT permno, date, ret
        FROM {_DSF}
        WHERE permno = ANY(%(permnos)s)
          AND date >= %(start)s
          AND date <= %(end)s
          AND ret IS NOT NULL
          AND ret > -1        -- exclude CRSP missing-data codes (<= -1)
        ORDER BY date
    """
    df = conn.raw_sql(sql, params={"permnos": permnos, "start": window_start, "end": window_end})
    if df.empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    df["ret"] = pd.to_numeric(df["ret"], errors="coerce")
    wide = df.pivot_table(index="date", columns="permno", values="ret", aggfunc="last")
    wide.columns = wide.columns.astype(int)
    log.info(
        "comovement_loader: pulled returns for %d permnos over %d dates",
        wide.shape[1], wide.shape[0],
    )
    return wide


def _pull_market_returns(
    window_start: date,
    window_end: date,
    conn: "wrds.Connection",
) -> pd.Series:
    """
    Pull CRSP VW market returns (including dividends) for the date range.

    Returns a Series indexed by date.
    """
    sql = f"""
        SELECT date, vwretd
        FROM {_DSI}
        WHERE date >= %(start)s AND date <= %(end)s
          AND vwretd IS NOT NULL
        ORDER BY date
    """
    df = conn.raw_sql(sql, params={"start": window_start, "end": window_end})
    df["date"] = pd.to_datetime(df["date"])
    df["vwretd"] = pd.to_numeric(df["vwretd"], errors="coerce")
    return df.set_index("date")["vwretd"]


def _compute_partial_correlations(
    returns_wide: pd.DataFrame,
    market_ret: pd.Series,
    window_days: int,
    min_obs: int,
) -> pd.DataFrame:
    """
    Strip the VW market factor via OLS and return the pairwise partial-correlation matrix.

    Steps:
    1. Align on dates where market return is non-NaN; keep only the last window_days dates.
    2. Drop stocks with fewer than min_obs non-NaN returns in the window.
    3. Compute per-stock OLS beta vs the market on complete cases (unbiased, vectorized).
    4. Compute OLS residuals (NaN propagates where original return was NaN).
    5. Return pandas pairwise correlation of residuals (handles NaN via pairwise complete).
    """
    # Align to market dates, keep last window_days
    market_aligned = market_ret.reindex(returns_wide.index).dropna()
    returns_aligned = returns_wide.reindex(market_aligned.index)
    if len(returns_aligned) > window_days:
        returns_aligned = returns_aligned.iloc[-window_days:]
        market_aligned = market_aligned.reindex(returns_aligned.index)

    # Drop stocks below min_obs
    valid_stocks = returns_aligned.count() >= min_obs
    returns_aligned = returns_aligned.loc[:, valid_stocks]
    if returns_aligned.empty:
        return pd.DataFrame()

    M = market_aligned.to_numpy(dtype=float, na_value=np.nan)   # shape (T,)
    R = returns_aligned.to_numpy(dtype=float, na_value=np.nan)  # shape (T, N), may contain NaN

    # Vectorized unbiased OLS beta per stock on complete-case pairs
    mask = ~np.isnan(R)                                     # (T, N) True where ret is valid
    M2_per_col = np.sum((M[:, None] ** 2) * mask, axis=0)  # sum of m_t^2 over valid days, per stock
    Mrsum = np.nansum(M[:, None] * R, axis=0)              # sum of m_t * r_it, per stock
    betas = np.where(M2_per_col > 0, Mrsum / M2_per_col, 0.0)  # (N,)

    # OLS residuals — NaN propagates where R is NaN (correct: don't use those obs)
    residuals = R - M[:, None] * betas[None, :]            # (T, N)
    residuals_df = pd.DataFrame(
        residuals, index=returns_aligned.index, columns=returns_aligned.columns
    )

    return residuals_df.corr()  # pairwise-complete correlation


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_comovement(
    as_of: date,
    wrds_conn: "wrds.Connection",
    driver: "Driver",
    window_days: int = 252,
    min_obs: int = 120,
    min_partial_corr: float = 0.30,
    batch_size: int = 500,
    database: str | None = None,
) -> dict[str, int]:
    """
    Compute trailing partial-correlation co-movement edges and write to Neo4j.

    Parameters
    ----------
    as_of            : session T0; window ends strictly before this date
    wrds_conn        : active wrds.Connection
    driver           : Neo4j driver
    window_days      : trading days in the trailing return window (default 252 = 1 year)
    min_obs          : minimum non-NaN daily returns required to include a stock
    min_partial_corr : minimum partial correlation to write an edge (default 0.30)
    batch_size       : rows per Neo4j UNWIND batch
    database         : Neo4j database (falls back to NEO4J_DATABASE env var)

    Returns stats dict: edges_written, pairs_above_threshold, stocks_used,
    stocks_dropped_min_obs, nodes_merged.
    """
    db = database or os.getenv("NEO4J_DATABASE", "neo4j")

    # 1. Get permnos from the graph
    permnos = _get_graph_permnos(driver, db)
    if not permnos:
        log.warning("comovement_loader: no Security nodes found in graph")
        return _empty_stats()

    # 2. Date window — strictly before T0
    window_end   = as_of - timedelta(days=1)
    window_start = as_of - timedelta(days=int(window_days * 1.55))

    # 3. Pull returns and market factor
    returns_wide = _pull_returns(permnos, window_start, window_end, wrds_conn)
    if returns_wide.empty:
        log.warning("comovement_loader: no return data found in window %s – %s", window_start, window_end)
        return _empty_stats()

    market_ret = _pull_market_returns(window_start, window_end, wrds_conn)

    # 4. Compute partial correlation matrix
    log.info(
        "comovement_loader: computing partial correlations (%d stocks, window up to %d days)",
        returns_wide.shape[1], window_days,
    )
    n_stocks_before = returns_wide.shape[1]
    corr_matrix = _compute_partial_correlations(returns_wide, market_ret, window_days, min_obs)
    if corr_matrix.empty:
        log.warning("comovement_loader: no stocks survived min_obs=%d filter", min_obs)
        return _empty_stats()

    n_stocks_used = len(corr_matrix)
    n_stocks_dropped = n_stocks_before - n_stocks_used
    log.info(
        "comovement_loader: %d stocks used, %d dropped (min_obs=%d)",
        n_stocks_used, n_stocks_dropped, min_obs,
    )

    # 5. Extract pairs above threshold (upper triangle only — undirected)
    corr_vals = corr_matrix.values
    cols = corr_matrix.columns.tolist()
    rows_idx, cols_idx = np.triu_indices(len(cols), k=1)
    pairs_corr = corr_vals[rows_idx, cols_idx]

    above = pairs_corr >= min_partial_corr
    n_above = above.sum()
    log.info(
        "comovement_loader: %d pairs above min_partial_corr=%.2f",
        n_above, min_partial_corr,
    )

    if n_above == 0:
        return {
            "edges_written": 0,
            "pairs_above_threshold": 0,
            "stocks_used": n_stocks_used,
            "stocks_dropped_min_obs": n_stocks_dropped,
            "nodes_merged": 0,
        }

    as_of_str = as_of.isoformat()
    window_end_str = window_end.isoformat()

    edges = []
    for i in np.where(above)[0]:
        p1 = int(cols[rows_idx[i]])
        p2 = int(cols[cols_idx[i]])
        # Canonical direction: lower permno first (undirected)
        src, dst = (f"permno:{min(p1,p2)}", f"permno:{max(p1,p2)}")
        edges.append({
            "src":         src,
            "dst":         dst,
            "weight":      float(round(pairs_corr[i], 6)),
            "as_of":       as_of_str,
            "window_end":  window_end_str,
            "window_days": window_days,
            "provenance":  "crsp_comovement",
        })

    # 6. Write to Neo4j
    cypher = """
        UNWIND $batch AS row
        MERGE (a:Security {canonical_id: row.src})
        MERGE (b:Security {canonical_id: row.dst})
        MERGE (a)-[r:COMOVES_WITH {src: row.src, dst: row.dst, provenance: row.provenance}]->(b)
        SET r.weight      = row.weight,
            r.as_of       = row.as_of,
            r.window_end  = row.window_end,
            r.window_days = row.window_days
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
        "comovement_loader: %d :COMOVES_WITH edges written, %d nodes touched",
        n_written, len(node_ids),
    )
    return {
        "edges_written": n_written,
        "pairs_above_threshold": int(n_above),
        "stocks_used": n_stocks_used,
        "stocks_dropped_min_obs": n_stocks_dropped,
        "nodes_merged": len(node_ids),
    }


def _empty_stats() -> dict[str, int]:
    return {
        "edges_written": 0,
        "pairs_above_threshold": 0,
        "stocks_used": 0,
        "stocks_dropped_min_obs": 0,
        "nodes_merged": 0,
    }
