"""
Offline unit tests for candidate_generator.py — no WRDS or OptionMetrics credentials required.
"""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from blind_spot.candidate_generator import (
    Candidate,
    _compute_salience,
    _parse_permno,
    _pull_atm_iv,
    _pull_atm_straddle,
    _pull_iv_history,
    _resolve_universe_secids,
    generate,
)

AS_OF  = date(2023, 12, 29)   # a Friday — last trading day of 2023
WINDOW = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_conn(**sql_returns):
    """Return a mock wrds.Connection whose raw_sql yields each DataFrame in call order."""
    conn = MagicMock()
    conn.raw_sql.side_effect = list(sql_returns.values())
    return conn


def conn_sequence(*dfs):
    """Return a mock conn whose successive raw_sql calls return dfs in order."""
    conn = MagicMock()
    conn.raw_sql.side_effect = list(dfs)
    return conn


def link_df(rows):
    """Build an opcrsphist-like DataFrame."""
    return pd.DataFrame(rows, columns=["permno", "secid", "score"])


def vsurfd_df(secids, dates, ivs):
    """Build a vsurfd-like long DataFrame."""
    rows = []
    for secid, d, iv in zip(secids, dates, ivs):
        rows.append({"secid": secid, "date": str(d), "impl_volatility": iv})
    return pd.DataFrame(rows)


def opprcd_df(rows):
    """Build an opprcd-like DataFrame."""
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# _parse_permno
# ---------------------------------------------------------------------------

def test_parse_permno_valid():
    assert _parse_permno("permno:14593") == 14593


def test_parse_permno_none_input():
    assert _parse_permno(None) is None


def test_parse_permno_wrong_prefix():
    assert _parse_permno("gvkey:001011") is None


def test_parse_permno_malformed():
    assert _parse_permno("permno:abc") is None


# ---------------------------------------------------------------------------
# _resolve_universe_secids
# ---------------------------------------------------------------------------

def test_resolve_universe_secids_basic():
    conn = conn_sequence(link_df([
        {"permno": 10000, "secid": 5001, "score": 1.0},
        {"permno": 14593, "secid": 5002, "score": 1.0},
    ]))
    result = _resolve_universe_secids([10000, 14593], AS_OF, conn)
    assert result == {10000: 5001, 14593: 5002}


def test_resolve_universe_secids_picks_best_score():
    conn = conn_sequence(link_df([
        {"permno": 10000, "secid": 5001, "score": 6.0},
        {"permno": 10000, "secid": 5099, "score": 1.0},  # better link
    ]))
    result = _resolve_universe_secids([10000], AS_OF, conn)
    assert result[10000] == 5099


