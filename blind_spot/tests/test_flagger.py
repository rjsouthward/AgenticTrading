"""
Offline unit tests for flagger.py — no WRDS, Neo4j, or OptionMetrics credentials required.
"""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from blind_spot.flagger import (
    Flag,
    SeedRecord,
    _build_reason,
    expand_entity,
    flag_blind_spots,
    pull_seeds_from_fbrain,
)

AS_OF = date(2023, 12, 29)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_driver(session_rows):
    """
    Return a mock Neo4j driver whose session yields the given rows.
    session_rows is a list of dicts, one per row returned by run().
    """
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    session_ctx.run.return_value.__iter__ = MagicMock(return_value=iter(session_rows))
    return driver, session_ctx


def make_driver_multi(*row_lists):
    """
    Driver whose successive session.run() calls return each row list in order.
    Each element of row_lists is a list of dicts for one run() call.
    """
    driver = MagicMock()
    session_ctx = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session_ctx)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)

    iters = [iter(rows) for rows in row_lists]
    call_count = [0]

    def run_side_effect(*args, **kwargs):
        idx = call_count[0]
        call_count[0] += 1
        mock_result = MagicMock()
        mock_result.__iter__ = MagicMock(return_value=iters[idx] if idx < len(iters) else iter([]))
        return mock_result

    session_ctx.run.side_effect = run_side_effect
    return driver, session_ctx


def make_candidate(cid, salience=0.5, coverage=True, iv_rank=0.5):
    """Build a minimal Candidate-like object."""
    from blind_spot.candidate_generator import Candidate
    return Candidate(
        canonical_id = cid,
        implied_move = None,
        iv_rank      = iv_rank,
        measure      = "iv_rank",
        salience     = salience,
        coverage     = coverage,
        as_of        = datetime.combine(AS_OF, datetime.min.time()),
    )


def make_seeds(*cids):
    return [SeedRecord(canonical_id=cid, weight=1.0, slug=f"page-{i}") for i, cid in enumerate(cids)]


# ---------------------------------------------------------------------------
# _build_reason
# ---------------------------------------------------------------------------

def test_build_reason_supplies_with_source_span():
    edges = [{"kind": "SUPPLIES", "source_span": "Apple (15.0% of FY2022 revenues)"}]
    reason = _build_reason(["permno:10000", "permno:20000"], edges, {"permno:10000"})
    assert "Apple" in reason
    assert "named customer-supplier" in reason


def test_build_reason_supplies_no_source_span():
    edges = [{"kind": "SUPPLIES", "source_span": None}]
    reason = _build_reason(["permno:10000", "permno:20000"], edges, {"permno:10000"})
    assert "supply chain" in reason


def test_build_reason_competes_with_direct():
    edges = [{"kind": "COMPETES_WITH", "source_span": None}]
    reason = _build_reason(["permno:10000", "permno:20000"], edges, {"permno:10000"})
    assert "product-market peer" in reason
    assert "1" in reason  # 1 hop


def test_build_reason_comoves_with():
    edges = [{"kind": "COMOVES_WITH", "source_span": None}]
    reason = _build_reason(["permno:10000", "permno:20000"], edges, {"permno:10000"})
    assert "co-moving" in reason


def test_build_reason_no_edges():
    reason = _build_reason([], [], set())
    assert reason  # non-empty string


def test_build_reason_supplies_beats_comoves():
    """SUPPLIES should be cited even when COMOVES_WITH also appears."""
    edges = [
        {"kind": "COMOVES_WITH", "source_span": None},
        {"kind": "SUPPLIES", "source_span": "Big Customer (20% of revenue)"},
    ]
    reason = _build_reason(["a", "b", "c"], edges, {"a"})
    assert "Big Customer" in reason


# ---------------------------------------------------------------------------
# expand_entity
# ---------------------------------------------------------------------------

