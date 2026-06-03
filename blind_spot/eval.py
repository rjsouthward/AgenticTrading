"""
Eval harness for Blind Spot v0.5 — Task 7.

Three tiers (BUILD.md §8):

  1. Snapshot reproducibility
       Tested in test_candidate_generator.py: identical input → identical ranking.

  2. Copilot accuracy vs A_final
       Precision-favoring Fβ (β=0.5) of surfaced flags against the analyst's resolved set,
       plus time-to-coverage: the turn at which the candidate list first covers all of A_final.

       F_β = (1 + β²) × prec × rec / (β² × prec + rec)

  3. Flag accept/dismiss precision
       accepts / (accepts + dismisses) within the session — the cheap same-day proxy
       that tunes k and β. Ex-post realized importance is detector territory: log it, don't optimize.

Everything is logged to a JSONL file so full trajectories are available for future RL of the
traversal policy. Each line is one event; the file is append-only.

Log event types:
  "candidates" — full Lane B ranked list for the turn (canonical IDs + salience + coverage)
  "flags"      — top-k flags surfaced to the analyst
  "accept"     — analyst accepted a flag
  "dismiss"    — analyst dismissed a flag
  "a_final"    — end-of-session resolved analyst list (the label)

Usage
-----
    logger = SessionLogger(Path("logs/sessions.jsonl"), session_id="2023-12-29-ryan")
    logger.log_candidates(turn=1, candidates=candidates, as_of=date(2023, 12, 29))
    logger.log_flags(turn=1, flags=flags)
    logger.log_accept(turn=1, flag_id="permno:14593")
    logger.log_a_final(a_final={"permno:14593", "permno:20000"})

    scores = score_session(Path("logs/sessions.jsonl"), "2023-12-29-ryan")
    # {"f_beta": 0.80, "precision": 0.90, "recall": 0.72, "time_to_coverage": 3, ...}
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blind_spot.candidate_generator import Candidate
    from blind_spot.flagger import Flag

log = logging.getLogger(__name__)

CanonicalId = str
_BETA_DEFAULT = 0.5


# ---------------------------------------------------------------------------
# Session logger
# ---------------------------------------------------------------------------

class SessionLogger:
    """
    Append-only JSONL logger for a single analyst session.

    Each call writes one event to the log file immediately (no buffering).
    Thread-safety is not guaranteed — use one logger per session.
    """

    def __init__(self, log_path: Path, session_id: str) -> None:
        self.log_path   = Path(log_path)
        self.session_id = session_id
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event: dict) -> None:
        event["session_id"] = self.session_id
        event["timestamp"]  = datetime.now(timezone.utc).isoformat()
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def log_candidates(
        self,
        turn: int,
        candidates: list["Candidate"],
        as_of: date,
    ) -> None:
        """Log the full Lane B ranked candidate list for this turn."""
        self._write({
            "event": "candidates",
            "turn":  turn,
            "as_of": str(as_of),
            "candidates": [
                {
                    "canonical_id": c.canonical_id,
                    "salience":     round(c.salience, 6),
                    "iv_rank":      round(c.iv_rank, 6) if c.iv_rank is not None else None,
                    "implied_move": round(c.implied_move, 6) if c.implied_move is not None else None,
                    "measure":      c.measure,
                    "coverage":     c.coverage,
                }
                for c in candidates
            ],
        })

    def log_flags(self, turn: int, flags: list["Flag"]) -> None:
        """Log the flags surfaced to the analyst for this turn."""
        self._write({
            "event": "flags",
            "turn":  turn,
            "flags": [
                {
                    "canonical_id":       f.canonical_id,
                    "salience":           round(f.salience, 6),
                    "on_entity_frontier": f.on_entity_frontier,
                    "on_thesis_frontier": f.on_thesis_frontier,
                    "reason":             f.reason,
                    "entity_path":        f.entity_path,
                }
                for f in flags
            ],
        })

    def log_accept(self, turn: int, flag_id: CanonicalId) -> None:
        """Record that the analyst accepted a flag."""
        self._write({"event": "accept", "turn": turn, "flag_id": flag_id})

    def log_dismiss(self, turn: int, flag_id: CanonicalId) -> None:
        """Record that the analyst dismissed a flag."""
        self._write({"event": "dismiss", "turn": turn, "flag_id": flag_id})

    def log_a_final(self, a_final: set[CanonicalId]) -> None:
        """Record the analyst's resolved name list at session end."""
        self._write({"event": "a_final", "a_final_ids": sorted(a_final)})


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _f_beta(precision: float, recall: float, beta: float) -> float:
    """Compute Fβ score. Returns 0.0 when precision + recall = 0."""
    b2 = beta ** 2
    denom = b2 * precision + recall
    if denom == 0.0:
        return 0.0
    return (1 + b2) * precision * recall / denom


