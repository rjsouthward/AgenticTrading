"""
Offline unit tests for PolygonBarSource — no real API key or network required.
All HTTP calls are intercepted via httpx's MockTransport.
"""
from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import time
from blind_spot.market_data import BARS_COLUMNS, BarSource, PolygonBarSource, _RateLimiter, build_permno_ticker_map


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

PERMNO_TO_TICKER = {10000: "AAPL", 20000: "NVDA", 30000: "MSFT"}
START = date(2024, 12, 2)
END   = date(2024, 12, 6)   # ~5 trading days

# Minimal Polygon aggs response for one ticker
def _polygon_response(ticker: str, n_bars: int = 5, base_close: float = 100.0) -> dict:
    bars = []
    close = base_close
    ts_ms = 1733097600000  # 2024-12-02 00:00:00 UTC in ms
    ONE_DAY_MS = 86_400_000
    for i in range(n_bars):
        close *= (1 + 0.01 * (i % 2 * 2 - 1))  # alternates +1% / -1%
        bars.append({"t": ts_ms + i * ONE_DAY_MS, "o": close * 0.99,
                     "h": close * 1.01, "l": close * 0.98,
                     "c": close, "v": 1_000_000.0})
    return {"ticker": ticker, "results": bars, "status": "OK"}


def _make_src(api_responses: dict[str, dict] | None = None) -> PolygonBarSource:
    """Build a PolygonBarSource that intercepts HTTP via httpx MockTransport."""
    import json, httpx

    responses = api_responses or {t: _polygon_response(t) for t in PERMNO_TO_TICKER.values()}
    responses["SPY"] = _polygon_response("SPY", base_close=500.0)

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.split("/")[4]   # /v2/aggs/ticker/{ticker}/...
        body = responses.get(ticker, {"results": [], "status": "OK"})
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)

    src = PolygonBarSource(
        api_key="test-key",
        permno_to_ticker=PERMNO_TO_TICKER,
        max_workers=1,
    )
    # Patch _fetch_ticker_bars to use mock transport
    original_fetch = src._fetch_ticker_bars.__func__

    def patched_fetch(self, ticker, start, end):
        import httpx
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000, "apiKey": self._api_key}
        results = []
        with httpx.Client(transport=transport, timeout=5.0) as client:
            while url:
                r = client.get(url, params=params)
                r.raise_for_status()
                body = r.json()
                results.extend(body.get("results") or [])
                url = body.get("next_url", "")
                params = {"apiKey": self._api_key}
        return results

    import types
    src._fetch_ticker_bars = types.MethodType(patched_fetch, src)
    return src


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_polygon_satisfies_barsource_protocol():
    src = PolygonBarSource(api_key="x", permno_to_ticker={})
    assert isinstance(src, BarSource)


# ---------------------------------------------------------------------------
# get_bars
# ---------------------------------------------------------------------------

def test_get_bars_returns_correct_columns():
    src = _make_src()
    df = src.get_bars(list(PERMNO_TO_TICKER), START, END)
    assert list(df.columns) == BARS_COLUMNS


def test_get_bars_covers_mapped_permnos():
    src = _make_src()
    df = src.get_bars(list(PERMNO_TO_TICKER), START, END)
    assert set(df["permno"].unique()) == set(PERMNO_TO_TICKER)


def test_get_bars_computes_ret_from_adjusted_close():
    src = _make_src()
    df = src.get_bars([10000], START, END)
    # First bar has no previous close → ret should be NaN; subsequent bars non-NaN
    aapl = df[df["permno"] == 10000].sort_values("date")
    assert pd.isna(aapl.iloc[0]["ret"])
    assert not pd.isna(aapl.iloc[1]["ret"])
    # Return = (close[t] - close[t-1]) / close[t-1]
    c0 = aapl.iloc[0]["close"]
    c1 = aapl.iloc[1]["close"]
    assert aapl.iloc[1]["ret"] == pytest.approx((c1 - c0) / c0, rel=1e-6)


def test_get_bars_close_always_positive():
    src = _make_src()
    df = src.get_bars(list(PERMNO_TO_TICKER), START, END)
    assert (df["close"].dropna() > 0).all()


def test_get_bars_sorted_by_permno_date():
    src = _make_src()
    df = src.get_bars(list(PERMNO_TO_TICKER), START, END)
    expected = df.sort_values(["permno", "date"])
    pd.testing.assert_frame_equal(df.reset_index(drop=True), expected.reset_index(drop=True))