def test_expand_entity_returns_set():
    rows = [{"cid": "permno:20000"}, {"cid": "permno:30000"}]
    driver, _ = make_driver(rows)
    result = expand_entity(["permno:10000"], driver, d_e=2, database="test")
    assert "permno:20000" in result
    assert "permno:30000" in result


def test_expand_entity_includes_seeds():
    rows = [{"cid": "permno:20000"}]
    driver, _ = make_driver(rows)
    result = expand_entity(["permno:10000"], driver, d_e=2, database="test")
    # Seeds are always included in the returned set
    assert "permno:10000" in result


def test_expand_entity_empty_seeds():
    driver = MagicMock()
    result = expand_entity([], driver, d_e=2, database="test")
    assert result == set()
    driver.session.assert_not_called()


def test_expand_entity_deduplicates():
    rows = [{"cid": "permno:20000"}, {"cid": "permno:20000"}]
    driver, _ = make_driver(rows)
    result = expand_entity(["permno:10000"], driver, d_e=1, database="test")
    assert result.count("permno:20000") if isinstance(result, list) else len([x for x in result if x == "permno:20000"]) == 1


def test_expand_entity_filters_null_cids():
    rows = [{"cid": None}, {"cid": "permno:20000"}]
    driver, _ = make_driver(rows)
    result = expand_entity(["permno:10000"], driver, d_e=2, database="test")
    assert None not in result


# ---------------------------------------------------------------------------
# flag_blind_spots — complement logic
# ---------------------------------------------------------------------------

def _setup_flag_test(
    universe_rows=None,
    path_rows=None,
    seed_cids=("permno:10000",),
    candidate_cids=("permno:20000", "permno:30000"),
    a_final=frozenset(["permno:30000"]),
    saliences=None,
):
    """Set up a full flag_blind_spots scenario with mocked Neo4j."""
    if universe_rows is None:
        universe_rows = [{"cid": cid} for cid in candidate_cids]
    if path_rows is None:
        path_rows = [
            {
                "target_cid": cid,
                "node_path": [seed_cids[0], cid],
                "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.4}],
            }
            for cid in candidate_cids
            if cid not in a_final
        ]
    if saliences is None:
        saliences = {cid: 0.5 for cid in candidate_cids}

    driver, session_ctx = make_driver_multi(universe_rows, path_rows)
    seeds = make_seeds(*seed_cids)
    candidates = [
        make_candidate(cid, salience=saliences.get(cid, 0.5))
        for cid in candidate_cids
    ]
    a_final_set = set(a_final)
    return driver, seeds, candidates, a_final_set


def test_flag_blind_spots_excludes_a_final():
    driver, seeds, candidates, a_final = _setup_flag_test(
        seed_cids=("permno:10000",),
        candidate_cids=("permno:20000", "permno:30000"),
        a_final=frozenset(["permno:30000"]),
    )
    flags = flag_blind_spots(candidates, a_final, seeds, driver, k=10, database="test")
    flagged_ids = {f.canonical_id for f in flags}
    assert "permno:30000" not in flagged_ids
    assert "permno:20000" in flagged_ids


