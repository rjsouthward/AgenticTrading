"""
Canonical identifier hub for Blind Spot v0.5.

The canonical key is CRSP permno. All public entry points return a
CanonicalId ("permno:<int>") or None. None means unresolved — never
substitute a guess. Log every None so the analyst can audit coverage.

Point-in-time: every resolution takes an as_of date and filters link
tables so only links that were active strictly before session T0 are used.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import wrds

log = logging.getLogger(__name__)

CanonicalId = str  # "permno:14593"

# WRDS table name constants.
# Verify with WRDS MCP (`wrds_mcp_reference_tools`) if schema changes:
#   SELECT table_name FROM information_schema.tables WHERE table_schema = '<lib>';
_CCM_LINK = "crsp_a_ccm.ccmxpf_lnkhist"
_OM_LINK = "wrdsapps_link_crsp_optionm.crsp_optionm_link"
_IBES_LINK = "wrdsapps_link_crsp_ibes.iclink"
_STOCK_NAMES = "crsp_a_stock.stocknames"

_SUPPORTED_ID_TYPES = frozenset({"gvkey", "secid", "ibes_ticker", "ticker"})


def _canon(permno: int) -> CanonicalId:
    return f"permno:{permno}"


# ---------------------------------------------------------------------------
# Single-identifier resolvers
# ---------------------------------------------------------------------------

def resolve_gvkey(
    gvkey: str,
    as_of: date,
    conn: "wrds.Connection",
) -> CanonicalId | None:
    """
    Resolve a Compustat gvkey → permno via CCM link history, point-in-time.

    Uses linktype LC/LU/LX (confirmed/unresearched/retired primary) and
    linkprim P/C (primary/calendar-primary) to keep only high-quality links.
    """
    sql = f"""
        SELECT lpermno
        FROM {_CCM_LINK}
        WHERE gvkey = %(gvkey)s
          AND linktype IN ('LC', 'LU', 'LX')
          AND linkprim IN ('P', 'C')
          AND linkdt <= %(as_of)s
          AND (linkenddt >= %(as_of)s OR linkenddt IS NULL)
        ORDER BY linkdt DESC
        LIMIT 1
    """
    rows = conn.raw_sql(sql, params={"gvkey": gvkey, "as_of": as_of})
    if rows.empty:
        log.warning("entity_resolution: unresolved gvkey=%s as_of=%s", gvkey, as_of)
        return None
    return _canon(int(rows.iloc[0]["lpermno"]))


def resolve_secid(
    secid: int,
    as_of: date,
    conn: "wrds.Connection",
) -> CanonicalId | None:
    """
    Resolve an OptionMetrics secid → permno via the CRSP-OptionMetrics link.

    Table: wrdsapps_link_crsp_optionm.crsp_optionm_link
    Verify column names (sdate/edate) via WRDS MCP if schema changes.
    """
    sql = f"""
        SELECT permno
        FROM {_OM_LINK}
        WHERE secid = %(secid)s
          AND sdate <= %(as_of)s
          AND (edate >= %(as_of)s OR edate IS NULL)
        ORDER BY sdate DESC
        LIMIT 1
    """
    rows = conn.raw_sql(sql, params={"secid": secid, "as_of": as_of})
    if rows.empty:
        log.warning("entity_resolution: unresolved secid=%s as_of=%s", secid, as_of)
        return None
    return _canon(int(rows.iloc[0]["permno"]))


def resolve_ibes_ticker(
    ibes_ticker: str,
    as_of: date,
    conn: "wrds.Connection",
) -> CanonicalId | None:
    """
    Resolve an IBES ticker → permno via the CRSP-IBES link table.

    Ties broken by match quality score (higher = better). Verify column names
    (ticker, lpermno, score, sdate, edate) via WRDS MCP if schema changes.
    """
    sql = f"""
        SELECT lpermno
        FROM {_IBES_LINK}
        WHERE ticker = %(ticker)s
          AND (sdate IS NULL OR sdate <= %(as_of)s)
          AND (edate IS NULL OR edate >= %(as_of)s)
        ORDER BY score DESC
        LIMIT 1
    """
    rows = conn.raw_sql(sql, params={"ticker": ibes_ticker, "as_of": as_of})
    if rows.empty:
        log.warning(
            "entity_resolution: unresolved ibes_ticker=%s as_of=%s", ibes_ticker, as_of
        )
        return None
    return _canon(int(rows.iloc[0]["lpermno"]))


def resolve_ticker(
    ticker: str,
    as_of: date,
    conn: "wrds.Connection",
) -> CanonicalId | None:
    """
    Resolve a trading ticker → permno via CRSP stocknames history.

    stocknames gives point-in-time ticker history (namedt/nameenddt), so
    ticker reuse across different permnos is handled correctly.
    """
    sql = f"""
        SELECT permno
        FROM {_STOCK_NAMES}
        WHERE ticker = %(ticker)s
          AND namedt <= %(as_of)s
          AND (nameenddt >= %(as_of)s OR nameenddt IS NULL)
        ORDER BY namedt DESC
        LIMIT 1
    """
    rows = conn.raw_sql(sql, params={"ticker": ticker, "as_of": as_of})
    if rows.empty:
        log.warning("entity_resolution: unresolved ticker=%s as_of=%s", ticker, as_of)
        return None
    return _canon(int(rows.iloc[0]["permno"]))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def resolve_to_permno(
    identifier: str | int,
    id_type: str,
    as_of: date,
    conn: "wrds.Connection",
) -> CanonicalId | None:
    """
    Resolve any supported identifier to a canonical CanonicalId.

    Parameters
    ----------
    identifier : the raw id value (gvkey string, secid int, ticker string, …)
    id_type    : one of "gvkey" | "secid" | "ibes_ticker" | "ticker"
    as_of      : session T0; resolution is point-in-time strictly before this date
    conn       : active wrds.Connection (caller owns lifecycle)

    Returns "permno:<int>" or None. None means unresolved; never a guess.
    """
    if id_type not in _SUPPORTED_ID_TYPES:
        raise ValueError(
            f"Unknown id_type {id_type!r}. Supported: {sorted(_SUPPORTED_ID_TYPES)}"
        )
    if id_type == "gvkey":
        return resolve_gvkey(str(identifier), as_of, conn)
    if id_type == "secid":
        return resolve_secid(int(identifier), as_of, conn)
    if id_type == "ibes_ticker":
        return resolve_ibes_ticker(str(identifier), as_of, conn)
    if id_type == "ticker":
        return resolve_ticker(str(identifier), as_of, conn)
    return None  # unreachable given the guard above


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def resolve_batch(
    items: list[tuple[str | int, str]],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[tuple[str | int, str], CanonicalId | None]:
    """
    Resolve a list of (identifier, id_type) pairs.

    Returns a dict keyed by the input pair. Unresolved entries map to None
    and are logged individually. Used for resolving A_final name lists.
    """
    return {
        (ident, id_type): resolve_to_permno(ident, id_type, as_of, conn)
        for ident, id_type in items
    }


def resolve_gvkeys_batch(
    gvkeys: list[str],
    as_of: date,
    conn: "wrds.Connection",
) -> dict[str, CanonicalId | None]:
    """
    Resolve a list of Compustat gvkeys → permnos in a single SQL round-trip.

    Used by the graph loaders (TNIC/VTNIC) where resolving one-at-a-time
    would be prohibitively slow. Unresolved gvkeys map to None and are logged.
    Returns a dict covering every input gvkey exactly once.
    """
    unique = list(set(gvkeys))
    if not unique:
        return {}

    sql = f"""
        SELECT gvkey, lpermno
        FROM {_CCM_LINK}
        WHERE gvkey = ANY(%(gvkeys)s)
          AND linktype IN ('LC', 'LU', 'LX')
          AND linkprim IN ('P', 'C')
          AND linkdt <= %(as_of)s
          AND (linkenddt >= %(as_of)s OR linkenddt IS NULL)
        ORDER BY linkdt DESC
    """
    rows = conn.raw_sql(sql, params={"gvkeys": unique, "as_of": as_of})

    result: dict[str, CanonicalId | None] = {}
    for _, row in rows.iterrows():
        gk = str(row["gvkey"])
        if gk not in result:  # first row = most-recent link (ORDER BY linkdt DESC)
            result[gk] = _canon(int(row["lpermno"]))

    for gk in unique:
        if gk not in result:
            log.warning("resolve_gvkeys_batch: unresolved gvkey=%s as_of=%s", gk, as_of)
            result[gk] = None

    return result
