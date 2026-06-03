"""
Lane B candidate generator for Blind Spot v0.5 — Task 5.

OptionMetrics vol surface → implied_move + iv_rank → within-bucket salience → Candidate list.
Pure function of a point-in-time market snapshot; deterministic given snapshot vintage.

Implied move (for named catalyst):
    implied_move = (C_ATM + P_ATM) / S
    at the first OM expiry after the catalyst date, evaluated at T0.

IV rank (trailing window W):
    iv_rank = (IV_T0 − min_W IV) / (max_W IV − min_W IV)
    Pulled from vsurfd at days=30, delta=50 (call ATM on the OM integer-delta grid).

Salience:
    Cross-sectional rank of iv_rank within a (SIC-division × market-cap-quintile) bucket,
    normalised to [0, 1]. Kills fat-tail domination by structurally-high-IV names and the
    small-cap/biotech IV confound.

Coverage = True when the secid is found in OptionMetrics and ATM IV is non-null on as_of.

Usage
-----
    from blind_spot.candidate_generator import generate
    candidates = generate(
        universe=["permno:14593", "permno:10000"],
        as_of=date(2023, 12, 31),
        events={"permno:14593": date(2024, 1, 10)},
        wrds_conn=conn,
    )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    import wrds

log = logging.getLogger(__name__)

CanonicalId = str

_OM_LINK    = "wrdsapps_link_crsp_optionm.opcrsphist"
_SECPRD     = "optionm_all.secprd{year}"
_VSURFD     = "optionm_all.vsurfd{year}"
_OPPRCD     = "optionm_all.opprcd{year}"
_STOCKNAMES = "crsp.stocknames"

# OptionMetrics vol surface parameters
_ATM_DELTA     = 50   # vsurfd integer-delta scale; 50 = 0.50 delta (call ATM)
_IV_TENOR      = 30   # standardised DTE for IV rank
_MAX_LINK_SCORE = 6   # opcrsphist: accept links up to score=6 (1=exact, higher=looser)
_MIN_IV_OBS    = 10   # minimum IV history obs to compute iv_rank


@dataclass(frozen=True)
class Candidate:
    canonical_id: CanonicalId
    implied_move: float | None   # straddle/S for catalyst expiry; None if no liquid chain
    iv_rank: float | None        # position in trailing min/max, in [0, 1]; None if <min_obs
    measure: str                 # "straddle" | "iv_rank" | "iv_level"
    salience: float              # within-bucket normalised rank, in (0, 1]
    coverage: bool               # True only if optionable + ATM IV is non-null on as_of
    as_of: datetime              # T0; every field knowable strictly before T0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_permno(cid: CanonicalId) -> int | None:
    if cid and cid.startswith("permno:"):
        try:
            return int(cid.split(":")[1])
        except ValueError:
            pass
    return None


def _vsurfd_table_years(window_start: date, window_end: date) -> list[int]:
    return sorted({window_start.year, window_end.year})


def _resolve_universe_secids(
    permnos: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, int]:
    """Return {permno: secid} for permnos with a valid OM link active on as_of."""
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
    # Keep best (lowest-score) link per permno
    df = df.sort_values("score").drop_duplicates("permno", keep="first")
    result = dict(zip(df["permno"], df["secid"]))
    log.info(
        "candidate_generator: %d/%d permnos resolved to OM secids",
        len(result), len(permnos),
    )
    return result


def _pull_atm_iv(
    secids: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, float]:
    """Return {secid: impl_volatility} from vsurfd at delta=50, days=30, date=as_of."""
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
    secids: list[int],
    as_of: date,
    window_days: int,
    conn: "wrds.Connection",
) -> dict[int, tuple[float, float]]:
    """
    Return {secid: (min_iv, max_iv)} over the trailing window ending strictly before as_of.

    Pulls up to `window_days` trading-day IV observations at delta=50, days=30 from vsurfd.
    Queries both years if the window crosses a calendar year boundary.
    """
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
    combined = combined.dropna(subset=["impl_volatility"])
    # Keep last window_days trading-day observations per stock
    combined = combined.sort_values("date")
    combined = combined.groupby("secid", group_keys=False).tail(window_days)

    result: dict[int, tuple[float, float]] = {}
    for secid_val, grp in combined.groupby("secid"):
        ivs = grp["impl_volatility"].values.astype(float)
        if len(ivs) >= _MIN_IV_OBS:
            result[int(secid_val)] = (float(ivs.min()), float(ivs.max()))

    log.info(
        "candidate_generator: iv_rank computable for %d/%d secids (min_obs=%d)",
        len(result), len(secids), _MIN_IV_OBS,
    )
    return result


def _pull_atm_straddle(
    secid_to_catalyst: dict[int, date],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, float]:
    """
    For each secid with a named catalyst, find the first OM expiry strictly after the catalyst
    and compute the model-free ATM straddle price: call_mid + put_mid.

    Returns {secid: raw_straddle_price}. Caller normalises by underlying S.
    Requires open_interest > 0 and both bid/offer non-null to trust the mid.
    """
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
        # First expiry strictly after the catalyst date
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

    log.info(
        "candidate_generator: straddle price found for %d/%d named catalysts",
        len(straddle_prices), len(secid_to_catalyst),
    )
    return straddle_prices


def _pull_underlying_prices(
    secids: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, float]:
    """Return {secid: close} from secprd on the last trading day strictly before as_of."""
    if not secids:
        return {}
    window_start = as_of - timedelta(days=10)
    sql = f"""
        SELECT secid, date, close
        FROM {_SECPRD.format(year=as_of.year)}
        WHERE secid = ANY(%(secids)s)
          AND date >= %(start)s
          AND date < %(end)s
          AND close IS NOT NULL
          AND close > 0
        ORDER BY date DESC
    """
    df = conn.raw_sql(sql, params={"secids": list(secids), "start": window_start, "end": as_of})
    if df.empty:
        return {}
    df["secid"] = df["secid"].astype(int)
    df = df.sort_values("date", ascending=False).drop_duplicates("secid", keep="first")
    return {int(r["secid"]): float(r["close"]) for _, r in df.iterrows()}


def _pull_sic_codes(
    permnos: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, int]:
    """Return {permno: siccd} from CRSP stocknames, point-in-time."""
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


def _pull_market_caps(
    secids: list[int],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[int, float]:
    """Return {secid: market_cap_usd} from secprd (close × shrout × 1000)."""
    if not secids:
        return {}
    window_start = as_of - timedelta(days=10)
    sql = f"""
        SELECT secid, date, close, shrout
        FROM {_SECPRD.format(year=as_of.year)}
        WHERE secid = ANY(%(secids)s)
          AND date >= %(start)s
          AND date < %(end)s
          AND close IS NOT NULL
          AND shrout IS NOT NULL
          AND close > 0
          AND shrout > 0
        ORDER BY date DESC
    """
    df = conn.raw_sql(sql, params={"secids": list(secids), "start": window_start, "end": as_of})
    if df.empty:
        return {}
    df["secid"] = df["secid"].astype(int)
    df = df.sort_values("date", ascending=False).drop_duplicates("secid", keep="first")
    df["mktcap"] = df["close"].astype(float) * df["shrout"].astype(float) * 1_000
    return {int(r["secid"]): float(r["mktcap"]) for _, r in df.iterrows()}


def _compute_salience(records: list[dict]) -> list[dict]:
    """
    Add a 'salience' field to each record via within-bucket rank of iv_rank.

    Bucket = (SIC major-division × market-cap quintile).
    Names without iv_rank fall back to iv_level for the bucket rank.
    Ties are broken by canonical_id (alphabetic) for full determinism.

    Returns records sorted descending by salience, then by canonical_id.
    """
    if not records:
        return records

    df = pd.DataFrame(records)

    # SIC major division: first digit of 4-digit SIC code (0–9)
    df["sic_div"] = (df["siccd"].fillna(0).astype(int) // 1000).clip(0, 9).astype(str)

    # Market-cap quintile (5 = largest); qcut falls back gracefully if few unique values
    try:
        df["cap_q"] = pd.qcut(
            df["mktcap"].fillna(0).astype(float), q=5, labels=False, duplicates="drop"
        ).fillna(0).astype(int).astype(str)
    except ValueError:
        df["cap_q"] = "0"

    df["bucket"] = df["sic_div"] + "_" + df["cap_q"]

    # Rank key: prefer iv_rank; fall back to iv_level for uncovered names
    df["rank_key"] = df["iv_rank"].where(df["iv_rank"].notna(), df["iv_level"])

    # Within-bucket ascending rank (higher iv_elevation → higher rank → higher salience)
    def bucket_salience(grp: pd.DataFrame) -> pd.Series:
        n = len(grp)
        ranked = grp["rank_key"].rank(method="average", ascending=True, na_option="bottom")
        return ranked / n

    df["salience"] = df.groupby("bucket", group_keys=False).apply(
        bucket_salience, include_groups=False
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
) -> list[Candidate]:
    """
    Generate a ranked Candidate list from a point-in-time OptionMetrics snapshot.

    Parameters
    ----------
    universe    : canonical IDs ("permno:XXXXX") to evaluate
    as_of       : session T0; all data knowable strictly before this date
    events      : {canonical_id: catalyst_date} for straddle-based implied_move
    wrds_conn   : active wrds.Connection
    window_days : trailing window for IV rank (default 252 = 1 year)

    Returns
    -------
    list[Candidate] sorted descending by salience; deterministic given snapshot vintage.
    """
    as_of_dt = datetime.combine(as_of, datetime.min.time())
    events = events or {}

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

    # 2. Resolve permno → secid
    permno_to_secid = _resolve_universe_secids(permnos, as_of, wrds_conn)
    if not permno_to_secid:
        log.warning("candidate_generator: no OM secids resolved; universe has no OM coverage")
        return []

    secid_to_permno = {v: k for k, v in permno_to_secid.items()}
    secids = list(permno_to_secid.values())

    # 3. ATM IV on as_of
    atm_iv = _pull_atm_iv(secids, as_of, wrds_conn)

    # 4. IV history → (min, max) per secid for iv_rank
    iv_history = _pull_iv_history(secids, as_of, window_days, wrds_conn)

    # 5. Straddle prices for named catalysts
    secid_to_catalyst: dict[int, date] = {}
    for cid, cat_date in events.items():
        p = _parse_permno(cid)
        if p is not None and p in permno_to_secid:
            secid_to_catalyst[permno_to_secid[p]] = cat_date

    straddle_raw = _pull_atm_straddle(secid_to_catalyst, as_of, wrds_conn) if secid_to_catalyst else {}
    prices       = _pull_underlying_prices(list(secid_to_catalyst.keys()), as_of, wrds_conn) if secid_to_catalyst else {}

    # 6. SIC codes + market caps for bucket assignment
    sic_codes = _pull_sic_codes(permnos, as_of, wrds_conn)
    mktcaps   = _pull_market_caps(secids, as_of, wrds_conn)

    # 7. Build per-stock records
    records: list[dict] = []
    for secid, permno in secid_to_permno.items():
        cid = permno_to_cid.get(permno)
        if cid is None:
            continue

        iv_today = atm_iv.get(secid)
        hist     = iv_history.get(secid)

        iv_rank: float | None = None
        if iv_today is not None and hist is not None:
            lo, hi = hist
            if hi > lo:
                iv_rank = float(np.clip((iv_today - lo) / (hi - lo), 0.0, 1.0))
            else:
                iv_rank = 0.5  # flat history → middle rank

        implied_move: float | None = None
        measure = "iv_level"
        if secid in straddle_raw and secid in prices and prices[secid] > 0:
            implied_move = float(straddle_raw[secid]) / prices[secid]
            measure = "straddle"
        elif iv_rank is not None:
            measure = "iv_rank"

        records.append({
            "canonical_id": cid,
            "implied_move": implied_move,
            "iv_rank":      iv_rank,
            "iv_level":     iv_today,
            "measure":      measure,
            "coverage":     iv_today is not None,
            "siccd":        sic_codes.get(permno, 0),
            "mktcap":       mktcaps.get(secid, 0.0),
        })

    if not records:
        log.warning("candidate_generator: no candidate records built")
        return []

    # 8. Within-bucket salience
    records_sorted = _compute_salience(records)

    # 9. Assemble Candidate objects
    candidates = [
        Candidate(
            canonical_id = r["canonical_id"],
            implied_move = r.get("implied_move"),
            iv_rank      = r.get("iv_rank"),
            measure      = r["measure"],
            salience     = float(r.get("salience", 0.0)),
            coverage     = bool(r["coverage"]),
            as_of        = as_of_dt,
        )
        for r in records_sorted
    ]

    log.info(
        "candidate_generator: %d candidates generated (%d with coverage, %d with straddle)",
        len(candidates),
        sum(1 for c in candidates if c.coverage),
        sum(1 for c in candidates if c.measure == "straddle"),
    )
    return candidates
