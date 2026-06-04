"""
Offline unit tests for signals.py — pure functions over synthetic bars, no credentials.
"""
import numpy as np
import pandas as pd
import pytest

from blind_spot import signals


def make_bars(per_permno: dict[int, dict], n_days: int = 70, seed: int = 0) -> pd.DataFrame:
    """
    Build a long-format bars frame.

    per_permno: {permno: {"ret_scale":, "vol_base":, "last_ret":, "last_vol_mult":,
                          "last_open_gap":}}
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B")
    rows = []
    for permno, cfg in per_permno.items():
        rets = rng.normal(0, cfg.get("ret_scale", 0.01), n_days)
        if "last_ret" in cfg:
            rets[-1] = cfg["last_ret"]
        vols = np.full(n_days, cfg.get("vol_base", 1_000_000.0))
        vols[-1] *= cfg.get("last_vol_mult", 1.0)
        close = 100.0 * np.cumprod(1 + rets)
        prev_close = np.concatenate([[100.0], close[:-1]])
        opens = prev_close.copy()
        if "last_open_gap" in cfg:
            opens[-1] = prev_close[-1] * (1 + cfg["last_open_gap"])
        for i, d in enumerate(dates):
            rows.append({
                "permno": permno, "date": d,
                "open": opens[i], "high": close[i] * 1.01, "low": close[i] * 0.99,
                "close": close[i], "volume": vols[i], "ret": rets[i], "shrout": 50_000.0,
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# zscore_map
# ---------------------------------------------------------------------------

def test_zscore_map_standardises():
    out = signals.zscore_map({1: 0.0, 2: 1.0, 3: 2.0})
    vals = np.array(list(out.values()))
    assert abs(vals.mean()) < 1e-9
    assert abs(vals.std() - 1.0) < 1e-9


def test_zscore_map_degenerate_is_zero():
    assert signals.zscore_map({1: 5.0}) == {1: 0.0}
    assert signals.zscore_map({1: 5.0, 2: 5.0}) == {1: 0.0, 2: 0.0}


def test_zscore_map_empty():
    assert signals.zscore_map({}) == {}


# ---------------------------------------------------------------------------
# realized_vol_spike
# ---------------------------------------------------------------------------

def test_realized_vol_spike_detects_elevation():
    bars = make_bars({
        1: {"ret_scale": 0.01},   # calm
        2: {"ret_scale": 0.01},
    })
    # Inject a high-vol recent stretch into permno 2's last 10 returns
    mask = (bars["permno"] == 2)
    idx = bars[mask].sort_values("date").index[-10:]
    rng = np.random.default_rng(1)
    bars.loc[idx, "ret"] = rng.normal(0, 0.06, len(idx))
    out = signals.realized_vol_spike(bars, short=10, long=60)
    assert out[2] > out[1]
    assert out[2] > 1.3   # recent vol clearly above its own baseline


def test_realized_vol_spike_skips_short_history():
    bars = make_bars({1: {"ret_scale": 0.01}}, n_days=10)  # < _MIN_VOL_OBS
    assert signals.realized_vol_spike(bars) == {}


# ---------------------------------------------------------------------------
# abnormal_volume
# ---------------------------------------------------------------------------

def test_abnormal_volume_flags_spike():
    bars = make_bars({
        1: {"vol_base": 1_000_000.0, "last_vol_mult": 8.0},  # volume explosion today
        2: {"vol_base": 1_000_000.0, "last_vol_mult": 1.0},  # steady
    })
    out = signals.abnormal_volume(bars, window=60)
    assert out[1] > out[2]
    assert out[1] > 3.0   # many sigma above its own trailing mean


def test_abnormal_volume_skips_short_history():
    bars = make_bars({1: {}}, n_days=15)  # < _MIN_VOLUME_OBS
    assert signals.abnormal_volume(bars) == {}


# ---------------------------------------------------------------------------
# dislocation
# ---------------------------------------------------------------------------

def test_dislocation_isolates_idiosyncratic_move():
    bars = make_bars({
        1: {"ret_scale": 0.01, "last_ret": 0.15},   # big firm-specific jump today
        2: {"ret_scale": 0.01, "last_ret": 0.005},  # quiet
    })
    dates = sorted(bars["date"].unique())
    rng = np.random.default_rng(7)
    market = pd.Series(rng.normal(0, 0.005, len(dates)), index=dates)  # non-flat market
    out = signals.dislocation(bars, market, beta_window=60)
    assert out[1] > out[2]
    assert out[1] > 0.10


def test_dislocation_empty_market():
    bars = make_bars({1: {}})
    assert signals.dislocation(bars, pd.Series(dtype=float)) == {}


# ---------------------------------------------------------------------------
# gap
# ---------------------------------------------------------------------------

def test_gap_detects_overnight_move():
    bars = make_bars({
        1: {"last_open_gap": 0.07},   # opens 7% from prev close
        2: {"last_open_gap": 0.0},
    })
    out = signals.gap(bars)
    assert out[1] > out[2]
    assert out[1] > 0.05


def test_gap_requires_open_and_prev_close():
    bars = make_bars({1: {}})
    bars.loc[bars.index[-1], "open"] = np.nan   # null open on last bar
    # permno 1's last bar has no open → omitted
    assert 1 not in signals.gap(bars)
