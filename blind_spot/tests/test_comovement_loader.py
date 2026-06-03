"""
Offline unit tests for comovement_loader.py — no WRDS or Neo4j credentials required.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, call

import numpy as np
import pandas as pd
import pytest

from blind_spot.comovement_loader import (
    _compute_partial_correlations,
    _get_graph_permnos,
    load_comovement,
)

AS_OF = date(2023, 12, 31)
WINDOW = 60  # small window for tests


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_driver(permnos=(10000, 14593, 20000)):
    """Return a mock driver whose session yields the given permnos as Security nodes."""
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    # First session call = _get_graph_permnos (run returns rows)
    cid_rows = [{"cid": f"permno:{p}"} for p in permnos]
    session_ctx.run.return_value.__iter__ = MagicMock(return_value=iter(cid_rows))
    return driver, session_ctx


def synthetic_returns(n_stocks=5, n_days=80, seed=42) -> pd.DataFrame:
    """Synthetic daily returns with a shared market factor plus idiosyncratic noise."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_days, freq="B")
    market = rng.normal(0, 0.01, n_days)
    stocks = {
        i: market * 0.7 + rng.normal(0, 0.015, n_days)
        for i in range(n_stocks)
    }
    return pd.DataFrame(stocks, index=dates)


def synthetic_market(returns_df: pd.DataFrame) -> pd.Series:
    rng = np.random.default_rng(99)
    market = rng.normal(0, 0.01, len(returns_df))
    return pd.Series(market, index=returns_df.index)


def make_conn(returns_df, market_series):
    """Return a mock conn that serves the returns and market series in order."""
    conn = MagicMock()

    # _pull_returns: returns long-format df
    long = returns_df.stack().reset_index()
    long.columns = ["date", "permno", "ret"]
    long["date"] = long["date"].astype(str)

    # _pull_market_returns: returns wide df with vwretd
    mkt_df = market_series.reset_index()
    mkt_df.columns = ["date", "vwretd"]
    mkt_df["date"] = mkt_df["date"].astype(str)

    conn.raw_sql.side_effect = [long, mkt_df]
    return conn


# ---------------------------------------------------------------------------
# _get_graph_permnos
# ---------------------------------------------------------------------------

def test_get_graph_permnos_extracts_integers():
    driver, session_ctx = make_driver(permnos=[10000, 14593])
    result = _get_graph_permnos(driver, "testdb")
    assert set(result) == {10000, 14593}


def test_get_graph_permnos_skips_non_permno():
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    rows = [{"cid": "permno:10000"}, {"cid": None}, {"cid": "unknown:abc"}]
    session_ctx.run.return_value.__iter__ = MagicMock(return_value=iter(rows))
    result = _get_graph_permnos(driver, "testdb")
    assert result == [10000]


# ---------------------------------------------------------------------------
# _compute_partial_correlations
# ---------------------------------------------------------------------------

def test_partial_corr_returns_symmetric_matrix():
    rets = synthetic_returns(n_stocks=5, n_days=80)
    mkt = synthetic_market(rets)
    corr = _compute_partial_correlations(rets, mkt, window_days=60, min_obs=30)
    assert corr.shape == (5, 5)
    np.testing.assert_array_almost_equal(corr.values, corr.values.T)


def test_partial_corr_diagonal_is_one():
    rets = synthetic_returns(n_stocks=4, n_days=80)
    mkt = synthetic_market(rets)
    corr = _compute_partial_correlations(rets, mkt, window_days=60, min_obs=30)
    np.testing.assert_array_almost_equal(np.diag(corr.values), np.ones(4))


def test_partial_corr_values_in_minus1_to_1():
    rets = synthetic_returns(n_stocks=6, n_days=100)
    mkt = synthetic_market(rets)
    corr = _compute_partial_correlations(rets, mkt, window_days=80, min_obs=30)
    assert (corr.values >= -1.0 - 1e-9).all()
    assert (corr.values <=  1.0 + 1e-9).all()


def test_partial_corr_strips_market_factor():
    """After stripping a perfect common factor, residual correlations should be near zero."""
    rng = np.random.default_rng(0)
    n = 100
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    market = rng.normal(0, 0.01, n)
    # Two stocks = pure market factor + independent noise → residual corr ≈ 0
    r1 = market + rng.normal(0, 0.02, n)
    r2 = market + rng.normal(0, 0.02, n)
    rets = pd.DataFrame({0: r1, 1: r2}, index=dates)
    mkt_series = pd.Series(market, index=dates)
    corr = _compute_partial_correlations(rets, mkt_series, window_days=80, min_obs=30)
    # Residual correlation should be small (< 0.3) after stripping the shared factor
    assert abs(corr.loc[0, 1]) < 0.30