def score_session(
    log_path: Path,
    session_id: str,
    beta: float = _BETA_DEFAULT,
) -> dict:
    """
    Compute eval metrics from a session log.

    Parameters
    ----------
    log_path   : path to the JSONL session log
    session_id : which session to score
    beta       : Fβ weight (default 0.5 = precision-favoring)

    Returns
    -------
    dict with keys:
      session_id, beta,
      f_beta, precision, recall        — None if A_final not logged
      time_to_coverage                 — int (turns) or None if not fully covered
      accept_precision                 — float or None if no accept/dismiss events
      n_flags, n_accepts, n_dismisses,
      n_a_final, n_covered_in_candidates
    """
    log_path = Path(log_path)
    if not log_path.exists():
        raise FileNotFoundError(f"Session log not found: {log_path}")

    # Read all events for this session
    events = []
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id") == session_id:
                events.append(ev)

    # Parse events by type
    candidate_turns: dict[int, list[CanonicalId]] = {}   # turn → list of canonical IDs
    flag_ids: set[CanonicalId] = set()                    # all flags surfaced this session
    accepts:  set[CanonicalId] = set()
    dismisses: set[CanonicalId] = set()
    a_final: set[CanonicalId] | None = None

    for ev in events:
        kind = ev.get("event")
        turn = ev.get("turn", 0)

        if kind == "candidates":
            ids = [c["canonical_id"] for c in ev.get("candidates", [])]
            candidate_turns[turn] = ids

        elif kind == "flags":
            for f in ev.get("flags", []):
                if f.get("canonical_id"):
                    flag_ids.add(f["canonical_id"])

        elif kind == "accept":
            fid = ev.get("flag_id")
            if fid:
                accepts.add(fid)

        elif kind == "dismiss":
            fid = ev.get("flag_id")
            if fid:
                dismisses.add(fid)

        elif kind == "a_final":
            a_final = set(ev.get("a_final_ids", []))

    # Accept/dismiss precision
    n_accepts   = len(accepts)
    n_dismisses = len(dismisses)
    total_decisions = n_accepts + n_dismisses
    accept_precision = n_accepts / total_decisions if total_decisions > 0 else None

    # Precision / Recall / Fβ vs A_final
    if a_final is None:
        f_beta_val = precision = recall = None
        n_a_final = 0
    else:
        n_a_final    = len(a_final)
        n_flags      = len(flag_ids)
        hits         = flag_ids & a_final

        if n_flags == 0:
            precision = None
        else:
            precision = len(hits) / n_flags

        if n_a_final == 0:
            recall = None
        else:
            recall = len(hits) / n_a_final

        if precision is None or recall is None:
            f_beta_val = None
        else:
            f_beta_val = _f_beta(precision, recall, beta)

    # Time-to-coverage: the turn at which the last A_final name was first seen in candidates
    time_to_coverage: int | None = None
    n_covered = 0
    if a_final is not None and a_final:
        first_seen: dict[CanonicalId, int] = {}
        for turn in sorted(candidate_turns.keys()):
            for cid in candidate_turns[turn]:
                if cid in a_final and cid not in first_seen:
                    first_seen[cid] = turn
        n_covered = len(first_seen)
        if len(first_seen) == len(a_final):
            time_to_coverage = max(first_seen.values())
        # else: some A_final names never appeared → None

    return {
        "session_id":              session_id,
        "beta":                    beta,
        "f_beta":                  round(f_beta_val, 6) if f_beta_val is not None else None,
        "precision":               round(precision,  6) if precision  is not None else None,
        "recall":                  round(recall,     6) if recall     is not None else None,
        "time_to_coverage":        time_to_coverage,
        "accept_precision":        round(accept_precision, 6) if accept_precision is not None else None,
        "n_flags":                 len(flag_ids),
        "n_accepts":               n_accepts,
        "n_dismisses":             n_dismisses,
        "n_a_final":               n_a_final if a_final is not None else 0,
        "n_covered_in_candidates": n_covered,
    }


# ---------------------------------------------------------------------------
# Multi-session aggregation
# ---------------------------------------------------------------------------

def aggregate_sessions(
    log_path: Path,
    session_ids: list[str],
    beta: float = _BETA_DEFAULT,
) -> dict:
    """
    Score multiple sessions and return macro-averaged metrics.

    Averages f_beta, precision, recall, accept_precision over sessions where
    those metrics are computable. time_to_coverage is reported as median.
    """
    scores = []
    for sid in session_ids:
        try:
            scores.append(score_session(log_path, sid, beta=beta))
        except Exception as exc:
            log.warning("eval: failed to score session %s: %s", sid, exc)

    if not scores:
        return {"n_sessions": 0}

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    def _median(vals):
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    return {
        "n_sessions":            len(scores),
        "beta":                  beta,
        "mean_f_beta":           _mean([s["f_beta"]           for s in scores]),
        "mean_precision":        _mean([s["precision"]        for s in scores]),
        "mean_recall":           _mean([s["recall"]           for s in scores]),
        "mean_accept_precision": _mean([s["accept_precision"] for s in scores]),
        "median_time_to_coverage": _median([s["time_to_coverage"] for s in scores]),
    }
