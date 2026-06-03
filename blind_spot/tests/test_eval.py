"""
Offline unit tests for eval.py — no WRDS or Neo4j credentials required.
"""
import json
import uuid
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from blind_spot.eval import (
    SessionLogger,
    _f_beta,
    aggregate_sessions,
    score_session,
)

AS_OF = date(2023, 12, 29)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candidate(cid, salience=0.5, coverage=True, iv_rank=0.5):
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


def make_flag(cid, salience=0.5):
    from blind_spot.flagger import Flag
    return Flag(
        canonical_id       = cid,
        salience           = salience,
        on_entity_frontier = True,
        on_thesis_frontier = False,
        entity_path        = ["permno:seed", cid],
        thesis_path        = None,
        reason             = "product-market peer",
    )


def fresh_logger(tmp_path, session_id=None):
    sid = session_id or str(uuid.uuid4())
    path = tmp_path / "sessions.jsonl"
    return SessionLogger(path, sid), path, sid


def write_full_session(tmp_path, session_id, candidate_ids, flag_ids, a_final_ids,
                       accepts=(), dismisses=(), n_turns=1):
    """Write a complete synthetic session to the log."""
    logger = SessionLogger(tmp_path / "sessions.jsonl", session_id)
    candidates = [make_candidate(cid) for cid in candidate_ids]
    flags      = [make_flag(cid) for cid in flag_ids]

    for turn in range(1, n_turns + 1):
        logger.log_candidates(turn, candidates, AS_OF)
        logger.log_flags(turn, flags)

    for cid in accepts:
        logger.log_accept(turn=1, flag_id=cid)
    for cid in dismisses:
        logger.log_dismiss(turn=1, flag_id=cid)

    logger.log_a_final(set(a_final_ids))
    return tmp_path / "sessions.jsonl"


# ---------------------------------------------------------------------------
# _f_beta
# ---------------------------------------------------------------------------

def test_f_beta_perfect():
    assert _f_beta(1.0, 1.0, beta=0.5) == pytest.approx(1.0)


def test_f_beta_zero_precision():
    assert _f_beta(0.0, 1.0, beta=0.5) == pytest.approx(0.0)


def test_f_beta_zero_recall():
    assert _f_beta(1.0, 0.0, beta=0.5) == pytest.approx(0.0)


def test_f_beta_both_zero():
    assert _f_beta(0.0, 0.0, beta=0.5) == pytest.approx(0.0)


def test_f_beta_precision_favoring_penalises_low_precision():
    """With β=0.5, low precision should hurt more than low recall."""
    score_low_prec = _f_beta(precision=0.2, recall=1.0, beta=0.5)
    score_low_rec  = _f_beta(precision=1.0, recall=0.2, beta=0.5)
    assert score_low_prec < score_low_rec


def test_f_beta_half_precision_half_recall():
    result = _f_beta(0.5, 0.5, beta=0.5)
    assert 0.0 < result < 1.0


# ---------------------------------------------------------------------------
# SessionLogger — write events
# ---------------------------------------------------------------------------

def test_session_logger_creates_file(tmp_path):
    logger, path, _ = fresh_logger(tmp_path)
    logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    assert path.exists()


def test_session_logger_appends_jsonl(tmp_path):
    logger, path, sid = fresh_logger(tmp_path)
    logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    logger.log_flags(1, [make_flag("permno:10000")])
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["event"] == "candidates"
    assert lines[1]["event"] == "flags"


def test_session_logger_stores_session_id(tmp_path):
    logger, path, sid = fresh_logger(tmp_path, "my-session-id")
    logger.log_candidates(1, [], AS_OF)
    line = json.loads(path.read_text().splitlines()[0])
    assert line["session_id"] == "my-session-id"


def test_session_logger_log_accept_dismiss(tmp_path):
    logger, path, _ = fresh_logger(tmp_path)
    logger.log_accept(1, "permno:14593")
    logger.log_dismiss(1, "permno:10000")
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    events = {l["event"] for l in lines}
    assert "accept" in events
    assert "dismiss" in events


def test_session_logger_log_a_final(tmp_path):
    logger, path, _ = fresh_logger(tmp_path)
    logger.log_a_final({"permno:14593", "permno:20000"})
    line = json.loads(path.read_text().splitlines()[0])
    assert line["event"] == "a_final"
    assert set(line["a_final_ids"]) == {"permno:14593", "permno:20000"}


def test_session_logger_candidates_stored_completely(tmp_path):
    logger, path, _ = fresh_logger(tmp_path)
    cands = [make_candidate(f"permno:{i}") for i in range(5)]
    logger.log_candidates(1, cands, AS_OF)
    line = json.loads(path.read_text().splitlines()[0])
    assert len(line["candidates"]) == 5
    assert all("canonical_id" in c for c in line["candidates"])
    assert all("salience" in c for c in line["candidates"])
    assert all("coverage" in c for c in line["candidates"])


