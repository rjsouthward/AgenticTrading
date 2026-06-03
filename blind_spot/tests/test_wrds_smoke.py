"""
WRDS smoke test — requires live WRDS credentials.

Run with:  WRDS_USERNAME=<your_username> pytest -m integration blind_spot/tests/test_wrds_smoke.py -v

What this tests:
  1. wrds.Connection() can be established (credentials valid / cached).
  2. crsp_a_ccm.ccmxpf_lnkhist is accessible and has the expected columns.
  3. A dated point-in-time gvkey resolution round-trips through resolve_gvkey.
  4. crsp_a_stock.stocknames is accessible and resolves a known ticker.

The chosen fixture: AAPL (gvkey=001690, permno=14593 from ~1980 onward).
These are public facts; if the assertion fails the link table schema changed.
"""
import pytest
from datetime import date

pytest.importorskip("wrds", reason="wrds package not installed")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn():
    import os
    import wrds
    username = os.environ.get("WRDS_USERNAME", "").strip()
    if not username:
        pytest.skip("Set WRDS_USERNAME env var to run integration tests")
    c = wrds.Connection(username=username)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. CCM link table is accessible and has expected columns
# ---------------------------------------------------------------------------

def test_ccm_link_table_accessible(conn):
    rows = conn.raw_sql(
        "SELECT gvkey, lpermno, linktype, linkprim, linkdt, linkenddt "
        "FROM crsp_a_ccm.ccmxpf_lnkhist LIMIT 5"
    )
    assert not rows.empty, "ccmxpf_lnkhist returned no rows"
    for col in ("gvkey", "lpermno", "linktype", "linkprim", "linkdt", "linkenddt"):
        assert col in rows.columns, f"missing column: {col}"


# ---------------------------------------------------------------------------
# 2. resolve_gvkey round-trip for a known fixture
# ---------------------------------------------------------------------------

def test_resolve_gvkey_aapl(conn):
    from blind_spot.entity_resolution import resolve_gvkey

    # AAPL: gvkey=001690, permno=14593 (active from the 1980s onward)
    result = resolve_gvkey("001690", date(2020, 1, 15), conn)
    assert result == "permno:14593", (
        f"Expected permno:14593 for AAPL gvkey=001690, got {result!r}. "
        "If CCM reassigned the link, update this fixture."
    )


# ---------------------------------------------------------------------------
# 3. CRSP stocknames accessible + ticker resolution
# ---------------------------------------------------------------------------

def test_stocknames_table_accessible(conn):
    rows = conn.raw_sql(
        "SELECT permno, ticker, namedt, nameendt "
        "FROM crsp_a_stock.stocknames LIMIT 5"
    )
    assert not rows.empty, "stocknames returned no rows"
    for col in ("permno", "ticker", "namedt", "nameendt"):
        assert col in rows.columns, f"missing column: {col}"


def test_resolve_ticker_aapl(conn):
    from blind_spot.entity_resolution import resolve_ticker

    result = resolve_ticker("AAPL", date(2020, 1, 15), conn)
    assert result == "permno:14593", (
        f"Expected permno:14593 for AAPL, got {result!r}."
    )


# ---------------------------------------------------------------------------
# 4. Point-in-time: same (gvkey, as_of) returns identical permno on re-run
# ---------------------------------------------------------------------------

def test_gvkey_resolution_is_deterministic(conn):
    from blind_spot.entity_resolution import resolve_gvkey

    as_of = date(2019, 6, 1)
    r1 = resolve_gvkey("001690", as_of, conn)
    r2 = resolve_gvkey("001690", as_of, conn)
    assert r1 == r2, "resolve_gvkey is not deterministic for the same (gvkey, as_of)"