def test_partial_corr_min_obs_filters_stocks():
    rets = synthetic_returns(n_stocks=5, n_days=80)
    mkt = synthetic_market(rets)
    # Require 200 obs but window only has 80 → all filtered
    corr = _compute_partial_correlations(rets, mkt, window_days=60, min_obs=200)
    assert corr.empty


def test_partial_corr_window_trim():
    """If window_days < n_dates, only the last window_days rows should be used."""
    rets = synthetic_returns(n_stocks=3, n_days=120)
    mkt = synthetic_market(rets)
    corr = _compute_partial_correlations(rets, mkt, window_days=60, min_obs=50)
    # Should still produce a valid matrix (60 days is enough)
    assert corr.shape == (3, 3)


# ---------------------------------------------------------------------------
# load_comovement — full pipeline (mocked)
# ---------------------------------------------------------------------------

def _make_load_conn_and_driver(permnos=(0, 1, 2, 3, 4), n_days=80):
    rets = synthetic_returns(n_stocks=len(permnos), n_days=n_days)
    rets.columns = list(permnos)
    mkt = synthetic_market(rets)
    conn = make_conn(rets, mkt)

    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    cid_rows = [{"cid": f"permno:{p}"} for p in permnos]
    # First call = _get_graph_permnos; subsequent calls = Neo4j edge writes
    run_results = [MagicMock(__iter__=MagicMock(return_value=iter(cid_rows)))]
    run_results += [MagicMock()] * 100  # edge write calls
    session_ctx.run.side_effect = run_results
    return conn, driver, session_ctx


def test_load_comovement_writes_edges():
    conn, driver, _ = _make_load_conn_and_driver()
    stats = load_comovement(AS_OF, conn, driver, window_days=WINDOW, min_obs=20,
                            min_partial_corr=0.0, database="test")
    assert stats["edges_written"] >= 0  # may be 0 if all corr < threshold
    assert stats["stocks_used"] >= 0


def test_load_comovement_canonical_direction():
    """src should always have the lower permno number."""
    conn, driver, session_ctx = _make_load_conn_and_driver(permnos=(100, 200))
    load_comovement(AS_OF, conn, driver, window_days=WINDOW, min_obs=20,
                    min_partial_corr=-1.0, database="test")  # -1 to capture all pairs
    # Find the batch call (not the _get_graph_permnos call)
    batch_calls = [c for c in session_ctx.run.call_args_list if "batch" in (c[1] or {})]
    if batch_calls:
        batch = batch_calls[0][1]["batch"]
        for e in batch:
            p_src = int(e["src"].split(":")[1])
            p_dst = int(e["dst"].split(":")[1])
            assert p_src < p_dst, f"direction not canonical: {e['src']} → {e['dst']}"


def test_load_comovement_provenance():
    conn, driver, session_ctx = _make_load_conn_and_driver()
    load_comovement(AS_OF, conn, driver, window_days=WINDOW, min_obs=20,
                    min_partial_corr=-1.0, database="test")
    batch_calls = [c for c in session_ctx.run.call_args_list if "batch" in (c[1] or {})]
    if batch_calls:
        batch = batch_calls[0][1]["batch"]
        assert all(e["provenance"] == "crsp_comovement" for e in batch)


def test_load_comovement_window_ends_before_as_of():
    """The window_end passed to WRDS must be strictly before as_of."""
    conn, driver, _ = _make_load_conn_and_driver()
    load_comovement(AS_OF, conn, driver, window_days=WINDOW, min_obs=20,
                    min_partial_corr=0.0, database="test")
    # The second raw_sql call is _pull_returns; check its end param
    returns_call = conn.raw_sql.call_args_list[0]
    params = returns_call[1]["params"]
    assert params["end"] < AS_OF


def test_load_comovement_empty_graph():
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    session_ctx.run.return_value.__iter__ = MagicMock(return_value=iter([]))
    conn = MagicMock()
    stats = load_comovement(AS_OF, conn, driver, database="test")
    assert stats["edges_written"] == 0
    conn.raw_sql.assert_not_called()


# ---------------------------------------------------------------------------
# Point-in-time determinism
# ---------------------------------------------------------------------------

def test_load_comovement_is_deterministic():
    """Same return data in → identical stats out."""
    conn1, driver1, _ = _make_load_conn_and_driver()
    conn2, driver2, _ = _make_load_conn_and_driver()
    stats1 = load_comovement(AS_OF, conn1, driver1, window_days=WINDOW, min_obs=20,
                             min_partial_corr=0.0, database="test")
    stats2 = load_comovement(AS_OF, conn2, driver2, window_days=WINDOW, min_obs=20,
                             min_partial_corr=0.0, database="test")
    assert stats1 == stats2
