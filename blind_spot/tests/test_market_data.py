"""
Offline unit tests for market_data.py — WrdsBarSource with a mocked WRDS connection.
"""
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from blind_spot.market_data import (
    BARS_COLUMNS,
    BarSource,
    WrdsBarSource,
    default_window,
)


def test_default_window_ends_before_as_of():
    start, end = default_window(date(2024, 6, 14), lookback_days=60)
    assert end < date(2024, 6, 14)
    assert start < end


def test_wrds_bar_source_satisfies_protocol():
    src = WrdsBarSource(MagicMock())
    assert isinstance(src, BarSource)


def test_get_bars_strips_prc_sign_and_missing_ret():
    conn = MagicMock()
    raw = pd.DataFrame({
        "permno": [10000, 10000, 20000],
        "date":   ["2024-06-10", "2024-06-11", "2024-06-11"],
        "open":   [10.0, 10.5, 50.0],
        "high":   [10.6, 10.9, 51.0],
        "low":    [9.8, 10.1, 49.0],
        "prc":    [10.5, -10.4, 50.5],   # negative = bid/ask midpoint, sign is not price
        "volume": [1000.0, 1200.0, 800.0],
        "ret":    [0.01, -2.0, 0.02],    # -2.0 is a CRSP missing sentinel (<= -1)
        "shrout": [50_000.0, 50_000.0, 30_000.0],
    })
    conn.raw_sql.return_value = raw
    src = WrdsBarSource(conn)
    bars = src.get_bars([10000, 20000], date(2024, 6, 1), date(2024, 6, 13))

    assert list(bars.columns) == BARS_COLUMNS
    # prc sign stripped
    assert (bars["close"] > 0).all()
    assert bars.loc[bars["date"] == "2024-06-11"].iloc[0]["close"] == pytest.approx(10.4)
    # missing-code return → NaN
    bad = bars[(bars["permno"] == 10000) & (bars["date"] == "2024-06-11")].iloc[0]
    assert np.isnan(bad["ret"])


def test_get_bars_empty():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame()
    src = WrdsBarSource(conn)
    bars = src.get_bars([10000], date(2024, 6, 1), date(2024, 6, 13))
    assert bars.empty
    assert list(bars.columns) == BARS_COLUMNS


def test_get_bars_no_permnos_skips_query():
    conn = MagicMock()
    src = WrdsBarSource(conn)
    out = src.get_bars([], date(2024, 6, 1), date(2024, 6, 13))
    assert out.empty
    conn.raw_sql.assert_not_called()


def test_get_market_returns_series():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({
        "date":   ["2024-06-10", "2024-06-11"],
        "vwretd": [0.004, -0.002],
    })
    src = WrdsBarSource(conn)
    mkt = src.get_market(date(2024, 6, 1), date(2024, 6, 13))
    assert isinstance(mkt, pd.Series)
    assert len(mkt) == 2
    assert mkt.iloc[0] == pytest.approx(0.004)
