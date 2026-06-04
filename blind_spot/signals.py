"""
Attention signals for Blind Spot Lane B.

Lane B's job is "market-lit": surface names the market is actively moving on, so the
analyst's attention reproduces faster. These signals measure that directly from daily
bars — no options required, so the full universe is scored and `as_of` is bounded only
by how current the bar source is.

Every signal is a pure function of a long-format bars frame (see market_data.BARS_COLUMNS)
and returns {permno: float}. All are null-tolerant: a permno with insufficient history is
simply absent from the result, never an error. Higher = more market-lit.

Signals
-------
realized_vol_spike  short-window realized vol ÷ long-window baseline. "It's moving now,
                    relative to its own normal." Ratio, ~1.0 = calm, >1.5 = elevated.
abnormal_volume     latest dollar volume z-scored against its trailing distribution.
                    The cleanest attention proxy there is.
dislocation         |idiosyncratic return| on the latest bar — the firm-specific move left
                    after stripping the market via per-stock OLS beta. Reuses the residual
                    construction from comovement_loader so the two layers agree.
gap                 |overnight gap| = |open − prev_close| / prev_close on the latest bar.

These are intentionally simple and monotone; the composite blend and weighting live in
candidate_generator so the signal math here stays easy to reason about and test.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Trading days needed before a signal is trustworthy
_MIN_VOL_OBS    = 15
_MIN_VOLUME_OBS = 20
_MIN_BETA_OBS   = 20


def _pivot(bars: pd.DataFrame, value: str) -> pd.DataFrame:
    """Long bars → wide [date × permno] frame of `value`, sorted by date."""
    if bars.empty or value not in bars.columns:
        return pd.DataFrame()
    wide = bars.pivot_table(index="date", columns="permno", values=value, aggfunc="last")
    return wide.sort_index()


def realized_vol_spike(
    bars: pd.DataFrame, short: int = 10, long: int = 60
) -> dict[int, float]:
    """
    short-window realized vol ÷ long-window baseline, per permno.

    A ratio > 1 means the stock is more volatile than its own recent norm — the market is
    moving on it. Requires at least `_MIN_VOL_OBS` returns in the long window.
    """
    rets = _pivot(bars, "ret")
    if rets.empty:
        return {}
    out: dict[int, float] = {}
    for permno in rets.columns:
        r = rets[permno].dropna()
        if len(r) < _MIN_VOL_OBS:
            continue
        long_w  = r.tail(long)
        short_w = r.tail(short)
        base = long_w.std()
        if not np.isfinite(base) or base <= 0 or len(short_w) < 2:
            continue
        out[int(permno)] = float(short_w.std() / base)
    return out


def abnormal_volume(bars: pd.DataFrame, window: int = 60) -> dict[int, float]:
    """
    z-score of the latest dollar volume against its trailing-`window` distribution.

    Dollar volume = close × volume, which normalises across price levels. Absent or flat
    history → permno omitted.
    """
    if bars.empty:
        return {}
    b = bars.copy()
    b["dollar_vol"] = b["close"] * b["volume"]
    dv = _pivot(b, "dollar_vol")
    if dv.empty:
        return {}
    out: dict[int, float] = {}
    for permno in dv.columns:
        s = dv[permno].dropna()
        if len(s) < _MIN_VOLUME_OBS:
            continue
        latest = s.iloc[-1]
        hist   = s.iloc[:-1]
        mu, sd = hist.mean(), hist.std()
        if not np.isfinite(sd) or sd <= 0:
            continue
        out[int(permno)] = float((latest - mu) / sd)
    return out


def dislocation(
    bars: pd.DataFrame, market: pd.Series, beta_window: int = 60
) -> dict[int, float]:
    """
    |idiosyncratic return| on the latest shared bar, per permno.

    Per-stock OLS beta vs the market is estimated on the trailing `beta_window` (unbiased,
    complete-case), then the residual of the latest return is |ret − β·mkt|. This is the
    same market-stripping the co-movement loader uses, so an "it's dislocating" signal here
    is consistent with a :COMOVES_WITH edge there.
    """
    rets = _pivot(bars, "ret")
    if rets.empty or market.empty:
        return {}

    mkt = market.reindex(rets.index).astype(float)
    valid_dates = mkt.dropna().index
    rets = rets.reindex(valid_dates)
    mkt  = mkt.reindex(valid_dates)
    if len(valid_dates) < _MIN_BETA_OBS:
        return {}

    win_dates = valid_dates[-beta_window:]
    R = rets.reindex(win_dates)
    M = mkt.reindex(win_dates).to_numpy(dtype=float)
    latest_date = valid_dates[-1]

    out: dict[int, float] = {}
    for permno in R.columns:
        r = R[permno].to_numpy(dtype=float)
        mask = ~np.isnan(r)
        if mask.sum() < _MIN_BETA_OBS:
            continue
        m = M[mask]
        denom = float(np.sum(m * m))
        if denom <= 0:
            continue
        beta = float(np.sum(m * r[mask]) / denom)

        latest_ret = rets.at[latest_date, permno]
        latest_mkt = mkt.at[latest_date]
        if pd.isna(latest_ret) or pd.isna(latest_mkt):
            continue
        out[int(permno)] = float(abs(latest_ret - beta * latest_mkt))
    return out


def gap(bars: pd.DataFrame) -> dict[int, float]:
    """
    |overnight gap| = |open − prev_close| / prev_close on the latest bar, per permno.

    Requires a non-null open on the latest date and a positive prior close. Permnos without
    both are omitted (CRSP openprc is frequently null on illiquid names).
    """
    if bars.empty:
        return {}
    out: dict[int, float] = {}
    for permno, grp in bars.sort_values("date").groupby("permno"):
        if len(grp) < 2:
            continue
        last = grp.iloc[-1]
        prev = grp.iloc[-2]
        prev_close = prev["close"]
        open_px    = last["open"]
        if pd.isna(open_px) or pd.isna(prev_close) or prev_close <= 0:
            continue
        out[int(permno)] = float(abs(open_px - prev_close) / prev_close)
    return out


def zscore_map(raw: dict[int, float]) -> dict[int, float]:
    """
    Cross-sectionally standardise a {permno: value} signal to mean 0, sd 1.

    Used to put heterogeneous signals (a vol *ratio*, a volume *z*, a *return* magnitude)
    on a common scale before blending. A degenerate (≤1 value or zero-variance) signal maps
    everything to 0.0 — i.e. it contributes nothing rather than dominating.
    """
    if len(raw) < 2:
        return {k: 0.0 for k in raw}
    vals = np.array(list(raw.values()), dtype=float)
    mu, sd = vals.mean(), vals.std()
    if not np.isfinite(sd) or sd <= 0:
        return {k: 0.0 for k in raw}
    return {k: float((v - mu) / sd) for k, v in raw.items()}