def test_session_logger_flags_stored_completely(tmp_path):
    logger, path, _ = fresh_logger(tmp_path)
    flags = [make_flag(f"permno:{i}") for i in range(3)]
    logger.log_flags(1, flags)
    line = json.loads(path.read_text().splitlines()[0])
    assert len(line["flags"]) == 3
    assert all("reason" in f for f in line["flags"])


def test_session_logger_multi_session_same_file(tmp_path):
    """Two sessions in the same log file should not interfere."""
    path = tmp_path / "sessions.jsonl"
    for sid in ("session-A", "session-B"):
        logger = SessionLogger(path, sid)
        logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)

    lines = [json.loads(l) for l in path.read_text().splitlines()]
    session_ids = {l["session_id"] for l in lines}
    assert "session-A" in session_ids
    assert "session-B" in session_ids


# ---------------------------------------------------------------------------
# score_session — metric computation
# ---------------------------------------------------------------------------

def test_score_session_perfect(tmp_path):
    """All A_final names are flagged and covered → perfect score."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000", "permno:20000"],
        flag_ids=["permno:10000", "permno:20000"],
        a_final_ids=["permno:10000", "permno:20000"],
    )
    scores = score_session(log_path, "s1")
    assert scores["precision"] == pytest.approx(1.0)
    assert scores["recall"]    == pytest.approx(1.0)
    assert scores["f_beta"]    == pytest.approx(1.0)


def test_score_session_zero_precision(tmp_path):
    """Flags include no A_final names → precision=0, recall=0."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000", "permno:99999"],
        flag_ids=["permno:99999"],
        a_final_ids=["permno:10000"],
    )
    scores = score_session(log_path, "s1")
    assert scores["precision"] == pytest.approx(0.0)
    assert scores["recall"]    == pytest.approx(0.0)
    assert scores["f_beta"]    == pytest.approx(0.0)


def test_score_session_partial_recall(tmp_path):
    """Only one of two A_final names is flagged."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000", "permno:20000"],
        flag_ids=["permno:10000"],
        a_final_ids=["permno:10000", "permno:20000"],
    )
    scores = score_session(log_path, "s1")
    assert scores["precision"] == pytest.approx(1.0)
    assert scores["recall"]    == pytest.approx(0.5)
    assert 0.0 < scores["f_beta"] < 1.0


def test_score_session_no_a_final(tmp_path):
    """No a_final logged → precision/recall/f_beta are None."""
    logger, path, sid = fresh_logger(tmp_path)
    logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    logger.log_flags(1, [make_flag("permno:10000")])
    scores = score_session(path, sid)
    assert scores["f_beta"]    is None
    assert scores["precision"] is None
    assert scores["recall"]    is None


def test_score_session_time_to_coverage_single_turn(tmp_path):
    """All A_final names appear in candidates on turn 1."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000", "permno:20000"],
        flag_ids=["permno:10000"],
        a_final_ids=["permno:10000", "permno:20000"],
        n_turns=1,
    )
    scores = score_session(log_path, "s1")
    assert scores["time_to_coverage"] == 1


def test_score_session_time_to_coverage_multiple_turns(tmp_path):
    """A_final name appears in candidates for the first time on turn 2."""
    path = tmp_path / "sessions.jsonl"
    logger = SessionLogger(path, "s2")
    # Turn 1: only permno:10000
    logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    logger.log_flags(1, [make_flag("permno:10000")])
    # Turn 2: both
    logger.log_candidates(2, [make_candidate("permno:10000"), make_candidate("permno:20000")], AS_OF)
    logger.log_flags(2, [make_flag("permno:10000")])
    logger.log_a_final({"permno:10000", "permno:20000"})

    scores = score_session(path, "s2")
    assert scores["time_to_coverage"] == 2


def test_score_session_time_to_coverage_none_when_uncovered(tmp_path):
    """Some A_final name never appears in candidates → time_to_coverage=None."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000"],    # permno:99999 never appears
        flag_ids=["permno:10000"],
        a_final_ids=["permno:10000", "permno:99999"],
    )
    scores = score_session(log_path, "s1")
    assert scores["time_to_coverage"] is None
    assert scores["n_covered_in_candidates"] == 1


def test_score_session_accept_precision(tmp_path):
    """2 accepts, 1 dismiss → accept_precision = 2/3."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000", "permno:20000", "permno:30000"],
        flag_ids=["permno:10000", "permno:20000", "permno:30000"],
        a_final_ids=["permno:10000"],
        accepts=["permno:10000", "permno:20000"],
        dismisses=["permno:30000"],
    )
    scores = score_session(log_path, "s1")
    assert scores["accept_precision"] == pytest.approx(2 / 3)


