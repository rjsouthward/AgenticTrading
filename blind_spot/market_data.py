"""
Market-data abstraction for Blind Spot Lane B.

The salience layer needs *current* daily bars over the full universe every morning.
WRDS/CRSP is the right source for historical replay (survivorship-bias-free, delisting
returns) but lags by months and gates the universe through a subscription load. A live
feed (Polygon) is the right source for production but is survivorship-biased in history.

`BarSource` is the seam between the two. Lane B is written against this protocol and
neither knows nor cares where bars come from:

    backtest / eval   →  WrdsBarSource      (CRSP dsf/dsi)
    production         →  PolygonBarSource   (Polygon.io REST API)

Bars schema (long format, one row per permno-date)
--------------------------------------------------
    permno   int
    date     datetime64
    open     float | NaN
    high     float | NaN
    low      float | NaN
    close    float          (always positive; CRSP prc sign stripped)
    volume   float          (shares)
    ret      float | NaN    (total return incl. dividends; missing codes → NaN)
    shrout   float | NaN    (shares outstanding, thousands — for market cap)

Point-in-time rule
------------------
A source must return ONLY bars with date <= `end`. `end` is the last date whose close is
knowable strictly before session T0 (i.e. T0 - 1 trading day). Callers pass
`end = as_of - 1 day`; the source is not responsible for the T0 offset, only for never
returning a bar dated after `end`.

Polygon notes
-------------
Polygon returns adjusted bars (splits + dividends folded into price), so the price
return equals the total return — consistent with CRSP `ret`. Shares outstanding (`shrout`)
are not in daily aggs; pass `shrout_map` from a CRSP bootstrap or accept imperfect
market-cap bucketing (all unknowns land in the bottom quintile).

Bootstrap the permno→ticker mapping once from CRSP:

    from blind_spot.market_data import build_permno_ticker_map
    mapping = build_permno_ticker_map(permnos, as_of=date.today(), conn=wrds_conn)

Refresh quarterly or whenever coverage gaps appear.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import wrds

log = logging.getLogger(__name__)


# Suppress httpx URL logging at INFO level so api keys never appear in logs even
# if a caller accidentally puts them in URL params. Bearer-auth header is the
# safer path; this is belt-and-braces.
logging.getLogger("httpx").setLevel(logging.WARNING)


class _RateLimiter:
    """
    Thread-safe token bucket. Allows up to `rate_per_min` requests per rolling 60s window.

    Used by PolygonBarSource to honour Polygon's free-tier 5-req/min cap (or Starter+'s
    higher caps) without spraying 429s. Each `acquire()` blocks until a token is free.
    """

    def __init__(self, rate_per_min: float):
        self._interval = 60.0 / max(rate_per_min, 0.01)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_allowed - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_allowed = now + self._interval

BARS_COLUMNS = ["permno", "date", "open", "high", "low", "close", "volume", "ret", "shrout"]


@runtime_checkable
class BarSource(Protocol):
    """Daily-bar provider. Implementations must honour the point-in-time rule."""

    def get_bars(
        self, permnos: list[int], start: date, end: date
    ) -> pd.DataFrame:
        """Long-format daily bars for `permnos` over [start, end]. See module docstring."""
        ...

    def get_market(self, start: date, end: date) -> pd.Series:
        """Value-weighted market total-return series indexed by date over [start, end]."""
        ...


# ---------------------------------------------------------------------------
# WRDS / CRSP implementation — historical replay & eval
# ---------------------------------------------------------------------------

_DSF = "crsp_a_stock.dsf"
_DSI = "crsp_a_stock.dsi"


class WrdsBarSource:
    """
    BarSource backed by CRSP daily stock file (`dsf`) and index file (`dsi`).

    CRSP is survivorship-bias-free with proper delisting handling, which is why it stays
    the source for backtest/eval even after Polygon is wired for production.
    """

    def __init__(self, conn: "wrds.Connection"):
        self._conn = conn

    def get_bars(self, permnos: list[int], start: date, end: date) -> pd.DataFrame:
        if not permnos:
            return pd.DataFrame(columns=BARS_COLUMNS)
        sql = f"""
            SELECT permno, date,
                   openprc AS open,
                   askhi   AS high,
                   bidlo   AS low,
                   prc     AS prc,
                   vol     AS volume,
                   ret     AS ret,
                   shrout  AS shrout
            FROM {_DSF}
            WHERE permno = ANY(%(permnos)s)
              AND date >= %(start)s
              AND date <= %(end)s
        """
        df = self._conn.raw_sql(
            sql, params={"permnos": list(permnos), "start": start, "end": end}
        )
        if df.empty:
            return pd.DataFrame(columns=BARS_COLUMNS)

        df["date"]   = pd.to_datetime(df["date"])
        df["permno"] = df["permno"].astype(int)

        # CRSP prc is negative when it is a bid/ask midpoint (no trade) — sign is not price.
        df["close"] = pd.to_numeric(df["prc"], errors="coerce").abs()
        for col in ("open", "high", "low", "volume", "shrout"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # ret carries non-numeric missing codes (e.g. 'B', 'C') and sentinel values <= -1.
        df["ret"] = pd.to_numeric(df["ret"], errors="coerce")
        df.loc[df["ret"] <= -1, "ret"] = np.nan

        df = df[BARS_COLUMNS].sort_values(["permno", "date"]).reset_index(drop=True)
        log.info(
            "WrdsBarSource: %d bars for %d permnos over %s–%s",
            len(df), df["permno"].nunique(), start, end,
        )
        return df

    def get_market(self, start: date, end: date) -> pd.Series:
        sql = f"""
            SELECT date, vwretd
            FROM {_DSI}
            WHERE date >= %(start)s AND date <= %(end)s
              AND vwretd IS NOT NULL
            ORDER BY date
        """
        df = self._conn.raw_sql(sql, params={"start": start, "end": end})
        if df.empty:
            return pd.Series(dtype=float)
        df["date"]   = pd.to_datetime(df["date"])
        df["vwretd"] = pd.to_numeric(df["vwretd"], errors="coerce")
        return df.dropna(subset=["vwretd"]).set_index("date")["vwretd"]


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def default_window(as_of: date, lookback_days: int) -> tuple[date, date]:
    """
    [start, end] for a trailing window ending strictly before T0.

    end          = as_of - 1 calendar day  (last knowable close)
    start        = as_of - lookback_days * 1.55  (generous, to clear weekends/holidays)
    """
    end   = as_of - timedelta(days=1)
    start = as_of - timedelta(days=int(lookback_days * 1.55))
    return start, end


# ---------------------------------------------------------------------------
# Polygon.io implementation — production / near-current bars
# ---------------------------------------------------------------------------

_POLYGON_AGGS = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
_POLYGON_TICKER_DETAILS = "https://api.polygon.io/v3/reference/tickers/{ticker}"
_STOCKNAMES = "crsp.stocknames"


class PolygonBarSource:
    """
    BarSource backed by the Polygon.io REST API.

    Fetches adjusted daily OHLCV for the full universe — splits and dividends are
    folded into the adjusted close, so the price return equals the total return and is
    directly comparable to CRSP `ret`.

    Parameters
    ----------
    api_key          : Polygon API key (set POLYGON_API_KEY in .env)
    permno_to_ticker : {permno: ticker} mapping. Build once from CRSP stocknames via
                       `build_permno_ticker_map()`, then cache. Polygon uses current
                       tickers, so refresh quarterly.
    market_ticker    : Ticker used as the market proxy (default "SPY")
    max_workers      : Parallel fetch threads. Keep ≤ ceil(rate_per_min / 12) for the
                       free tier so the rate limiter doesn't queue half the requests.
                       Free tier: 1; Starter+: 8–16; Developer+: 50–100.
    rate_per_min     : Polygon plan ceiling. Free=5, Starter=100, Developer=10000,
                       Advanced=unlimited. The token-bucket limiter enforces this.
    timeout          : Per-request HTTP timeout in seconds
    shrout_map       : Optional {permno: shares_outstanding_thousands}. When absent,
                       `shrout` is NaN and market-cap bucketing degrades gracefully.

    The API key travels in the `Authorization: Bearer` header (Polygon's recommended
    method), not as a URL `apiKey=` query param — so the key never appears in HTTP
    access logs, transcripts, or 429 error messages that quote the URL.
    """

    def __init__(
        self,
        api_key: str,
        permno_to_ticker: dict[int, str],
        market_ticker: str = "SPY",
        max_workers: int = 1,
        rate_per_min: float = 5,
        timeout: float = 30.0,
        shrout_map: dict[int, float] | None = None,
    ):
        self._api_key = api_key
        self._permno_to_ticker = {k: v.upper() for k, v in permno_to_ticker.items()}
        self._ticker_to_permno = {v.upper(): k for k, v in self._permno_to_ticker.items()}
        self._market_ticker = market_ticker.upper()
        self._max_workers = max_workers
        self._timeout = timeout
        self._shrout_map = shrout_map or {}
        self._limiter = _RateLimiter(rate_per_min)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_ticker_bars(
        self, ticker: str, start: date, end: date
    ) -> list[dict]:
        """Fetch all adjusted daily bars for one ticker, following pagination."""
        import httpx
        url = _POLYGON_AGGS.format(ticker=ticker, start=start, end=end)
        params: dict = {"adjusted": "true", "sort": "asc", "limit": 5000}
        results = []
        with httpx.Client(timeout=self._timeout, headers=self._headers) as client:
            while url:
                self._limiter.acquire()
                r = client.get(url, params=params)

                # Honour Polygon's Retry-After on 429; otherwise back off ~15s and retry once
                retries = 0
                while r.status_code == 429 and retries < 3:
                    retry_after = float(r.headers.get("Retry-After", "15"))
                    log.warning(
                        "PolygonBarSource: 429 on %s, sleeping %.0fs (retry %d/3)",
                        ticker, retry_after, retries + 1,
                    )
                    time.sleep(retry_after)
                    self._limiter.acquire()
                    r = client.get(url, params=params)
                    retries += 1

                r.raise_for_status()
                body = r.json()
                results.extend(body.get("results") or [])
                # Polygon paginates via next_url; rare for daily bars over ≤252 days
                url = body.get("next_url", "")
                params = {}   # next_url already carries the cursor
        return results

    @staticmethod
    def _polygon_to_rows(
        ticker: str,
        permno: int,
        raw: list[dict],
        shrout: float | None,
    ) -> list[dict]:
        """
        Convert Polygon agg dicts to BARS_COLUMNS rows, computing `ret` from
        consecutive adjusted closes (= total return since prices are adjusted).
        """
        if not raw:
            return []
        rows = []
        prev_close: float | None = None
        for bar in raw:
            ts_ms = bar.get("t", 0)
            bar_date = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_localize(None).normalize()
            close = float(bar.get("c", 0)) or None
            ret: float | None = None
            if close is not None and prev_close is not None and prev_close > 0:
                ret = (close - prev_close) / prev_close
            rows.append({
                "permno":  permno,
                "date":    bar_date,
                "open":    float(bar.get("o")) if bar.get("o") else None,
                "high":    float(bar.get("h")) if bar.get("h") else None,
                "low":     float(bar.get("l")) if bar.get("l") else None,
                "close":   close,
                "volume":  float(bar.get("v")) if bar.get("v") else None,
                "ret":     ret,
                "shrout":  shrout,
            })
            prev_close = close
        return rows

    # ------------------------------------------------------------------
    # BarSource protocol
    # ------------------------------------------------------------------

    def get_bars(self, permnos: list[int], start: date, end: date) -> pd.DataFrame:
        if not permnos:
            return pd.DataFrame(columns=BARS_COLUMNS)

        # Map permnos → tickers; log any gaps
        tasks: list[tuple[int, str]] = []
        for p in permnos:
            t = self._permno_to_ticker.get(p)
            if t:
                tasks.append((p, t))
            else:
                log.debug("PolygonBarSource: no ticker mapping for permno %d", p)
        if not tasks:
            log.warning("PolygonBarSource: no ticker mappings found for %d permnos", len(permnos))
            return pd.DataFrame(columns=BARS_COLUMNS)

        log.info(
            "PolygonBarSource: fetching %d tickers over %s–%s (%d threads)",
            len(tasks), start, end, self._max_workers,
        )
        all_rows: list[dict] = []
        errors = 0

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_task = {
                pool.submit(self._fetch_ticker_bars, ticker, start, end): (permno, ticker)
                for permno, ticker in tasks
            }
            for future in as_completed(future_to_task):
                permno, ticker = future_to_task[future]
                try:
                    raw = future.result()
                    shrout = self._shrout_map.get(permno)
                    all_rows.extend(self._polygon_to_rows(ticker, permno, raw, shrout))
                except Exception as exc:
                    log.warning("PolygonBarSource: %s failed: %s", ticker, exc)
                    errors += 1

        if errors:
            log.warning("PolygonBarSource: %d/%d tickers failed", errors, len(tasks))

        if not all_rows:
            return pd.DataFrame(columns=BARS_COLUMNS)

        df = pd.DataFrame(all_rows)[BARS_COLUMNS]
        df = df.sort_values(["permno", "date"]).reset_index(drop=True)
        log.info(
            "PolygonBarSource: %d bars for %d permnos (%d unmapped skipped)",
            len(df), df["permno"].nunique(), len(permnos) - len(tasks),
        )
        return df

    def get_market(self, start: date, end: date) -> pd.Series:
        """Fetch SPY (or `market_ticker`) adjusted daily return series."""
        try:
            raw = self._fetch_ticker_bars(self._market_ticker, start, end)
        except Exception as exc:
            log.error("PolygonBarSource: market fetch failed: %s", exc)
            return pd.Series(dtype=float)

        rows = self._polygon_to_rows(
            self._market_ticker, 0, raw, shrout=None
        )
        if not rows:
            return pd.Series(dtype=float)

        df = pd.DataFrame(rows)
        df = df.dropna(subset=["ret"]).set_index("date")
        return df["ret"].sort_index()


# ---------------------------------------------------------------------------
# CRSP bootstrap for permno → ticker mapping
# ---------------------------------------------------------------------------

def build_permno_ticker_map(
    permnos: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, str]:
    """
    Return {permno: ticker} from CRSP stocknames, point-in-time as of `as_of`.

    Use this once to bootstrap the mapping for PolygonBarSource, then cache it.
    Refresh quarterly — tickers change on renames, spin-offs, and delistings.

    Permnos without a matching CRSP record (e.g. very recent IPOs not yet in CRSP)
    are absent from the result; PolygonBarSource skips them and logs a debug message.
    """
    if not permnos:
        return {}
    sql = f"""
        SELECT permno, ticker
        FROM {_STOCKNAMES}
        WHERE permno = ANY(%(permnos)s)
          AND namedt <= %(as_of)s
          AND (nameenddt >= %(as_of)s OR nameenddt IS NULL)
          AND ticker IS NOT NULL
    """
    df = conn.raw_sql(sql, params={"permnos": list(permnos), "as_of": as_of})
    if df.empty:
        return {}
    df["permno"] = df["permno"].astype(int)
    df = df.drop_duplicates("permno", keep="first")
    mapping = {int(r["permno"]): str(r["ticker"]).strip() for _, r in df.iterrows()}
    log.info(
        "build_permno_ticker_map: %d/%d permnos resolved to tickers",
        len(mapping), len(permnos),
    )
    return mapping