def test_flag_blind_spots_excludes_uncovered():
    # permno:20000 is uncovered (coverage=False)
    universe_rows = [{"cid": "permno:20000"}, {"cid": "permno:40000"}]
    path_rows = [
        {"target_cid": "permno:40000", "node_path": ["permno:10000", "permno:40000"],
         "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.4}]},
    ]
    driver, session_ctx = make_driver_multi(universe_rows, path_rows)
    seeds = make_seeds("permno:10000")
    candidates = [
        make_candidate("permno:20000", coverage=False),
        make_candidate("permno:40000", coverage=True),
    ]
    flags = flag_blind_spots(candidates, set(), seeds, driver, k=10, database="test")
    flagged_ids = {f.canonical_id for f in flags}
    assert "permno:20000" not in flagged_ids
    assert "permno:40000" in flagged_ids


def test_flag_blind_spots_excludes_not_in_universe():
    # permno:99999 is covered and not in a_final, but NOT in universe
    universe_rows = [{"cid": "permno:20000"}]
    path_rows = [
        {"target_cid": "permno:20000", "node_path": ["permno:10000", "permno:20000"],
         "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.4}]},
    ]
    driver, session_ctx = make_driver_multi(universe_rows, path_rows)
    seeds = make_seeds("permno:10000")
    candidates = [
        make_candidate("permno:20000", coverage=True),
        make_candidate("permno:99999", coverage=True),  # not in universe
    ]
    flags = flag_blind_spots(candidates, set(), seeds, driver, k=10, database="test")
    flagged_ids = {f.canonical_id for f in flags}
    assert "permno:99999" not in flagged_ids
    assert "permno:20000" in flagged_ids


def test_flag_blind_spots_respects_k():
    universe_rows = [{"cid": f"permno:{i}"} for i in range(10)]
    path_rows = [
        {"target_cid": f"permno:{i}", "node_path": ["permno:seed", f"permno:{i}"],
         "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.3}]}
        for i in range(10)
    ]
    driver, _ = make_driver_multi(universe_rows, path_rows)
    seeds = make_seeds("permno:seed")
    candidates = [make_candidate(f"permno:{i}", salience=float(i) / 10) for i in range(10)]
    flags = flag_blind_spots(candidates, set(), seeds, driver, k=3, database="test")
    assert len(flags) <= 3


def test_flag_blind_spots_sorted_by_salience_desc():
    universe_rows = [{"cid": "permno:20000"}, {"cid": "permno:30000"}, {"cid": "permno:40000"}]
    path_rows = [
        {"target_cid": cid, "node_path": ["permno:seed", cid],
         "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.4}]}
        for cid in ["permno:20000", "permno:30000", "permno:40000"]
    ]
    driver, _ = make_driver_multi(universe_rows, path_rows)
    seeds = make_seeds("permno:seed")
    candidates = [
        make_candidate("permno:20000", salience=0.3),
        make_candidate("permno:30000", salience=0.9),
        make_candidate("permno:40000", salience=0.6),
    ]
    flags = flag_blind_spots(candidates, set(), seeds, driver, k=10, database="test")
    saliences = [f.salience for f in flags]
    assert saliences == sorted(saliences, reverse=True)


def test_flag_blind_spots_thesis_frontier_always_false():
    driver, seeds, candidates, a_final = _setup_flag_test()
    flags = flag_blind_spots(candidates, a_final, seeds, driver, k=10, database="test")
    assert all(f.on_thesis_frontier is False for f in flags)


def test_flag_blind_spots_thesis_path_always_none():
    driver, seeds, candidates, a_final = _setup_flag_test()
    flags = flag_blind_spots(candidates, a_final, seeds, driver, k=10, database="test")
    assert all(f.thesis_path is None for f in flags)


def test_flag_blind_spots_empty_seeds():
    driver = MagicMock()
    candidates = [make_candidate("permno:20000")]
    flags = flag_blind_spots(candidates, set(), [], driver, k=10, database="test")
    assert flags == []


def test_flag_blind_spots_all_in_a_final():
    driver, seeds, candidates, _ = _setup_flag_test(
        candidate_cids=("permno:20000",),
        a_final=frozenset(["permno:20000"]),
    )
    flags = flag_blind_spots(candidates, {"permno:20000"}, seeds, driver, k=10, database="test")
    assert flags == []


def test_flag_blind_spots_reason_non_empty():
    driver, seeds, candidates, a_final = _setup_flag_test()
    flags = flag_blind_spots(candidates, a_final, seeds, driver, k=10, database="test")
    for f in flags:
        assert isinstance(f.reason, str) and len(f.reason) > 0


def test_flag_blind_spots_entity_path_from_graph():
    driver, seeds, candidates, a_final = _setup_flag_test(
        seed_cids=("permno:10000",),
        candidate_cids=("permno:20000", "permno:30000"),
        a_final=frozenset(["permno:30000"]),
        path_rows=[
            {"target_cid": "permno:20000",
             "node_path": ["permno:10000", "permno:20000"],
             "edges": [{"kind": "COMPETES_WITH", "source_span": None, "weight": 0.4}]},
        ],
    )
    flags = flag_blind_spots(candidates, a_final, seeds, driver, k=10, database="test")
    flagged = [f for f in flags if f.canonical_id == "permno:20000"]
    assert len(flagged) == 1
    assert flagged[0].entity_path == ["permno:10000", "permno:20000"]
    assert flagged[0].on_entity_frontier is True


# ---------------------------------------------------------------------------
# pull_seeds_from_fbrain
# ---------------------------------------------------------------------------

def test_pull_seeds_from_fbrain_extracts_tickers():
    pages = [
        {
            "slug": "nvda-analysis",
            "title": "NVDA deep dive",
            "tags": ["NVDA", "semiconductors"],
            "updated_at": "2023-11-01T10:00:00+00:00",
            "in_degree": 3,
        }
    ]
    driver, _ = make_driver(pages)
    conn = MagicMock()

    with patch("blind_spot.flagger.resolve_batch", return_value={("NVDA", "ticker"): "permno:14593"}):
        seeds = pull_seeds_from_fbrain(driver, AS_OF, conn, database="test")

    assert len(seeds) == 1
    assert seeds[0].canonical_id == "permno:14593"
    assert seeds[0].slug == "nvda-analysis"


def test_pull_seeds_from_fbrain_skips_non_tickers():
    pages = [
        {
            "slug": "market-outlook",
            "title": "2023 outlook",
            "tags": ["semiconductors", "AI", "cloud-computing"],  # none are tickers
            "updated_at": "2023-10-01T00:00:00+00:00",
            "in_degree": 1,
        }
    ]
    driver, _ = make_driver(pages)
    conn = MagicMock()

    with patch("blind_spot.flagger.resolve_batch", return_value={}) as mock_resolve:
        seeds = pull_seeds_from_fbrain(driver, AS_OF, conn, database="test")

    # Tags with hyphens don't match the ticker regex; resolve_batch may not even be called
    assert seeds == []


def test_pull_seeds_from_fbrain_skips_unresolved_tickers():
    pages = [
        {
            "slug": "xyz-page",
            "title": "XYZ",
            "tags": ["XYZ"],  # unresolvable ticker
            "updated_at": "2023-11-01T00:00:00+00:00",
            "in_degree": 0,
        }
    ]
    driver, _ = make_driver(pages)
    conn = MagicMock()

    with patch("blind_spot.flagger.resolve_batch", return_value={("XYZ", "ticker"): None}):
        seeds = pull_seeds_from_fbrain(driver, AS_OF, conn, database="test")

    assert seeds == []


def test_pull_seeds_from_fbrain_weights_by_recency():
    pages = [
        {
            "slug": "fresh-page",
            "title": "AAPL",
            "tags": ["AAPL"],
            "updated_at": "2023-12-01T00:00:00+00:00",   # recent
            "in_degree": 1,
        },
        {
            "slug": "stale-page",
            "title": "AAPL",
            "tags": ["AAPL"],
            "updated_at": "2022-01-01T00:00:00+00:00",   # old
            "in_degree": 1,
        },
    ]
    # Two pages both resolve to same canonical_id → pick highest weight
    driver, _ = make_driver(pages)
    conn = MagicMock()

    with patch("blind_spot.flagger.resolve_batch", return_value={("AAPL", "ticker"): "permno:14593"}):
        seeds = pull_seeds_from_fbrain(driver, AS_OF, conn, database="test")

    # Should still produce one seed for permno:14593 (fresh-page wins)
    assert len(seeds) == 1
    assert seeds[0].slug == "fresh-page"


def test_pull_seeds_from_fbrain_empty_graph():
    driver, _ = make_driver([])
    conn = MagicMock()
    seeds = pull_seeds_from_fbrain(driver, AS_OF, conn, database="test")
    assert seeds == []