def test_get_bars_empty_permnos():
    src = _make_src()
    df = src.get_bars([], START, END)
    assert df.empty
    assert list(df.columns) == BARS_COLUMNS


def test_get_bars_unmapped_permnos_skipped():
    src = _make_src()
    df = src.get_bars([99999], START, END)   # no mapping for 99999
    assert df.empty


def test_get_bars_partial_mapping():
    src = _make_src()
    df = src.get_bars([10000, 99999], START, END)  # only 10000 is mapped
    assert set(df["permno"].unique()) == {10000}


def test_get_bars_failed_ticker_gracefully_skipped():
    import json, httpx

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.split("/")[4]
        if ticker == "NVDA":
            return httpx.Response(500, json={"status": "error"})
        return httpx.Response(200, json=_polygon_response(ticker))

    src = _make_src()
    import types, time

    def patched_fetch(self, ticker, start, end):
        import httpx
        transport = httpx.MockTransport(handler)
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
        params = {"apiKey": self._api_key}
        with httpx.Client(transport=transport, timeout=5.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()  # raises on 500
            return r.json().get("results", [])

    src._fetch_ticker_bars = types.MethodType(patched_fetch, src)
    df = src.get_bars(list(PERMNO_TO_TICKER), START, END)
    # NVDA failed but AAPL and MSFT should still be present
    assert 20000 not in set(df["permno"].unique())   # NVDA skipped
    assert 10000 in set(df["permno"].unique())        # AAPL present
    assert 30000 in set(df["permno"].unique())        # MSFT present


def test_get_bars_shrout_from_map():
    src = PolygonBarSource(
        api_key="test",
        permno_to_ticker={10000: "AAPL"},
        shrout_map={10000: 15_000_000.0},  # 15B shares in thousands
        max_workers=1,
    )
    import types, httpx

    def patched_fetch(self, ticker, start, end):
        return _polygon_response(ticker)["results"]

    src._fetch_ticker_bars = types.MethodType(patched_fetch, src)
    df = src.get_bars([10000], START, END)
    assert (df["shrout"].dropna() == 15_000_000.0).all()


# ---------------------------------------------------------------------------
# get_market
# ---------------------------------------------------------------------------

def test_get_market_returns_series():
    src = _make_src()
    mkt = src.get_market(START, END)
    assert isinstance(mkt, pd.Series)
    assert mkt.index.dtype == "datetime64[ns]"


def test_get_market_no_nan_in_series():
    src = _make_src()
    mkt = src.get_market(START, END)
    assert not mkt.isna().any()


def test_get_market_sorted_ascending():
    src = _make_src()
    mkt = src.get_market(START, END)
    assert list(mkt.index) == sorted(mkt.index)


# ---------------------------------------------------------------------------
# build_permno_ticker_map
# ---------------------------------------------------------------------------

def test_build_permno_ticker_map_basic():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame({
        "permno": [10000, 20000],
        "ticker": ["AAPL", "NVDA"],
    })
    result = build_permno_ticker_map([10000, 20000], date(2024, 12, 6), conn)
    assert result == {10000: "AAPL", 20000: "NVDA"}


def test_build_permno_ticker_map_empty_wrds():
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame()
    assert build_permno_ticker_map([10000], date(2024, 12, 6), conn) == {}


def test_build_permno_ticker_map_empty_input():
    conn = MagicMock()
    result = build_permno_ticker_map([], date(2024, 12, 6), conn)
    assert result == {}
    conn.raw_sql.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_spaces_calls():
    """At 60 req/min, calls should be at least ~1s apart (allow some jitter)."""
    limiter = _RateLimiter(rate_per_min=60)
    t0 = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.9   # ~1s between tokens, minus scheduler slack


def test_rate_limiter_first_call_is_immediate():
    """The bucket starts full — the first acquire should not sleep."""
    limiter = _RateLimiter(rate_per_min=5)
    t0 = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - t0 < 0.1


# ---------------------------------------------------------------------------
# Bearer auth
# ---------------------------------------------------------------------------

def test_api_key_lives_in_authorization_header_not_url():
    """Security regression: the API key must never appear in URL query params."""
    src = PolygonBarSource(
        api_key="super-secret-key-must-not-leak",
        permno_to_ticker={10000: "AAPL"},
    )
    assert src._headers == {"Authorization": "Bearer super-secret-key-must-not-leak"}
    # Belt-and-braces: nothing else on the instance should hold the key in URL-form
    assert "apiKey" not in str(vars(src))
