"""
Offline unit tests for segment_loader.py — no WRDS or Neo4j credentials required.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, call

import pandas as pd
import pytest

from blind_spot.segment_loader import (
    _FILING_LAG_DAYS,
    _pull_company_sales,
    _pull_customer_gvkeys,
    _pull_filing_dates,
    load_segments,
)

AS_OF = date(2023, 6, 30)
FY = 2022


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_conn(*dfs):
    """Return a conn whose raw_sql returns dfs in sequence."""
    conn = MagicMock()
    conn.raw_sql.side_effect = list(dfs)
    return conn


def make_driver():
    session_ctx = MagicMock()
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session_ctx


def seg_df(**kwargs):
    defaults = {
        "gvkey": ["001076"],
        "cid": [1],
        "sid": [1],
        "cnms": ["WALMART INC"],
        "salecs": [500.0],
        "datadate": [date(2022, 12, 31)],
    }
    defaults.update(kwargs)
    return pd.DataFrame(defaults)


# ---------------------------------------------------------------------------
# _pull_company_sales
# ---------------------------------------------------------------------------

def test_pull_company_sales_returns_dict():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({"gvkey": ["001076"], "sale": [3200.0]})
    result = _pull_company_sales(["001076"], FY, conn)
    assert result == {"001076": 3200.0}


def test_pull_company_sales_empty_input():
    conn = MagicMock()
    result = _pull_company_sales([], FY, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


# ---------------------------------------------------------------------------
# _pull_customer_gvkeys
# ---------------------------------------------------------------------------

def test_pull_customer_gvkeys_returns_mapping():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({
        "gvkey": ["001076"],
        "cid": [1],
        "sid": [1],
        "cgvkey": ["001690"],
    })
    result = _pull_customer_gvkeys(["001076"], FY, conn)
    assert result == {("001076", 1, 1): "001690"}


def test_pull_customer_gvkeys_zero_pads():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({
        "gvkey": ["1076"],   # no padding in raw data
        "cid": [1],
        "sid": [1],
        "cgvkey": ["1690"],
    })
    result = _pull_customer_gvkeys(["001076"], FY, conn)
    assert ("001076", 1, 1) in result
    assert result[("001076", 1, 1)] == "001690"


# ---------------------------------------------------------------------------
# _pull_filing_dates
# ---------------------------------------------------------------------------

def test_pull_filing_dates_returns_date():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({
        "gvkey": ["001076"],
        "fdate": [date(2023, 2, 15)],
    })
    result = _pull_filing_dates(["001076"], FY, conn)
    assert result["001076"] == date(2023, 2, 15)


def test_pull_filing_dates_empty_input():
    conn = MagicMock()
    result = _pull_filing_dates([], FY, conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


# ---------------------------------------------------------------------------
# load_segments — full pipeline (mocked)
# ---------------------------------------------------------------------------

def _make_load_conn(
    segs=None,
    company_sales=None,
    customer_gvkeys=None,
    filing_dates=None,
    supplier_permnos=None,
    customer_permnos=None,
):
    """Wire up a conn whose raw_sql returns fixtures in load_segments call order."""
    if segs is None:
        segs = seg_df()
    if company_sales is None:
        company_sales = pd.DataFrame({"gvkey": ["001076"], "sale": [3200.0]})
    if customer_gvkeys is None:
        customer_gvkeys = pd.DataFrame({"gvkey": ["001076"], "cid": [1], "sid": [1], "cgvkey": ["001690"]})
    if filing_dates is None:
        filing_dates = pd.DataFrame({"gvkey": ["001076"], "fdate": [date(2023, 2, 15)]})
    if supplier_permnos is None:
        supplier_permnos = pd.DataFrame({"gvkey": ["001076"], "lpermno": [10000]})
    if customer_permnos is None:
        customer_permnos = pd.DataFrame({"gvkey": ["001690"], "lpermno": [14593]})
    conn = MagicMock()
    conn.raw_sql.side_effect = [
        segs, company_sales, customer_gvkeys, filing_dates,
        supplier_permnos, customer_permnos,
    ]
    return conn


def test_load_segments_happy_path():
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    stats = load_segments(FY, AS_OF, conn, driver)

    assert stats["edges_written"] == 1
    assert stats["edges_null_customer"] == 0
    assert stats["edges_skipped_postdated"] == 0
    assert stats["nodes_merged"] == 2


def test_load_segments_edge_direction_is_supplier_to_customer():
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert batch[0]["src"] == "permno:10000"   # supplier
    assert batch[0]["dst"] == "permno:14593"   # customer


def test_load_segments_source_span_contains_customer_name():
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert "WALMART" in batch[0]["source_span"]


def test_load_segments_revenue_fraction():
    # salecs=500, sale=3200 → weight ≈ 0.156
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert abs(batch[0]["weight"] - 500 / 3200) < 1e-9


def test_load_segments_filing_date_as_as_of():
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert batch[0]["as_of"] == "2023-02-15"
    assert batch[0]["lag_estimated"] is False


def test_load_segments_lag_estimated_when_no_filing_date():
    """When no SEC filing date is found, fall back to datadate + 90d."""
    conn = _make_load_conn(
        filing_dates=pd.DataFrame(columns=["gvkey", "fdate"])  # empty
    )
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    expected_date = (date(2022, 12, 31) + timedelta(days=_FILING_LAG_DAYS)).isoformat()
    assert batch[0]["as_of"] == expected_date
    assert batch[0]["lag_estimated"] is True


def test_load_segments_postdated_edge_skipped():
    """Edge with filing_date >= as_of must not be written."""
    # filing date is after as_of
    conn = _make_load_conn(
        filing_dates=pd.DataFrame({"gvkey": ["001076"], "fdate": [date(2023, 7, 1)]})
    )
    driver, _ = make_driver()

    stats = load_segments(FY, AS_OF, conn, driver)  # as_of = 2023-06-30

    assert stats["edges_written"] == 0
    assert stats["edges_skipped_postdated"] == 1


def test_load_segments_null_customer_skipped():
    """No seglink match → customer stays None → edge skipped, counted separately."""
    conn = _make_load_conn(
        customer_gvkeys=pd.DataFrame(columns=["gvkey", "cid", "sid", "cgvkey"])  # empty
    )
    driver, _ = make_driver()

    # With no customer gvkeys, the customer_permnos call won't happen; reset side_effect
    conn.raw_sql.side_effect = [
        seg_df(),
        pd.DataFrame({"gvkey": ["001076"], "sale": [3200.0]}),
        pd.DataFrame(columns=["gvkey", "cid", "sid", "cgvkey"]),  # empty seglink
        pd.DataFrame({"gvkey": ["001076"], "fdate": [date(2023, 2, 15)]}),
        pd.DataFrame({"gvkey": ["001076"], "lpermno": [10000]}),  # supplier permno
        pd.DataFrame(columns=["gvkey", "lpermno"]),               # customer permno (none)
    ]

    stats = load_segments(FY, AS_OF, conn, driver)

    assert stats["edges_written"] == 0
    assert stats["edges_null_customer"] == 1


def test_load_segments_min_revenue_frac_filter():
    """Edges below min_revenue_frac are dropped before resolution."""
    # salecs=500, sale=3200 → weight ≈ 0.156; filter at 0.20 should drop it
    conn = _make_load_conn()
    driver, _ = make_driver()

    stats = load_segments(FY, AS_OF, conn, driver, min_revenue_frac=0.20)

    assert stats["edges_written"] == 0
    assert stats["edges_skipped_frac"] == 1


def test_load_segments_provenance():
    conn = _make_load_conn()
    driver, session_ctx = make_driver()

    load_segments(FY, AS_OF, conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert batch[0]["provenance"] == "compustat_segment"
    assert batch[0]["fiscal_year"] == FY


# ---------------------------------------------------------------------------
# Point-in-time determinism
# ---------------------------------------------------------------------------

def test_load_segments_is_deterministic():
    driver1, _ = make_driver()
    driver2, _ = make_driver()

    stats1 = load_segments(FY, AS_OF, _make_load_conn(), driver1)
    stats2 = load_segments(FY, AS_OF, _make_load_conn(), driver2)

    assert stats1 == stats2
