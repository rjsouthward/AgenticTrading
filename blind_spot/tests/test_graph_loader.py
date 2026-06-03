"""
Offline unit tests for graph_loader.py.

All WRDS and Neo4j I/O is mocked — no credentials or live DB required.
"""
import textwrap
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

from blind_spot.graph_loader import (
    _load_edges,
    ensure_schema,
    load_tnic,
    load_vtnic,
    tnic_vintage_for,
)


# ---------------------------------------------------------------------------
# tnic_vintage_for — point-in-time vintage selection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("as_of, expected", [
    # before July 1 of Y → vintage is Y-2
    (date(2021, 1, 15),  2019),
    (date(2021, 6, 30),  2019),
    # on or after July 1 of Y → vintage is Y-1
    (date(2021, 7,  1),  2020),
    (date(2021, 8,  1),  2020),
    (date(2022, 6, 30),  2020),
    (date(2022, 7,  1),  2021),
])
def test_tnic_vintage_for(as_of, expected):
    assert tnic_vintage_for(as_of) == expected


# ---------------------------------------------------------------------------
# ensure_schema — runs the right Cypher
# ---------------------------------------------------------------------------

def test_ensure_schema_creates_constraint_and_index():
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    ensure_schema(driver, database="testdb")

    calls = [str(c) for c in session_ctx.run.call_args_list]
    assert any("Security" in c and "UNIQUE" in c for c in calls), "no uniqueness constraint"
    assert any("INDEX" in c for c in calls), "no index"


# ---------------------------------------------------------------------------
# Helpers for constructing minimal TNIC/VTNIC flat files
# ---------------------------------------------------------------------------

def _write_tnic_file(tmp_path: Path, rows: list[tuple], year: int | None = None) -> Path:
    fp = tmp_path / "tnic.txt"
    header = "gvkey1 gvkey2 score" if year is None else "gvkey1 gvkey2 score year"
    lines = [header]
    for row in rows:
        if year is not None:
            lines.append(f"{row[0]} {row[1]} {row[2]} {year}")
        else:
            lines.append(f"{row[0]} {row[1]} {row[2]}")
    fp.write_text("\n".join(lines))
    return fp


def _mock_wrds(mapping: dict[str, str | None]):
    """Return a mock wrds conn whose raw_sql returns a DataFrame built from mapping."""
    conn = MagicMock()
    rows = [
        {"gvkey": gk, "lpermno": int(pid.split(":")[1]) if pid else None}
        for gk, pid in mapping.items()
        if pid is not None
    ]
    conn.raw_sql.return_value = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["gvkey", "lpermno"])
    return conn


def _mock_driver() -> tuple[MagicMock, MagicMock]:
    """Return (driver, session_ctx) mocks with __enter__/__exit__ wired up."""
    session_ctx = MagicMock()
    driver = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session_ctx


# ---------------------------------------------------------------------------
# load_tnic — happy path
# ---------------------------------------------------------------------------

def test_load_tnic_happy_path(tmp_path):
    fp = _write_tnic_file(tmp_path, [("001076", "001690", "0.15")], year=2020)
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, session_ctx = _mock_driver()

    stats = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)

    assert stats["edges_written"] == 1
    assert stats["edges_skipped_score"] == 0
    assert stats["edges_skipped_unresolved"] == 0
    assert stats["nodes_merged"] == 2


def test_load_tnic_no_year_column(tmp_path):
    """File without a year column: all rows are loaded regardless of vintage_year."""
    fp = _write_tnic_file(tmp_path, [("001076", "001690", "0.15")])  # no year col
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, _ = _mock_driver()

    stats = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)
    assert stats["edges_written"] == 1


# ---------------------------------------------------------------------------
# Score filtering
# ---------------------------------------------------------------------------

def test_load_tnic_score_filter(tmp_path):
    rows = [("001076", "001690", "0.15"), ("001076", "002000", "0.005")]
    fp = _write_tnic_file(tmp_path, rows, year=2020)
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593", "002000": "permno:20000"})
    driver, _ = _mock_driver()

    stats = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver, min_score=0.01)
    assert stats["edges_written"] == 1
    assert stats["edges_skipped_score"] == 1


