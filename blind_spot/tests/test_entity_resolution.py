"""
Offline unit tests for entity_resolution.py.

All WRDS I/O is mocked — no credentials required.
Tests cover: happy-path resolution, None-on-empty, ValueError on bad id_type,
and the point-in-time guarantee (SQL params carry the as_of date).
"""
from datetime import date
from unittest.mock import MagicMock, call

import pandas as pd
import pytest

from blind_spot.entity_resolution import (
    CanonicalId,
    resolve_batch,
    resolve_gvkey,
    resolve_ibes_ticker,
    resolve_secid,
    resolve_ticker,
    resolve_to_permno,
)

AS_OF = date(2020, 6, 15)


def mock_conn(rows: dict) -> MagicMock:
    """Return a mock wrds.Connection whose raw_sql returns a DataFrame of `rows`."""
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame(rows)
    return conn


def empty_conn() -> MagicMock:
    """Return a mock wrds.Connection that always returns an empty DataFrame."""
    conn = MagicMock()
    conn.raw_sql.return_value = pd.DataFrame()
    return conn


# ---------------------------------------------------------------------------
# resolve_gvkey
# ---------------------------------------------------------------------------

def test_resolve_gvkey_found():
    conn = mock_conn({"lpermno": [14593]})
    assert resolve_gvkey("001076", AS_OF, conn) == "permno:14593"


def test_resolve_gvkey_not_found_returns_none():
    assert resolve_gvkey("999999", AS_OF, empty_conn()) is None


def test_resolve_gvkey_passes_as_of_in_params():
    conn = mock_conn({"lpermno": [10]})
    resolve_gvkey("001076", AS_OF, conn)
    _, kwargs = conn.raw_sql.call_args
    params = kwargs["params"]
    assert params["as_of"] == AS_OF
    assert params["gvkey"] == "001076"


# ---------------------------------------------------------------------------
# resolve_secid
# ---------------------------------------------------------------------------

def test_resolve_secid_found():
    conn = mock_conn({"permno": [76076]})
    assert resolve_secid(101234, AS_OF, conn) == "permno:76076"


def test_resolve_secid_not_found_returns_none():
    assert resolve_secid(0, AS_OF, empty_conn()) is None


def test_resolve_secid_passes_as_of_in_params():
    conn = mock_conn({"permno": [76076]})
    resolve_secid(101234, AS_OF, conn)
    _, kwargs = conn.raw_sql.call_args
    params = kwargs["params"]
    assert params["as_of"] == AS_OF
    assert params["secid"] == 101234


# ---------------------------------------------------------------------------
# resolve_ibes_ticker
# ---------------------------------------------------------------------------

def test_resolve_ibes_ticker_found():
    conn = mock_conn({"lpermno": [14593]})
    assert resolve_ibes_ticker("AAPL", AS_OF, conn) == "permno:14593"


def test_resolve_ibes_ticker_not_found_returns_none():
    assert resolve_ibes_ticker("ZZZZ", AS_OF, empty_conn()) is None


# ---------------------------------------------------------------------------
# resolve_ticker
# ---------------------------------------------------------------------------

def test_resolve_ticker_found():
    conn = mock_conn({"permno": [14593]})
    assert resolve_ticker("AAPL", AS_OF, conn) == "permno:14593"


def test_resolve_ticker_not_found_returns_none():
    assert resolve_ticker("ZZZZ", AS_OF, empty_conn()) is None


def test_resolve_ticker_passes_as_of_in_params():
    conn = mock_conn({"permno": [14593]})
    resolve_ticker("AAPL", AS_OF, conn)
    _, kwargs = conn.raw_sql.call_args
    params = kwargs["params"]
    assert params["as_of"] == AS_OF
    assert params["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# resolve_to_permno dispatcher
# ---------------------------------------------------------------------------

def test_dispatcher_gvkey():
    conn = mock_conn({"lpermno": [14593]})
    assert resolve_to_permno("001076", "gvkey", AS_OF, conn) == "permno:14593"


def test_dispatcher_secid():
    conn = mock_conn({"permno": [14593]})
    assert resolve_to_permno(101234, "secid", AS_OF, conn) == "permno:14593"


def test_dispatcher_ibes_ticker():
    conn = mock_conn({"lpermno": [14593]})
    assert resolve_to_permno("AAPL", "ibes_ticker", AS_OF, conn) == "permno:14593"


def test_dispatcher_ticker():
    conn = mock_conn({"permno": [14593]})
    assert resolve_to_permno("AAPL", "ticker", AS_OF, conn) == "permno:14593"


def test_dispatcher_unknown_id_type_raises():
    with pytest.raises(ValueError, match="Unknown id_type"):
        resolve_to_permno("AAPL", "cusip", AS_OF, MagicMock())


# ---------------------------------------------------------------------------
# resolve_batch
# ---------------------------------------------------------------------------

def test_batch_resolves_all_items():
    conn = mock_conn({"lpermno": [14593]})  # returns same row for every call
    items = [("001076", "gvkey"), ("AAPL", "ibes_ticker")]
    result = resolve_batch(items, AS_OF, conn)
    assert result[("001076", "gvkey")] == "permno:14593"
    assert result[("AAPL", "ibes_ticker")] == "permno:14593"


def test_batch_none_on_unresolved():
    items = [("ZZZZ", "ticker")]
    result = resolve_batch(items, AS_OF, empty_conn())
    assert result[("ZZZZ", "ticker")] is None


# ---------------------------------------------------------------------------
# Point-in-time determinism: same vintage → same result
# ---------------------------------------------------------------------------

def test_determinism_same_vintage_same_result():
    """Resolving the same (identifier, as_of) twice returns identical output."""
    conn = mock_conn({"lpermno": [14593]})
    r1 = resolve_gvkey("001076", AS_OF, conn)
    r2 = resolve_gvkey("001076", AS_OF, conn)
    assert r1 == r2


def test_determinism_different_as_of_may_differ():
    """
    Two different as_of dates can return different permnos (merger/ticker reuse).
    Verify the as_of param is forwarded; correctness of the mapping is WRDS's.
    """
    early = date(2000, 1, 1)
    late = date(2020, 1, 1)
    conn_early = mock_conn({"lpermno": [10000]})
    conn_late = mock_conn({"lpermno": [14593]})
    r_early = resolve_gvkey("001076", early, conn_early)
    r_late = resolve_gvkey("001076", late, conn_late)
    assert r_early == "permno:10000"
    assert r_late == "permno:14593"
    # Confirm the as_of date was actually threaded through
    assert conn_early.raw_sql.call_args[1]["params"]["as_of"] == early
    assert conn_late.raw_sql.call_args[1]["params"]["as_of"] == late
