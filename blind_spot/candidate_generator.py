"""
Lane B candidate generator for Blind Spot.

Lane B is the analyst-independent "market-lit" ranking: it scans the full universe and
surfaces names the market is actively moving on, so the analyst's attention reproduces
faster. The salience signal is built from daily bars (realized-vol spike, abnormal volume,
idiosyncratic dislocation, overnight gap) — see `signals.py`. OptionMetrics IV/straddle is
an *optional enrichment* term, not a gate: names without options coverage are still scored
and ranked on the bar-based signals alone.

This is the key decoupling. The old design routed the whole universe through the
OptionMetrics permno↔secid link table (`opcrsphist`), so:
  - no OM data for a date  →  zero candidates (the `as_of` ceiling)
  - no OM coverage for a name  →  silently dropped from the universe
Now the universe is whatever has daily bars from the `BarSource`, and `as_of` is bounded
only by how current that source is (CRSP for backtest, Polygon for production).

Salience
--------
Composite attention score = weighted sum of cross-sectionally z-scored signals, ranked
within a (SIC major-division × market-cap quintile) bucket and normalised to (0, 1]. The
bucket kills the small-cap/high-vol confound. IV rank folds in as one more z-scored term
where options data exists.

Usage
-----
    from blind_spot.candidate_generator import generate
    from blind_spot.market_data import WrdsBarSource

    candidates = generate(
        universe=["permno:14593", "permno:10000"],
        as_of=date(2024, 6, 14),
        wrds_conn=conn,                     # backtest: CRSP bars + optional OM enrichment
        # bar_source=PolygonBarSource(...), # production: live bars (Phase 2)
    )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from blind_spot import signals
from blind_spot.market_data import BarSource, WrdsBarSource, default_window

if TYPE_CHECKING:
    import wrds

log = logging.getLogger(__name__)

CanonicalId = str

# --- OptionMetrics tables (enrichment path only) ---------------------------
_OM_LINK    = "wrdsapps_link_crsp_optionm.opcrsphist"
_VSURFD     = "optionm_all.vsurfd{year}"
_OPPRCD     = "optionm_all.opprcd{year}"
_STOCKNAMES = "crsp.stocknames"

_ATM_DELTA      = 50    # vsurfd integer-delta scale; 50 = 0.50 delta (call ATM)
_IV_TENOR       = 30    # standardised DTE for IV rank
_MAX_LINK_SCORE = 6     # opcrsphist: accept links up to score=6 (1=exact, higher=looser)
_MIN_IV_OBS     = 10    # minimum IV history obs to compute iv_rank

# --- Attention-signal parameters -------------------------------------------
_RV_SHORT     = 10
_RV_LONG      = 60
_VOL_WINDOW   = 60
_BETA_WINDOW  = 60
_MIN_LOOKBACK_DAYS = 90   # ensure enough history for the long baselines above

# Composite blend. Tuned conservatively: the three core "it's moving" signals carry equal
# weight; gap and IV are lighter nudges. Override via the `weights` arg.
_DEFAULT_WEIGHTS = {
    "realized_vol_spike": 1.0,
    "abnormal_volume":    1.0,
    "dislocation":        1.0,
    "gap":                0.5,
    "iv":                 0.5,
}


@dataclass(frozen=True)
class Candidate:
    canonical_id: CanonicalId
    implied_move: float | None       # straddle/S at catalyst expiry; None if no named event
    iv_rank: float | None            # trailing IV position [0,1]; None if no options data
    measure: str                     # "straddle" | "attention"
    salience: float                  # within-bucket normalised rank of the composite, (0,1]
    coverage: bool                   # True = backed by market-data bars (a real candidate)
    as_of: datetime                  # T0; every field knowable strictly before T0
    attention: float = 0.0           # composite z-score (pre-bucket-rank)
    has_options: bool = False        # True = OptionMetrics IV available for this name
    components: dict[str, float] | None = None   # per-signal z-scores, for logging / RL


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def _parse_permno(cid: CanonicalId) -> int | None:
    if cid and cid.startswith("permno:"):
        try:
            return int(cid.split(":")[1])
        except ValueError:
            pass
    return None


def _market_caps_from_bars(bars: pd.DataFrame) -> dict[int, float]:
    """{permno: market_cap_usd} from the latest bar (close × shrout × 1000)."""
    if bars.empty:
        return {}
    latest = (
        bars.dropna(subset=["close", "shrout"])
        .sort_values("date")
        .groupby("permno")
        .tail(1)
    )
    out: dict[int, float] = {}
    for _, r in latest.iterrows():
        if r["close"] > 0 and r["shrout"] > 0:
            out[int(r["permno"])] = float(r["close"]) * float(r["shrout"]) * 1_000
    return out


def _pull_sic_codes(
    permnos: list[int], as_of: date, conn: "wrds.Connection"
) -> dict[int, int]:
    """{permno: siccd} from CRSP stocknames, point-in-time."""
    if not permnos:
        return {}
    sql = f"""
        SELECT permno, siccd
        FROM {_STOCKNAMES}
        WHERE permno = ANY(%(permnos)s)
          AND namedt <= %(as_of)s
          AND (nameenddt >= %(as_of)s OR nameenddt IS NULL)
    """
    df = conn.raw_sql(sql, params={"permnos": list(permnos), "as_of": as_of})
    if df.empty:
        return {}
    df["permno"] = df["permno"].astype(int)
    df["siccd"]  = pd.to_numeric(df["siccd"], errors="coerce").fillna(0).astype(int)
    df = df.drop_duplicates("permno", keep="first")
    return {int(r["permno"]): int(r["siccd"]) for _, r in df.iterrows()}


# ---------------------------------------------------------------------------
# OptionMetrics enrichment (optional) — adds an IV term, never gates the universe
# ---------------------------------------------------------------------------

def _vsurfd_table_years(window_start: date, window_end: date) -> list[int]:
    return sorted({window_start.year, window_end.year})


def _resolve_universe_secids(
    permnos: list[int], as_of: date, conn: "wrds.Connection"
) -> dict[int, int]:
    """{permno: secid} for permnos with a valid OM link active on as_of."""
    if not permnos:
        return {}
    sql = f"""
        SELECT permno, secid, score
        FROM {_OM_LINK}
        WHERE permno = ANY(%(permnos)s)
          AND score <= %(max_score)s
          AND (sdate IS NULL OR sdate <= %(as_of)s)
          AND (edate IS NULL OR edate >= %(as_of)s)
    """
    df = conn.raw_sql(sql, params={"permnos": list(permnos), "max_score": _MAX_LINK_SCORE, "as_of": as_of})
    if df.empty:
        return {}
    df = df.dropna(subset=["permno", "secid"])
    df["permno"] = df["permno"].astype(int)
    df["secid"]  = df["secid"].astype(int)
    df = df.sort_values("score").drop_duplicates("permno", keep="first")
    result = dict(zip(df["permno"], df["secid"]))
    log.info("candidate_generator: %d/%d permnos resolved to OM secids (enrichment)", len(result), len(permnos))
    return result


def _pull_atm_iv(secids: list[int], as_of: date, conn: "wrds.Connection") -> dict[int, float]:
    """{secid: impl_volatility} from vsurfd at delta=50, days=30, date=as_of."""
    if not secids:
        return {}
    sql = f"""
        SELECT secid, impl_volatility
        FROM {_VSURFD.format(year=as_of.year)}
        WHERE secid = ANY(%(secids)s)
          AND date = %(as_of)s
          AND days = %(tenor)s
          AND delta = %(delta)s
          AND impl_volatility IS NOT NULL
          AND impl_volatility > 0
    """
    df = conn.raw_sql(sql, params={"secids": list(secids), "as_of": as_of, "tenor": _IV_TENOR, "delta": _ATM_DELTA})
    if df.empty:
        return {}
    df["secid"] = df["secid"].astype(int)
    df["impl_volatility"] = pd.to_numeric(df["impl_volatility"], errors="coerce")
    df = df.dropna(subset=["impl_volatility"])
    return {int(r["secid"]): float(r["impl_volatility"]) for _, r in df.iterrows()}


def _pull_iv_history(
    secids: list[int], as_of: date, window_days: int, conn: "wrds.Connection"
) -> dict[int, tuple[float, float]]:
    """{secid: (min_iv, max_iv)} over the trailing window ending strictly before as_of."""
    if not secids:
        return {}
    window_end   = as_of - timedelta(days=1)
    window_start = as_of - timedelta(days=int(window_days * 1.55))
    years = _vsurfd_table_years(window_start, window_end)

    dfs: list[pd.DataFrame] = []
    for year in years:
        sql = f"""
            SELECT secid, date, impl_volatility
            FROM {_VSURFD.format(year=year)}
            WHERE secid = ANY(%(secids)s)
              AND date >= %(start)s
              AND date <= %(end)s
              AND days = %(tenor)s
              AND delta = %(delta)s
              AND impl_volatility IS NOT NULL
              AND impl_volatility > 0
        """
        df = conn.raw_sql(sql, params={"secids": list(secids), "start": window_start, "end": window_end, "tenor": _IV_TENOR, "delta": _ATM_DELTA})
        if not df.empty:
            dfs.append(df)
    if not dfs:
        return {}

    combined = pd.concat(dfs, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"])
    combined["impl_volatility"] = pd.to_numeric(combined["impl_volatility"], errors="coerce")
    combined["secid"] = combined["secid"].astype(int)
    combined = combined.dropna(subset=["impl_volatility"]).sort_values("date")
    combined = combined.groupby("secid", group_keys=False).tail(window_days)

    result: dict[int, tuple[float, float]] = {}
    for secid_val, grp in combined.groupby("secid"):
        ivs = grp["impl_volatility"].values.astype(float)
        if len(ivs) >= _MIN_IV_OBS:
            result[int(secid_val)] = (float(ivs.min()), float(ivs.max()))
    return result


def _pull_atm_straddle(
    secid_to_catalyst: dict[int, date], as_of: date, conn: "wrds.Connection"
) -> dict[int, float]:
    """{secid: raw_straddle_price} for the first OM expiry strictly after each catalyst."""
    if not secid_to_catalyst:
        return {}
    secids = list(secid_to_catalyst.keys())
    sql = f"""
        SELECT secid, exdate, cp_flag, best_bid, best_offer, delta, open_interest
        FROM {_OPPRCD.format(year=as_of.year)}
        WHERE secid = ANY(%(secids)s)
          AND date = %(as_of)s
          AND ABS(delta) >= 0.35
          AND ABS(delta) <= 0.65
          AND best_bid IS NOT NULL
          AND best_offer IS NOT NULL
          AND best_bid >= 0
          AND open_interest > 0
    """
    df = conn.raw_sql(sql, params={"secids": secids, "as_of": as_of})
    if df.empty:
        return {}
    df["exdate"]    = pd.to_datetime(df["exdate"]).dt.date
    df["mid"]       = (df["best_bid"].astype(float) + df["best_offer"].astype(float)) / 2.0
    df["abs_delta"] = df["delta"].abs()
    df["secid"]     = df["secid"].astype(int)

    straddle_prices: dict[int, float] = {}
    for secid, catalyst_date in secid_to_catalyst.items():
        sub = df[df["secid"] == secid]
        if sub.empty:
            continue
        valid_expiries = sub[sub["exdate"] > catalyst_date]["exdate"].unique()
        if len(valid_expiries) == 0:
            continue
        target_expiry = sorted(valid_expiries)[0]
        expiry_opts = sub[sub["exdate"] == target_expiry]
        calls = expiry_opts[expiry_opts["cp_flag"] == "C"].sort_values("abs_delta")
        puts  = expiry_opts[expiry_opts["cp_flag"] == "P"].sort_values("abs_delta")
        if calls.empty or puts.empty:
            continue
        call_mid = float(calls.iloc[0]["mid"])
        put_mid  = float(puts.iloc[0]["mid"])
        if call_mid > 0 and put_mid > 0:
            straddle_prices[secid] = call_mid + put_mid
    return straddle_prices


def _enrich_options(
    eligible_permnos: list[int],
    events: dict[CanonicalId, date],
    as_of: date,
    window_days: int,
    bars: pd.DataFrame,
    conn: "wrds.Connection",
) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    """
    Optional OptionMetrics enrichment over the bar-eligible universe.

    Returns (iv_rank_map, iv_level_map, straddle_move) keyed by permno. Any failure to
    resolve OM data returns empty maps — the caller proceeds on bar signals alone.
    """
    permno_to_secid = _resolve_universe_secids(eligible_permnos, as_of, conn)
    if not permno_to_secid:
        return {}, {}, {}
    secid_to_permno = {v: k for k, v in permno_to_secid.items()}
    secids = list(permno_to_secid.values())

    atm_iv  = _pull_atm_iv(secids, as_of, conn)
    iv_hist = _pull_iv_history(secids, as_of, window_days, conn)

    iv_rank_map: dict[int, float] = {}
    iv_level_map: dict[int, float] = {}
    for secid, p in secid_to_permno.items():
        iv_today = atm_iv.get(secid)
        if iv_today is None:
            continue
        iv_level_map[p] = iv_today
        hist = iv_hist.get(secid)
        if hist is not None:
            lo, hi = hist
            iv_rank_map[p] = 0.5 if hi <= lo else float(np.clip((iv_today - lo) / (hi - lo), 0.0, 1.0))

    # Straddle implied move for named catalysts; underlying S taken from CRSP bars
    secid_to_catalyst: dict[int, date] = {}
    for cid, cat_date in events.items():
        pp = _parse_permno(cid)
        if pp is not None and pp in permno_to_secid:
            secid_to_catalyst[permno_to_secid[pp]] = cat_date
    straddle_raw = _pull_atm_straddle(secid_to_catalyst, as_of, conn) if secid_to_catalyst else {}

    last_close = bars.sort_values("date").groupby("permno")["close"].last().to_dict()
    straddle_move: dict[int, float] = {}
    for secid, raw in straddle_raw.items():
        p = secid_to_permno.get(secid)
        S = last_close.get(p)
        if S is not None and S > 0:
            straddle_move[p] = float(raw) / float(S)

    log.info(
        "candidate_generator: OM enrichment — %d iv_rank, %d iv_level, %d straddle",
        len(iv_rank_map), len(iv_level_map), len(straddle_move),
    )
    return iv_rank_map, iv_level_map, straddle_move


# ---------------------------------------------------------------------------
# Salience
# ---------------------------------------------------------------------------

def _compute_salience(records: list[dict]) -> list[dict]:
    """
    Add a 'salience' field via within-bucket rank of the composite attention score.

    Bucket = (SIC major-division × market-cap quintile). Ties broken by canonical_id for
    full determinism. Returns records sorted descending by salience, then canonical_id.
    """
    if not records:
        return records
    df = pd.DataFrame(records)

    df["sic_div"] = (df["siccd"].fillna(0).astype(int) // 1000).clip(0, 9).astype(str)
    try:
        df["cap_q"] = pd.qcut(
            df["mktcap"].fillna(0).astype(float), q=5, labels=False, duplicates="drop"
        ).fillna(0).astype(int).astype(str)
    except ValueError:
        df["cap_q"] = "0"
    df["bucket"] = df["sic_div"] + "_" + df["cap_q"]

    # Within-bucket ascending rank of the composite (higher composite → higher salience),
    # normalised by bucket size. transform keeps the result aligned to df's index.
    df["salience"] = df.groupby("bucket")["composite"].transform(
        lambda s: s.rank(method="average", ascending=True, na_option="bottom") / len(s)
    )
    df["salience"] = df["salience"].fillna(0.0)

    records_out = df.to_dict("records")
    return sorted(records_out, key=lambda r: (-r["salience"], r["canonical_id"]))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate(
    universe: list[CanonicalId],
    as_of: date,
    events: dict[CanonicalId, date] | None = None,
    wrds_conn: "wrds.Connection" = None,
    window_days: int = 252,
    bar_source: BarSource | None = None,
    enrich_with_options: bool = True,
    weights: dict[str, float] | None = None,
) -> list[Candidate]:
    """
    Generate a ranked Candidate list from point-in-time daily bars.

    Parameters
    ----------
    universe            : canonical IDs ("permno:XXXXX") to evaluate
    as_of               : session T0; all data knowable strictly before this date
    events              : {canonical_id: catalyst_date} for straddle-based implied_move
    wrds_conn           : active wrds.Connection. Used to build a WrdsBarSource when
                          `bar_source` is not given, and for SIC + optional OM enrichment.
    window_days         : trailing window for IV rank enrichment (default 252)
    bar_source          : explicit BarSource (e.g. PolygonBarSource). Defaults to
                          WrdsBarSource(wrds_conn).
    enrich_with_options : fold an IV term into salience where OM data exists (needs wrds_conn)
    weights             : override the composite blend; merged over _DEFAULT_WEIGHTS

    Returns
    -------
    list[Candidate] sorted descending by salience; deterministic given the bar vintage.
    """
    as_of_dt = datetime.combine(as_of, datetime.min.time())
    events   = events or {}
    weights  = {**_DEFAULT_WEIGHTS, **(weights or {})}

    # 1. Parse permnos
    permno_to_cid: dict[int, CanonicalId] = {}
    for cid in universe:
        p = _parse_permno(cid)
        if p is not None:
            permno_to_cid[p] = cid
    if not permno_to_cid:
        log.warning("candidate_generator: no valid permno canonical IDs in universe")
        return []
    permnos = list(permno_to_cid.keys())

    # 2. Resolve a bar source
    if bar_source is None:
        if wrds_conn is None:
            raise ValueError("generate() requires either bar_source or wrds_conn")
        bar_source = WrdsBarSource(wrds_conn)

    # 3. Pull bars + market over the trailing window
    lookback = max(window_days, _MIN_LOOKBACK_DAYS)
    start, end = default_window(as_of, lookback)
    bars = bar_source.get_bars(permnos, start, end)
    if bars.empty:
        log.warning("candidate_generator: no bars in window %s–%s", start, end)
        return []
    market = bar_source.get_market(start, end)

    eligible = sorted({int(p) for p in bars["permno"]})

    # 4. Attention signals → z-scored
    z = {
        "realized_vol_spike": signals.zscore_map(signals.realized_vol_spike(bars, _RV_SHORT, _RV_LONG)),
        "abnormal_volume":    signals.zscore_map(signals.abnormal_volume(bars, _VOL_WINDOW)),
        "dislocation":        signals.zscore_map(signals.dislocation(bars, market, _BETA_WINDOW)),
        "gap":                signals.zscore_map(signals.gap(bars)),
    }

    # 5. Bucketing inputs (CRSP — covers every eligible name)
    mktcaps   = _market_caps_from_bars(bars)
    sic_codes = _pull_sic_codes(permnos, as_of, wrds_conn) if wrds_conn is not None else {}

    # 6. Optional OM enrichment
    iv_rank_map: dict[int, float] = {}
    iv_level_map: dict[int, float] = {}
    straddle_move: dict[int, float] = {}
    if enrich_with_options and wrds_conn is not None:
        iv_rank_map, iv_level_map, straddle_move = _enrich_options(
            eligible, events, as_of, window_days, bars, wrds_conn
        )
    z["iv"] = signals.zscore_map(iv_rank_map) if iv_rank_map else {}

    # 7. Composite score + records
    records: list[dict] = []
    for p in eligible:
        cid = permno_to_cid.get(p)
        if cid is None:
            continue
        components = {k: float(z[k].get(p, 0.0)) for k in weights}
        composite  = float(sum(weights[k] * components[k] for k in weights))
        implied_move = straddle_move.get(p)
        records.append({
            "canonical_id": cid,
            "composite":    composite,
            "components":   components,
            "implied_move": implied_move,
            "iv_rank":      iv_rank_map.get(p),
            "measure":      "straddle" if implied_move is not None else "attention",
            "coverage":     True,   # every eligible name is backed by market-data bars
            "has_options":  (p in iv_rank_map) or (p in iv_level_map),
            "siccd":        sic_codes.get(p, 0),
            "mktcap":       mktcaps.get(p, 0.0),
        })

    if not records:
        log.warning("candidate_generator: no candidate records built")
        return []

    # 8. Within-bucket salience
    records_sorted = _compute_salience(records)

    # 9. Assemble Candidates
    candidates = [
        Candidate(
            canonical_id = r["canonical_id"],
            implied_move = r.get("implied_move"),
            iv_rank      = r.get("iv_rank"),
            measure      = r["measure"],
            salience     = float(r.get("salience", 0.0)),
            coverage     = bool(r["coverage"]),
            as_of        = as_of_dt,
            attention    = float(r["composite"]),
            has_options  = bool(r["has_options"]),
            components   = r["components"],
        )
        for r in records_sorted
    ]

    log.info(
        "candidate_generator: %d candidates (%d with options enrichment, %d with straddle)",
        len(candidates),
        sum(1 for c in candidates if c.has_options),
        sum(1 for c in candidates if c.measure == "straddle"),
    )
    return candidates
