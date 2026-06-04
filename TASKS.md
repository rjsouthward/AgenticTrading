# Blind Spot — Tasks

Working task list. Architecture and spec live in [`BUILD.md`](BUILD.md);
this file tracks the *what's next* and incubates proposals before they earn a build slot.

**Status legend:** `[ ]` not started · `[~]` in progress · `[x]` done · `[-]` deferred
**Effort:** XS (<1h) / S (1-4h) / M (1-2d) / L (>2d) / XL (>1wk)

---

## Post-demo polish (highest priority)

The system runs end-to-end on current data via Polygon. These items remove the
remaining sharp edges before the next session.

- [ ] (XS) **Rotate the Polygon API key.** It was visible in the conversation
  transcript before the Bearer-auth fix. New key, paste it into `.env`, done.
- [ ] (S) **Winsorize signal z-scores.** The Polygon smoke produced flags with
  10σ z-scores on a handful of names — genuine extreme events or data artifacts,
  but either way they dominate the composite blend. Clip at ±5σ inside
  `signals.zscore_map()` before composition. One-line change + a test that an
  outlier doesn't drag a whole bucket.
- [ ] (S) **Wire Polygon into `/run-pipeline` by default.** The skill currently
  builds a `WrdsBarSource` implicitly via `wrds_conn`. Add a stage-2 question:
  "Backtest (CRSP) or current (Polygon)?" with current as the default when
  `as_of` is within the last 30 days. The Polygon path is faster and doesn't
  touch WRDS.
- [ ] (S) **Fix `put_page` so it accepts an `updated_at` override.** Today the
  fbrain MCP server stamps `updated_at = now()`, which makes retro demos
  (T0 in the past) silently invisible to `pull_seeds_from_fbrain` — we hit
  this during the 2023-12-29 demo and worked around it with a manual Cypher
  backdate. Add an optional `updated_at: str | None = None` arg to `put_page`;
  keep the default to `now()` for the live case.
- [ ] (S) **Bootstrap a `shrout_map` for Polygon.** `PolygonBarSource` accepts
  one but the smoke doesn't populate it, so market-cap bucketing in
  `_compute_salience` collapses to "everything in bucket 0." Pull
  permno→shrout from CRSP at bootstrap time alongside the ticker map, cache
  to JSON, refresh quarterly. Brings full salience resolution to the Polygon
  path.
- [ ] (XS) **Add a `data/` `.gitignore` carve-out.** `data/permno_ticker_map.json`
  is a useful cache but should not be committed. Make sure `data/` is ignored
  except for an explicit `.gitkeep`.

## Sprint: data freshness & integrity

- [ ] (S) **Permno→ticker cache freshness check.** Today the cache is "build once,
  never refresh." Add an `mtime` check that triggers a rebuild if the file is
  >90 days old, with a log line explaining what's happening.
- [ ] (M) **VTNIC data files.** Loader is implemented (`load_vtnic` in
  `graph_loader.py`), files not sourced. Download from Hoberg-Phillips Tuck site
  and run.
- [ ] (M) **Capital IQ Key Developments → catalyst events.** `generate()` accepts
  an `events: dict[permno, date]` arg but nothing populates it. Query
  `ciq_keydev` for the analyst's seeds + flag candidates over the next 30 days
  and pass as `events`. Activates the straddle-implied-move path in the
  composite.

## Backlog

- [ ] (M) **`Eval v0.6` proposal in this file** — implement once the design
  questions below are resolved. Highest payoff item but expensive without
  sessions to validate against.
- [ ] (M) **Trajectory replay test.** Freeze a JSONL session log as a fixture,
  assert `score_session()` returns identical numbers across versions. The
  determinism guarantee for the eval, complementing the snapshot test for
  `generate()`.
- [ ] (M) **Multi-day session.** Right now a "session" is a single morning's run.
  Real adoption will mean re-opening the same session across days as positions
  evolve. Decide whether to model that as separate session_ids or one rolling
  session — affects frame-lock metrics.
- [ ] (L) **Lane A retrieval.** Frozen Claude + Exa.ai news. The eval harness
  already supports it; the candidate-side ingestion is the missing piece.
  Out of scope until Lane B has real session data to compare against.