def test_score_session_no_accept_dismiss(tmp_path):
    """No accept/dismiss events → accept_precision is None."""
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000"],
        flag_ids=["permno:10000"],
        a_final_ids=["permno:10000"],
    )
    scores = score_session(log_path, "s1")
    assert scores["accept_precision"] is None


def test_score_session_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        score_session(tmp_path / "nonexistent.jsonl", "s1")


def test_score_session_only_own_session(tmp_path):
    """score_session ignores events from other sessions in the same file."""
    path = tmp_path / "sessions.jsonl"
    # Session A: flags hit A_final perfectly
    logger_a = SessionLogger(path, "session-A")
    logger_a.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    logger_a.log_flags(1, [make_flag("permno:10000")])
    logger_a.log_a_final({"permno:10000"})
    # Session B: flags miss A_final entirely
    logger_b = SessionLogger(path, "session-B")
    logger_b.log_candidates(1, [make_candidate("permno:99999")], AS_OF)
    logger_b.log_flags(1, [make_flag("permno:99999")])
    logger_b.log_a_final({"permno:10000"})

    scores_a = score_session(path, "session-A")
    scores_b = score_session(path, "session-B")
    assert scores_a["precision"] == pytest.approx(1.0)
    assert scores_b["precision"] == pytest.approx(0.0)


def test_score_session_returns_expected_keys(tmp_path):
    log_path = write_full_session(
        tmp_path, "s1",
        candidate_ids=["permno:10000"],
        flag_ids=["permno:10000"],
        a_final_ids=["permno:10000"],
        accepts=["permno:10000"],
    )
    scores = score_session(log_path, "s1")
    required_keys = {
        "session_id", "beta", "f_beta", "precision", "recall",
        "time_to_coverage", "accept_precision",
        "n_flags", "n_accepts", "n_dismisses", "n_a_final", "n_covered_in_candidates",
    }
    assert required_keys.issubset(scores.keys())


def test_score_session_deduplicates_flags_across_turns(tmp_path):
    """Flags from different turns with the same canonical_id should not double-count."""
    path = tmp_path / "sessions.jsonl"
    logger = SessionLogger(path, "s1")
    flag = make_flag("permno:10000")
    logger.log_candidates(1, [make_candidate("permno:10000")], AS_OF)
    logger.log_flags(1, [flag])
    logger.log_candidates(2, [make_candidate("permno:10000")], AS_OF)
    logger.log_flags(2, [flag])          # same flag, different turn
    logger.log_a_final({"permno:10000"})

    scores = score_session(path, "s1")
    assert scores["n_flags"] == 1        # de-duplicated
    assert scores["precision"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# aggregate_sessions
# ---------------------------------------------------------------------------

def test_aggregate_sessions_mean_f_beta(tmp_path):
    # Session 1: precision=1.0, recall=1.0 → f_beta=1.0
    write_full_session(tmp_path, "s1",
                       candidate_ids=["permno:10000"], flag_ids=["permno:10000"],
                       a_final_ids=["permno:10000"])
    # Session 2: precision=0.0, recall=0.0 → f_beta=0.0
    write_full_session(tmp_path, "s2",
                       candidate_ids=["permno:20000"], flag_ids=["permno:20000"],
                       a_final_ids=["permno:10000"])

    agg = aggregate_sessions(tmp_path / "sessions.jsonl", ["s1", "s2"])
    assert agg["n_sessions"] == 2
    assert agg["mean_f_beta"] == pytest.approx(0.5)


def test_aggregate_sessions_median_time_to_coverage(tmp_path):
    path = tmp_path / "sessions.jsonl"
    # s1: time_to_coverage = 1
    write_full_session(tmp_path, "s1",
                       candidate_ids=["permno:10000"], flag_ids=["permno:10000"],
                       a_final_ids=["permno:10000"])
    # s2: need multi-turn setup
    logger2 = SessionLogger(path, "s2")
    logger2.log_candidates(1, [], AS_OF)
    logger2.log_candidates(2, [make_candidate("permno:10000")], AS_OF)
    logger2.log_flags(2, [])
    logger2.log_a_final({"permno:10000"})

    agg = aggregate_sessions(path, ["s1", "s2"])
    assert agg["median_time_to_coverage"] in (1, 2, 1.5)  # between 1 and 2


def test_aggregate_sessions_no_sessions(tmp_path):
    path = tmp_path / "sessions.jsonl"
    path.touch()
    agg = aggregate_sessions(path, [])
    assert agg["n_sessions"] == 0
