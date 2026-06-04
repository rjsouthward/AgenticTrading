"""
Offline unit tests for candidate_generator.py — the refactored, bar-driven Lane B.

The headline test is `test_generate_runs_with_zero_optionmetrics`: a full candidate list
is produced from bars alone, with no WRDS/OM connection at all. That is the decoupling the
refactor exists to deliver.
"""
from datetime import date, datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from blind_spot.candidate_generator import Candidate, generate, _compute_salience
from blind_spot.tests.test_signals import make_bars

AS_OF = date(2024, 6, 14)


class FakeBarSource:
    """In-memory BarSource for deterministic offline tests."""

    def __init__(self, bars: pd.DataFrame, market: pd.Series):
        self._bars = bars
        self._market = market

    def get_bars(self, permnos, start, end):
        return self._bars[self._bars["permno"].isin(permnos)].copy()

    def get_market(self, start, end):
        return self._market


def _lit_vs_calm_source(seed=0):
    """permno 1 is clearly market-lit (vol + volume + gap spike); 2..5 are calm."""
    cfg = {
        1: {"ret_scale": 0.01, "last_vol_mult": 10.0, "last_open_gap": 0.08, "last_ret": 0.12},
        2: {"ret_scale": 0.01},
        3: {"ret_scale": 0.01},
        4: {"ret_scale": 0.01},
        5: {"ret_scale": 0.01},
    }
    bars = make_bars(cfg, n_days=70, seed=seed)
    # Elevate permno 1's recent realized vol too
    mask = (bars["permno"] == 1)
    idx = bars[mask].sort_values("date").index[-10:]
    rng = np.random.default_rng(seed + 1)
    bars.loc[idx, "ret"] = rng.normal(0, 0.05, len(idx))
    market = pd.Series(0.0, index=sorted(bars["date"].unique()))
    return FakeBarSource(bars, market)


def _universe(n=5):
    return [f"permno:{i}" for i in range(1, n + 1)]


# ---------------------------------------------------------------------------
# The decoupling test
# ---------------------------------------------------------------------------

def test_generate_runs_with_zero_optionmetrics():
    src = _lit_vs_calm_source()
    candidates = generate(
        universe=_universe(),
        as_of=AS_OF,
        bar_source=src,
        wrds_conn=None,             # no WRDS at all
        enrich_with_options=False,
    )
    assert len(candidates) == 5
    assert all(isinstance(c, Candidate) for c in candidates)
    # Every name is bar-backed → coverage True, but none has options
    assert all(c.coverage for c in candidates)
    assert all(not c.has_options for c in candidates)
    assert all(c.measure == "attention" for c in candidates)


def test_market_lit_name_ranks_first():
    src = _lit_vs_calm_source()
    candidates = generate(_universe(), AS_OF, bar_source=src, wrds_conn=None,
                          enrich_with_options=False)
    # permno 1 is the only lit name; it should top the salience ranking
    assert candidates[0].canonical_id == "permno:1"
    assert candidates[0].attention > candidates[-1].attention


def test_generate_is_deterministic():
    c1 = generate(_universe(), AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None,
                  enrich_with_options=False)
    c2 = generate(_universe(), AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None,
                  enrich_with_options=False)
    assert [(c.canonical_id, round(c.salience, 9), round(c.attention, 9)) for c in c1] == \
           [(c.canonical_id, round(c.salience, 9), round(c.attention, 9)) for c in c2]


def test_sorted_descending_by_salience():
    cands = generate(_universe(), AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None,
                     enrich_with_options=False)
    sals = [c.salience for c in cands]
    assert sals == sorted(sals, reverse=True)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_universe():
    assert generate([], AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None) == []


def test_no_valid_permnos():
    src = _lit_vs_calm_source()
    assert generate(["ticker:NVDA", "garbage"], AS_OF, bar_source=src, wrds_conn=None) == []


def test_empty_bars_returns_empty():
    empty = FakeBarSource(pd.DataFrame(columns=["permno", "date", "ret", "close", "volume",
                                                "open", "high", "low", "shrout"]),
                          pd.Series(dtype=float))
    assert generate(_universe(), AS_OF, bar_source=empty, wrds_conn=None) == []


def test_requires_bar_source_or_conn():
    with pytest.raises(ValueError):
        generate(_universe(), AS_OF, bar_source=None, wrds_conn=None)


def test_components_recorded():
    cands = generate(_universe(), AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None,
                     enrich_with_options=False)
    c = cands[0]
    assert c.components is not None
    assert set(c.components) >= {"realized_vol_spike", "abnormal_volume", "dislocation", "gap"}


def test_custom_weights_zero_flattens_composite():
    src = _lit_vs_calm_source()
    flat = generate(_universe(), AS_OF, bar_source=src, wrds_conn=None,
                    enrich_with_options=False,
                    weights={"realized_vol_spike": 0, "abnormal_volume": 0,
                             "dislocation": 0, "gap": 0, "iv": 0})
    assert all(abs(c.attention) < 1e-12 for c in flat)


def test_as_of_is_set_on_candidates():
    cands = generate(_universe(), AS_OF, bar_source=_lit_vs_calm_source(), wrds_conn=None,
                     enrich_with_options=False)
    assert all(c.as_of == datetime(2024, 6, 14, 0, 0, 0) for c in cands)


# ---------------------------------------------------------------------------
# _compute_salience direct
# ---------------------------------------------------------------------------

def test_compute_salience_ranks_within_bucket():
    records = [
        {"canonical_id": "permno:1", "composite": 3.0, "siccd": 3500, "mktcap": 1e9},
        {"canonical_id": "permno:2", "composite": 1.0, "siccd": 3500, "mktcap": 1e9},
        {"canonical_id": "permno:3", "composite": 2.0, "siccd": 3500, "mktcap": 1e9},
    ]
    out = _compute_salience(records)
    assert out[0]["canonical_id"] == "permno:1"
    assert out[-1]["canonical_id"] == "permno:2"


def test_compute_salience_empty():
    assert _compute_salience([]) == []