- [ ] (L) **Detector-mode log.** For each flag, after T+5 trading days, log
  whether attention persisted and whether a material news event mentioned
  the name. Not scored (copilot, not detector) — but the labeling pipeline
  is useful for later policy work.
- [ ] (XL) **Thesis graph.** Unsupervised clustering of mention-context
  embeddings; populates `Flag.on_thesis_frontier`. Composer already supports
  the intersection logic.
- [ ] (XL) **RL traversal policy.** The point of the trajectory log.
  Needs ≥hundreds of sessions before training is meaningful.

## Done (chronological)

- [x] Repo prep, `.claude/skills/` populated, WRDS MCP pinned (Task 0)
- [x] Entity resolution layer (Task 1)
- [x] TNIC graph load (Task 2)
- [x] Compustat segments → `:SUPPLIES` edges (Task 3)
- [x] CRSP co-movement edges (Task 4)
- [x] Candidate generator (Task 5)
- [x] Expansion + flagger (Task 6)
- [x] Eval harness v0.5 — `SessionLogger` + `score_session` (Task 7)
- [x] **Lane B Phase 1 refactor** — `BarSource` seam, attention signals, OM optional
- [x] `/run-pipeline` skill — interactive demo orchestration
- [x] **Lane B Phase 2** — `PolygonBarSource` live, Bearer auth, token-bucket rate limiter
- [x] Bug fixes: `nameendt → nameenddt` in stocknames query; `resolve_batch` signature
  in `pull_seeds_from_fbrain`
- [x] README, BUILD.md reconciled with shipped state

---

# Proposal: Eval v0.6

## Why now

The demo run on 2023-12-29 produced `f_beta=0.11, precision=0.10, recall=0.25`,
which read as "the system is barely working." It wasn't — the structural composition
of `A_final` mechanically capped the score. That's the kind of silent measurement
bug §9 of BUILD.md exists to prevent. Five real problems were hiding under it:

## Problems with the current eval

### Problem 1 — `A_final` includes seeds, but seeds can't be flagged