def test_resolve_universe_secids_empty_permnos():
    conn = MagicMock()
    result = _resolve_universe_secids([], AS_OF, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


def test_resolve_universe_secids_empty_df():
    conn = conn_sequence(pd.DataFrame(columns=["permno", "secid", "score"]))
    result = _resolve_universe_secids([10000], AS_OF, conn)
    assert result == {}


# ---------------------------------------------------------------------------
# _pull_atm_iv
# ---------------------------------------------------------------------------

def test_pull_atm_iv_returns_dict():
    df = pd.DataFrame({"secid": [5001, 5002], "impl_volatility": [0.35, 0.22]})
    conn = conn_sequence(df)
    result = _pull_atm_iv([5001, 5002], AS_OF, conn)
    assert result == {5001: 0.35, 5002: 0.22}


def test_pull_atm_iv_empty_secids():
    conn = MagicMock()
    result = _pull_atm_iv([], AS_OF, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


def test_pull_atm_iv_filters_nulls():
    df = pd.DataFrame({"secid": [5001, 5002], "impl_volatility": [0.35, None]})
    conn = conn_sequence(df)
    result = _pull_atm_iv([5001, 5002], AS_OF, conn)
    assert 5001 in result
    assert 5002 not in result


# ---------------------------------------------------------------------------
# _pull_iv_history — min/max computation
# ---------------------------------------------------------------------------

def _make_history_df(secid, n_obs, iv_base=0.25, seed=42):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-01", periods=n_obs, freq="B")
    ivs = iv_base + rng.normal(0, 0.03, n_obs)
    return pd.DataFrame({
        "secid": secid,
        "date": dates.strftime("%Y-%m-%d"),
        "impl_volatility": np.clip(ivs, 0.01, None),
    })


def test_pull_iv_history_returns_min_max():
    df = _make_history_df(5001, 60)
    conn = conn_sequence(df)
    result = _pull_iv_history([5001], AS_OF, WINDOW, conn)
    assert 5001 in result
    lo, hi = result[5001]
    assert lo <= hi
    assert lo > 0


def test_pull_iv_history_min_obs_floor():
    # Only 5 obs — below MIN_IV_OBS (10)
    df = _make_history_df(5001, 5)
    conn = conn_sequence(df)
    result = _pull_iv_history([5001], AS_OF, WINDOW, conn)
    assert result == {}


def test_pull_iv_history_empty_secids():
    conn = MagicMock()
    result = _pull_iv_history([], AS_OF, WINDOW, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


def test_pull_iv_history_two_years_queries_twice():
    """Window spanning two calendar years should fire two SQL calls."""
    df2022 = _make_history_df(5001, 30)
    df2023 = _make_history_df(5001, 30, seed=99)
    conn = conn_sequence(df2022, df2023)
    # Use a window that forces as_of.year-1 to be included
    result = _pull_iv_history([5001], date(2023, 1, 31), window_days=200, conn=conn)
    assert conn.raw_sql.call_count == 2


# ---------------------------------------------------------------------------
# _pull_atm_straddle
# ---------------------------------------------------------------------------

def test_pull_atm_straddle_basic():
    cat_date = date(2024, 1, 10)
    expiry   = date(2024, 1, 19)  # first expiry after catalyst
    df = pd.DataFrame([
        {"secid": 5001, "exdate": str(expiry), "cp_flag": "C",
         "best_bid": 4.0, "best_offer": 4.5, "delta": 0.51, "open_interest": 100},
        {"secid": 5001, "exdate": str(expiry), "cp_flag": "P",
         "best_bid": 3.5, "best_offer": 4.0, "delta": -0.49, "open_interest": 80},
    ])
    conn = conn_sequence(df)
    result = _pull_atm_straddle({5001: cat_date}, AS_OF, conn)
    assert 5001 in result
    expected = (4.0 + 4.5) / 2 + (3.5 + 4.0) / 2  # call_mid + put_mid
    assert abs(result[5001] - expected) < 1e-9


def test_pull_atm_straddle_no_expiry_after_catalyst():
    cat_date = date(2024, 3, 1)
    expiry   = date(2024, 1, 19)  # before catalyst → should be ignored
    df = pd.DataFrame([
        {"secid": 5001, "exdate": str(expiry), "cp_flag": "C",
         "best_bid": 4.0, "best_offer": 4.5, "delta": 0.51, "open_interest": 100},
    ])
    conn = conn_sequence(df)
    result = _pull_atm_straddle({5001: cat_date}, AS_OF, conn)
    assert result == {}


def test_pull_atm_straddle_empty_dict():
    conn = MagicMock()
    result = _pull_atm_straddle({}, AS_OF, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


# ---------------------------------------------------------------------------
# _compute_salience
# ---------------------------------------------------------------------------

def _make_records(n=6, seed=7):
    rng = np.random.default_rng(seed)
    return [
        {
            "canonical_id": f"permno:{10000 + i}",
            "implied_move": None,
            "iv_rank": float(rng.uniform(0, 1)),
            "iv_level": float(rng.uniform(0.1, 0.6)),
            "measure": "iv_rank",
            "coverage": True,
            "siccd": 3500 + i * 100,
            "mktcap": float(rng.uniform(1e9, 1e11)),
        }
        for i in range(n)
    ]


def test_compute_salience_all_in_01():
    records = _make_records(10)
    out = _compute_salience(records)
    for r in out:
        assert 0.0 <= r["salience"] <= 1.0


def test_compute_salience_sorted_desc():
    records = _make_records(8)
    out = _compute_salience(records)
    saliences = [r["salience"] for r in out]
    assert saliences == sorted(saliences, reverse=True)


def test_compute_salience_deterministic():
    records1 = _make_records(6, seed=42)
    records2 = _make_records(6, seed=42)
    out1 = _compute_salience(records1)
    out2 = _compute_salience(records2)
    assert [r["canonical_id"] for r in out1] == [r["canonical_id"] for r in out2]
    assert [r["salience"] for r in out1] == [r["salience"] for r in out2]


def test_compute_salience_empty():
    assert _compute_salience([]) == []


def test_compute_salience_higher_iv_rank_gets_higher_salience():
    """Within a single bucket, higher iv_rank should always get higher or equal salience."""
    records = [
        {"canonical_id": "permno:10000", "iv_rank": 0.9, "iv_level": 0.5,
         "measure": "iv_rank", "coverage": True, "siccd": 3500, "mktcap": 5e9,
         "implied_move": None},
        {"canonical_id": "permno:10001", "iv_rank": 0.1, "iv_level": 0.2,
         "measure": "iv_rank", "coverage": True, "siccd": 3500, "mktcap": 4e9,
         "implied_move": None},
    ]
    out = _compute_salience(records)
    # permno:10000 (iv_rank=0.9) should rank first
    assert out[0]["canonical_id"] == "permno:10000"


# ---------------------------------------------------------------------------
# generate — full pipeline (mocked)
# ---------------------------------------------------------------------------

def _make_generate_conn(
    n_stocks=4,
    seed=42,
    with_straddle=False,
):
    """
    Build a mock conn whose raw_sql calls return data in the exact order
    that generate() issues them:
      1. _resolve_universe_secids → link_df
      2. _pull_atm_iv             → vsurfd df (today)
      3. _pull_iv_history         → vsurfd df (window, 1 year = 1 query)
      4. _pull_underlying_prices  → secprd df  (only if with_straddle)
      5. _pull_atm_straddle       → opprcd df  (only if with_straddle)
      6. _pull_sic_codes          → stocknames df
      7. _pull_market_caps        → secprd df
    """
    rng = np.random.default_rng(seed)
    permnos = list(range(10000, 10000 + n_stocks))
    secids  = list(range(5000, 5000 + n_stocks))

    # 1. link
    link = pd.DataFrame({"permno": permnos, "secid": secids, "score": [1.0] * n_stocks})

    # 2. ATM IV today
    iv_today_vals = rng.uniform(0.2, 0.6, n_stocks)
    iv_today = pd.DataFrame({"secid": secids, "impl_volatility": iv_today_vals})

    # 3. IV history (60 obs each)
    hist_rows = []
    for i, secid in enumerate(secids):
        dates = pd.date_range("2023-01-01", periods=60, freq="B")
        ivs = rng.uniform(0.15, 0.55, 60)
        for d, iv in zip(dates, ivs):
            hist_rows.append({"secid": secid, "date": str(d.date()), "impl_volatility": iv})
    hist_df = pd.DataFrame(hist_rows)

    # 4+5. Straddle (optional) — generate() calls _pull_atm_straddle before _pull_underlying_prices
    straddle_dfs: list[pd.DataFrame] = []
    if with_straddle:
        # opprcd rows (call 4)
        expiry = AS_OF + timedelta(days=30)
        opt_rows = []
        for secid in secids:
            for cp, delta in [("C", 0.50), ("P", -0.50)]:
                opt_rows.append({
                    "secid": secid, "exdate": str(expiry), "cp_flag": cp,
                    "best_bid": 3.0, "best_offer": 3.5,
                    "delta": delta, "open_interest": 200,
                })
        # underlying prices (call 5)
        prices_df = pd.DataFrame({
            "secid": secids, "date": str(AS_OF - timedelta(days=1)),
            "close": rng.uniform(50, 300, n_stocks),
        })
        straddle_dfs = [pd.DataFrame(opt_rows), prices_df]

    # 6. SIC codes
    sic_df = pd.DataFrame({"permno": permnos, "siccd": [3500 + i * 100 for i in range(n_stocks)]})

    # 7. Market caps
    cap_df = pd.DataFrame({
        "secid": secids,
        "date": [str(AS_OF - timedelta(days=1))] * n_stocks,
        "close": rng.uniform(50, 300, n_stocks),
        "shrout": rng.uniform(100_000, 500_000, n_stocks),
    })

    all_dfs = [link, iv_today, hist_df] + straddle_dfs + [sic_df, cap_df]
    conn = MagicMock()
    conn.raw_sql.side_effect = all_dfs
    return conn


def _make_universe(n=4):
    return [f"permno:{10000 + i}" for i in range(n)]


def test_generate_returns_candidates():
    conn = _make_generate_conn(n_stocks=4)
    result = generate(_make_universe(4), AS_OF, wrds_conn=conn, window_days=WINDOW)
    assert isinstance(result, list)
    assert all(isinstance(c, Candidate) for c in result)
    assert len(result) == 4


def test_generate_sorted_by_salience_desc():
    conn = _make_generate_conn(n_stocks=5)
    result = generate(_make_universe(5), AS_OF, wrds_conn=conn, window_days=WINDOW)
    saliences = [c.salience for c in result]
    assert saliences == sorted(saliences, reverse=True)


def test_generate_as_of_field():
    conn = _make_generate_conn(n_stocks=3)
    result = generate(_make_universe(3), AS_OF, wrds_conn=conn, window_days=WINDOW)
    expected_dt = datetime.combine(AS_OF, datetime.min.time())
    assert all(c.as_of == expected_dt for c in result)


def test_generate_empty_universe():
    conn = MagicMock()
    result = generate([], AS_OF, wrds_conn=conn)
    assert result == []
    conn.raw_sql.assert_not_called()


def test_generate_no_om_coverage():
    """When no permnos resolve to secids, return empty list."""
    conn = conn_sequence(pd.DataFrame(columns=["permno", "secid", "score"]))
    result = generate(_make_universe(3), AS_OF, wrds_conn=conn)
    assert result == []


def test_generate_with_straddle_measure():
    catalyst = {f"permno:{10000 + i}": AS_OF + timedelta(days=5) for i in range(4)}
    conn = _make_generate_conn(n_stocks=4, with_straddle=True)
    result = generate(_make_universe(4), AS_OF, events=catalyst, wrds_conn=conn, window_days=WINDOW)
    straddle_candidates = [c for c in result if c.measure == "straddle"]
    assert len(straddle_candidates) > 0


def test_generate_coverage_flag():
    conn = _make_generate_conn(n_stocks=4)
    result = generate(_make_universe(4), AS_OF, wrds_conn=conn, window_days=WINDOW)
    # All stocks have ATM IV in our mock → all covered
    assert all(c.coverage for c in result)


# ---------------------------------------------------------------------------
# Snapshot reproducibility — determinism invariant (BUILD.md §8)
# ---------------------------------------------------------------------------

def test_generate_is_deterministic():
    """Identical data in → identical candidate ranking out."""
    conn1 = _make_generate_conn(n_stocks=5, seed=123)
    conn2 = _make_generate_conn(n_stocks=5, seed=123)
    result1 = generate(_make_universe(5), AS_OF, wrds_conn=conn1, window_days=WINDOW)
    result2 = generate(_make_universe(5), AS_OF, wrds_conn=conn2, window_days=WINDOW)
    assert [c.canonical_id for c in result1] == [c.canonical_id for c in result2]
    assert [c.salience for c in result1] == [c.salience for c in result2]
    assert [c.iv_rank for c in result1] == [c.iv_rank for c in result2]


def test_generate_different_snapshots_may_differ():
    """Different data vintages may produce a different ranking."""
    conn1 = _make_generate_conn(n_stocks=5, seed=1)
    conn2 = _make_generate_conn(n_stocks=5, seed=999)
    result1 = generate(_make_universe(5), AS_OF, wrds_conn=conn1, window_days=WINDOW)
    result2 = generate(_make_universe(5), AS_OF, wrds_conn=conn2, window_days=WINDOW)
    # Both should produce valid lists; rankings may differ
    assert len(result1) == 5
    assert len(result2) == 5