# ---------------------------------------------------------------------------
# Unresolved gvkey — dropped, not errored
# ---------------------------------------------------------------------------

def test_load_tnic_unresolved_gvkey_is_skipped(tmp_path):
    fp = _write_tnic_file(tmp_path, [("001076", "ZZZZZZ", "0.20")], year=2020)
    conn = _mock_wrds({"001076": "permno:10000"})  # ZZZZZ not in mapping → None
    driver, _ = _mock_driver()

    stats = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)
    assert stats["edges_written"] == 0
    assert stats["edges_skipped_unresolved"] == 1
    assert stats["gvkeys_unresolved"] == 1


# ---------------------------------------------------------------------------
# Direction canonicalization: lower permno is always src
# ---------------------------------------------------------------------------

def test_load_tnic_canonical_direction(tmp_path):
    """Regardless of file order, src should have the lower permno number."""
    # 14593 > 10000, so the edge should be stored as 10000 → 14593
    fp = _write_tnic_file(tmp_path, [("001690", "001076", "0.20")], year=2020)
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, session_ctx = _mock_driver()

    load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert batch[0]["src"] == "permno:10000"
    assert batch[0]["dst"] == "permno:14593"


# ---------------------------------------------------------------------------
# Deduplication: A→B and B→A in same file → one edge, max weight kept
# ---------------------------------------------------------------------------

def test_load_tnic_deduplication(tmp_path):
    rows = [
        ("001076", "001690", "0.20"),
        ("001690", "001076", "0.25"),  # duplicate, higher weight
    ]
    fp = _write_tnic_file(tmp_path, rows, year=2020)
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, session_ctx = _mock_driver()

    stats = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)
    assert stats["edges_written"] == 1
    batch = session_ctx.run.call_args[1]["batch"]
    assert abs(batch[0]["weight"] - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# load_vtnic — uses vscore column and VERTICAL relationship type
# ---------------------------------------------------------------------------

def test_load_vtnic_uses_vscore_column(tmp_path):
    fp = tmp_path / "vtnic.txt"
    fp.write_text("gvkey1 gvkey2 vscore year\n001076 001690 0.30 2020\n")
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, session_ctx = _mock_driver()

    stats = load_vtnic(fp, 2020, date(2021, 8, 1), conn, driver)
    assert stats["edges_written"] == 1

    cypher_call = session_ctx.run.call_args[0][0]
    assert "VERTICAL" in cypher_call


# ---------------------------------------------------------------------------
# Provenance and vintage stored on edges
# ---------------------------------------------------------------------------

def test_edge_provenance_and_vintage(tmp_path):
    fp = _write_tnic_file(tmp_path, [("001076", "001690", "0.15")], year=2020)
    conn = _mock_wrds({"001076": "permno:10000", "001690": "permno:14593"})
    driver, session_ctx = _mock_driver()

    load_tnic(fp, 2020, date(2021, 8, 1), conn, driver)

    batch = session_ctx.run.call_args[1]["batch"]
    assert batch[0]["provenance"] == "tnic"
    assert batch[0]["vintage"] == 2020


# ---------------------------------------------------------------------------
# Missing file raises FileNotFoundError
# ---------------------------------------------------------------------------

def test_missing_file_raises(tmp_path):
    conn = MagicMock()
    driver = MagicMock()
    with pytest.raises(FileNotFoundError, match="hobergphillips"):
        load_tnic(tmp_path / "does_not_exist.txt", 2020, date(2021, 8, 1), conn, driver)


# ---------------------------------------------------------------------------
# Point-in-time determinism: same file + same vintage → same stats
# ---------------------------------------------------------------------------

def test_load_tnic_is_deterministic(tmp_path):
    fp = _write_tnic_file(tmp_path, [("001076", "001690", "0.15"), ("001076", "002000", "0.12")], year=2020)
    conn = _mock_wrds({
        "001076": "permno:10000",
        "001690": "permno:14593",
        "002000": "permno:20000",
    })

    driver1, _ = _mock_driver()
    driver2, _ = _mock_driver()

    stats1 = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver1)
    stats2 = load_tnic(fp, 2020, date(2021, 8, 1), conn, driver2)

    assert stats1 == stats2, "load_tnic is not deterministic for the same inputs"