The flagger **correctly** excludes seeds from flags (a name you're already watching
isn't a blind spot). But the demo used `A_final = seeds ∪ accepts`, which means
the seeds appear in the denominator of recall while being structurally absent from
the numerator. In the demo, 6 seeds + 2 accepts = 8 `A_final` names; only 2 were
flaggable. Mechanical ceiling: `recall ≤ 2/8 = 0.25`, regardless of how good the
flagger is.

The fix: split into `A_seeds` (warm-start, what they came in watching) and
`A_added` (names added during the session — the actual deliverable). Score Fβ
against `A_added`. Keep `A_final = A_seeds ∪ A_added` for downstream uses
(coverage, frame-lock detection), but it's no longer the scoring target.

### Problem 2 — No counterfactual: "would Lane B have surfaced this without the flagger?"

A flag in Lane B's top-5 is a *much* weaker signal that the entity graph added
value than a flag that lived at Lane B rank #847 of 3,905 and was pulled forward
by the structural filter. The current eval can't distinguish them — both
contribute equally to precision.

This matters because the flagger is the structural-neighborhood filter; if accepts
correlate with high Lane B rank, the structural filter is doing real work. If
accepts correlate with low Lane B rank, the flagger is mostly re-ranking what
Lane B would have surfaced anyway.

### Problem 3 — Binary accept/dismiss collapses four very different signals

| Decision | What it means | Current encoding |
|---|---|---|
| Added to watchlist, opened research page | "Real catch, acting on it" | `accept` |
| Worth knowing, no action today | "Useful awareness, soft accept" | `accept` |
| Irrelevant to me | "Noise" | `dismiss` |
| I already knew about this | **Frame lock-in signal** | `dismiss` |
| Right name, wrong reason | "Surface OK, explanation broken" | accept or dismiss?? |

The `already-knew` vs `irrelevant` collapse is the worst — it makes frame-lock
literally invisible, even though §9 of BUILD.md identifies it as a hard guardrail.

### Problem 4 — No longitudinal frame-lock metric

§9 names "frame lock-in" as a guardrail: *"If `U_analyst` converges to a fixed
point (seeds → flags → seeds), the tool has started confirming priors instead
of finding blind spots."* But the harness has no metric for it. Run the system
for 100 sessions, slowly converge to "Nvidia + the same 12 SaaS names every
morning," score `f_beta = 0.85` the whole way — and the guardrail never fires.

### Problem 5 — Explanation quality is invisible

The reason text *is* the trust mechanism. "MRVL supplies NOK at 26% of FY2022
revenues" is qualitatively different from "co-moving with MRVL (return-based
structural link)," even when both correctly flag the same name. A right answer
with a misleading reason erodes adoption faster than a wrong answer with a
good explanation. The harness logs the reason string but doesn't score it.

## Proposal — the shape of v0.6

Five additions, ordered by payoff per unit of analyst friction.

### Addition 1 — Split `A_seeds` and `A_added`; score against `A_added`

**New logger method:**
```python
logger.log_a_added(a_added={"permno:14593", ...})
# the names added during the session that weren't in A_seeds at T0
logger.log_a_final(a_final={...})  # keep for back-compat; computed if absent
```

`A_seeds` comes free — `pull_seeds_from_fbrain` already produces it. The new
event records the deliverable.

**Scoring changes:**
```python
hits      = flag_ids ∩ a_added       # not a_final
precision = |hits| / |flag_ids|
recall    = |hits| / |a_added|
```

Time-to-coverage also moves to `a_added`. Coverage of seeds is irrelevant —
they're always covered at turn 1 because they *are* the seeds.

**Effort:** S. Backward-compatible — if no `a_added` event, fall back to current
behavior with a warning.

### Addition 2 — Counterfactual lift via Lane B rank

For each accepted flag, log the rank it held in the underlying candidate list:

```python
# In flag_blind_spots, attach .lane_b_rank to each Flag before logging
Flag(canonical_id="permno:59328", ..., lane_b_rank=847)
```

Then a new metric:

```
lift = mean( (lane_b_rank_of_accept - 1) / n_candidates  for accept in accepts )
```

- `lift ≈ 0`: every accepted flag was already in Lane B's top of the list. The
  flagger isn't adding much; an analyst with just Lane B's top-20 would have
  caught the same names.
- `lift ≈ 0.5`: accepted flags came from the median of Lane B. The structural
  filter is consistently *rescuing* names Lane B alone would have buried.
- `lift ≈ 1`: accepts came from the bottom — suspicious, probably means the
  flagger is overruling Lane B in ways that the analyst happens to like, but
  which won't generalize.

The expected healthy range is 0.1–0.3: flags should mostly come from the upper
half of Lane B, but the filter should regularly rescue names from rank 50–500.

**Effort:** S. Already-computable from existing data; the new field on `Flag` is
the only schema change.

### Addition 3 — Four-tier feedback replacing binary accept/dismiss

```python
logger.log_accept(turn=1, flag_id="...", kind="act")        # added to watchlist
logger.log_accept(turn=1, flag_id="...", kind="note")        # worth knowing, no action
logger.log_dismiss(turn=1, flag_id="...", kind="noise")      # irrelevant
logger.log_dismiss(turn=1, flag_id="...", kind="known")      # frame-lock signal
logger.log_dismiss(turn=1, flag_id="...", kind="bad_reason") # name OK, explanation wrong
```

Defaults preserve existing API: `log_accept` with no `kind` arg = `kind="act"`,
`log_dismiss` with no kind = `kind="noise"`. So existing tests don't change.

**New metrics:**
- `accept_act_rate = n_accept_act / (n_accept_act + n_dismiss_noise)` — strict precision
- `frame_lock_rate = n_dismiss_known / n_flags` — direct measurement of guardrail
- `explanation_quality = 1 - n_dismiss_bad_reason / n_flags` — surface OK, story broken

**Effort:** S. The hard part is the UI side — `/run-pipeline` needs to ask the
right follow-up question on dismiss. Two extra keystrokes for the analyst
(k/n/b after a dismiss).

### Addition 4 — Frame-lock metrics over a rolling window of N sessions

Once `dismiss_known` exists, frame-lock is directly measurable. Three metrics:

```python
def frame_lock_scores(log_path, session_ids, n_window=10):
    last_n = session_ids[-n_window:]
    all_flags     = union(flag_ids for sid in last_n)
    novelty_rate  = len(all_flags) / (k * len(last_n))
    # If every session surfaces the same 20 names, novelty_rate → 1/n_window.
    # If every session surfaces 20 totally new names, novelty_rate → 1.0.

    known_rate    = mean(n_dismiss_known / n_flags  for sid in last_n)
    # Direct: are analysts saying "I already knew" more often?

    seed_drift    = jaccard(
        union(a_added for sid in last_n[:5]),    # earlier half
        union(a_added for sid in last_n[5:])     # later half
    )
    # 0 = totally different names being added; 1 = identical sets.
    # Healthy is the middle; a drop toward 1.0 signals lock-in even if
    # the analyst can't articulate it.
```

**Effort:** M. Mostly aggregation logic; the data exists once Additions 1 and 3 land.

### Addition 5 — Bucket-level slicing

The global Fβ is one number across a multi-edge-type, multi-sector, multi-tier
system. Slicing reveals *which* parts work.

```python
score_session_sliced(log_path, session_id) -> {
    "by_reason_type": {
        "named_supply_chain":  {"precision": 0.45, "n_flags": 11, ...},
        "product_market_peer": {"precision": 0.12, "n_flags": 24, ...},
        "co_moving":           {"precision": 0.08, "n_flags": 13, ...},
    },
    "by_salience_quartile": {
        "q4_top": {"precision": 0.31, "accept_act_rate": 0.40, ...},
        "q3":     {"precision": 0.22, ...},
        "q2":     {"precision": 0.14, ...},
        "q1_bot": {"precision": 0.07, ...},
    },
    "by_sector_division": {
        "3 (manufacturing)": {...},
        "7 (services)":       {...},
        ...
    },
}
```

The pharma-noise problem we saw in the demo (BBIO/ARWR via MRVL TNIC) would
show up here as `by_reason_type["product_market_peer"]["precision"] = 0.05`
while `by_reason_type["named_supply_chain"]["precision"] = 0.80`. That tells
you exactly what to fix (over-broad TNIC edges across sectors), with evidence.

**Effort:** M. Reuses existing data, mostly group-by aggregation.

## Phased rollout

Don't ship v0.6 in one go. Each phase is independently testable and provides
its own value:

| Phase | Adds | Effort | Pre-req |
|---|---|---|---|
| **v0.5.1** | `a_added` split (Addition 1) | S | none |
| **v0.5.2** | Counterfactual lift (Addition 2) | S | none |
| **v0.5.3** | Four-tier feedback (Addition 3) | S | UI side in `/run-pipeline` |
| **v0.5.4** | Bucket slicing (Addition 5) | M | none |
| **v0.6**   | Frame-lock window (Addition 4) | M | v0.5.3 + ≥10 sessions logged |

Stop at v0.5.4 and you have most of the value; v0.6's frame-lock metrics
genuinely need session history to be meaningful.

## Open questions

- **Reason scoring vs. tagging.** Addition 3 captures explanation breakdown
  via `dismiss_bad_reason`, but doesn't distinguish "the edge type was wrong"
  vs "the source_span was misleading" vs "the wording was unclear." A 1–5
  reason score per flag would be richer but adds analyst friction. Decide
  after observing how often `dismiss_bad_reason` fires.
- **Conviction tiers in `A_added`.** Should the analyst label *why* a name
  was added (earnings catch, supply-chain readthrough, gut feeling)? Useful
  for later RL — high-conviction adds should weight more — but speculative
  until we see if analysts naturally write that down.
- **Cross-session learning.** Once we have a `dismiss_known` signal, should
  the system *suppress* repeat flags within a rolling window? Risk: the
  analyst's frame shifts and a previously-dismissed name becomes relevant.
  Probably yes, but with a 30-day "memory" rather than permanent suppression.

## Out of scope for v0.6

- **Detector-mode realized-importance scoring.** "Did this name actually move
  in the next 5 days?" Logged but not scored. Crossing into that territory
  changes the system from copilot to predictor; the principle locks in §0
  of BUILD.md.
- **A/B testing different signal weights.** Once v0.5.4 lands, the slicing
  metrics will tell you which signals carry weight per bucket. *Tuning* is
  a follow-up project, not an eval expansion.
