---
name: run-pipeline
description: >
  Runs the Blind Spot v0.5 pipeline — Lane B candidate generation, entity expansion, and
  flagging — against a live Neo4j + WRDS environment. Use this skill whenever the user types
  /run-pipeline, asks to run the blind spot pipeline, wants to generate flags or candidates,
  or asks to score a session. The skill begins by asking whether the user wants to provide
  all arguments up front or be guided interactively stage by stage.
---

# /run-pipeline

This skill orchestrates the Blind Spot v0.5 morning copilot pipeline. It always starts
by asking the user which mode they prefer, then runs the appropriate flow.

## Step 1 — Ask mode preference

Use AskUserQuestion (the built-in question tool) with a single question:

> "How would you like to run the pipeline?"
>
> Options:
> - **Specify all arguments now** — I'll collect everything upfront and run the pipeline in one shot.
> - **Guide me interactively** — Walk through each stage with explanations; good for first runs or exploration.

Branch based on the answer.

---

## Mode A: Up-front (all arguments at once)

Collect these fields in a single AskUserQuestion (or a series of 2-4 question batches if the
single form would be overwhelming):

| Parameter | What to ask | Default |
|---|---|---|
| `as_of` | Session date (YYYY-MM-DD). This is T0 — all data is strictly before this date. | today |
| `universe` | Comma-separated permno IDs (e.g. `permno:14593, permno:10000`). Leave blank to use all :Security nodes already in the graph. | graph nodes |
| `k` | Max flags to surface (top-k by salience). | 20 |
| `d_e` | Entity expansion depth (1 = direct neighbors only, 2 = two hops). | 2 |
| `a_final` | Analyst's name list for today: comma-separated permnos. | (required) |
| `session_id` | Session ID for the eval log (e.g. `2023-12-29-ryan`). | `{as_of}-user` |
| `catalyst_events` | Optional: comma-separated `permno:DATE` pairs for named catalysts (e.g. `permno:14593:2024-01-10`). | none |

Once collected, run the full pipeline:

```
source .venv/bin/activate

# Stage 1: Lane B candidates
python run_candidate_generator.py --as_of {as_of} [--universe {universe}] [--events {events}]

# Stage 2: Flags
python run_flagger.py --as_of {as_of} --a_final {a_final} --k {k} --d_e {d_e}

# Stage 3: Log + score
python - <<'EOF'
from pathlib import Path
from datetime import date
from blind_spot.eval import SessionLogger, score_session
# ... (see logging section below)
EOF

# Stage 4: Persist flags + open live preview (recipe in "Stage 5 — Live preview" below)
```

Print section headers and a one-line summary after each stage (edges found, candidates ranked,
flags surfaced). At the end print the Fβ score block, then run the **Live preview** step
(see "Stage 5 — Live preview" below) to write the FlagSession to Neo4j and open the browser view.

---

## Mode B: Interactive (stage-by-stage)

Walk through four stages. After each stage, pause and summarize what happened before proceeding.
Explain the *purpose* of each stage so the analyst understands what they're looking at.

---

### Stage 1 — Entity graph

**Explain:** The entity graph is the structural map of which companies are connected to which.
It's built once (or refreshed periodically) from TNIC competitor edges, Compustat supply-chain
filings, and CRSP return co-movement. The flagger uses this graph to expand the analyst's seed
list into a neighborhood.

Ask:

> "Do you want to reload any graph layers, or use the existing graph as-is?"
>
> Options:
> - **Use existing graph** (fastest — skip all three load scripts)
> - **Reload co-movement only** (most likely to be stale — CRSP window updates daily)
> - **Reload all three layers** (TNIC + segments + co-movement; takes several minutes)

Run the appropriate scripts based on the answer:

```bash
source .venv/bin/activate

# Co-movement only
python run_comovement_load.py

# All layers
python run_tnic_load.py
python run_segment_load.py
python run_comovement_load.py
```

After running, print the stats summary from each script.

---

### Stage 2 — Lane B: candidate generation

**Explain:** Lane B ranks the full stock universe by how much the options market is pricing in —
using implied volatility rank within a sector×size bucket, and a straddle-implied move for
named catalyst dates. It runs over the entire universe and is analyst-independent.

Ask (collect all three at once):

1. **Session date** (`as_of`, YYYY-MM-DD, default today): This is T0 — no data after this date is used.
2. **Trailing IV window** (`window_days`, default 252): How many trading days of IV history to rank against.
3. **Catalyst events** (optional, `permno:DATE` pairs): Any earnings or event dates you know about. The straddle-implied move is computed for the first expiry after the catalyst.

Then ask about the universe:

> "Which stocks should Lane B scan?"
>
> - **All :Security nodes in the graph** (recommended — covers your full coverage universe)
> - **Specify a custom list** — enter comma-separated permno IDs

Run:

```bash
source .venv/bin/activate
python run_candidate_generator.py \
  --as_of {as_of} \
  --window_days {window_days} \
  [--events {permno:date ...}] \
  [--universe {permno ...}]
```

Print the top 10 candidates by salience as a table (canonical_id, measure, iv_rank, salience).

---

### Stage 3 — Flagger: expansion and blind spots

**Explain:** The flagger takes the complement. It expands your seed list (fbrain pages with
ticker tags) into a structural neighborhood using the entity graph, then finds candidates from
Lane B that sit inside that neighborhood but are *absent* from your list. Each flag includes
the shortest entity path from a seed to the flagged name and a human-readable reason.

Ask (collect all at once):

1. **Your name list today** (`a_final`): comma-separated permnos for names you're already watching.
   This is what you're comparing against — the flags are what's missing from this list.
2. **Max flags** (`k`, default 20): How many flags to surface. Fewer = higher precision.
3. **Expansion depth** (`d_e`, default 2): How many hops from your seeds to look.
   1 = direct competitors/suppliers only; 2 = peers-of-peers. Start with 2.

Run:

```bash
source .venv/bin/activate
python run_flagger.py \
  --as_of {as_of} \
  --a_final {a_final_joined} \
  --k {k} \
  --d_e {d_e}
```

Print each flag as:
```
[permno:XXXXX]  salience=0.87  path: permno:A → permno:B → permno:XXXXX
  Reason: NVDA supplies XXXXX (revenue fraction: 0.12, as_of 2023-03-15)
```

Ask the user to mark each flag as **accept** or **dismiss** (or skip).

---

### Stage 4 — Log and score

**Explain:** Every session is logged to a JSONL trajectory file. This lets you score precision
and recall after the fact (once you know what names you actually ended up covering), and the
logs are the dataset for future policy learning.

Ask:

1. **Session ID** (default `{as_of}-user`): Used to identify this session in the log.
2. **Log path** (default `logs/sessions.jsonl`): Where to append the session.

Run inline Python to log candidates, flags, accepts/dismisses, and a_final, then score:

```python
from pathlib import Path
from blind_spot.eval import SessionLogger, score_session

logger = SessionLogger(Path("{log_path}"), session_id="{session_id}")
logger.log_candidates(turn=1, candidates=candidates, as_of=as_of)
logger.log_flags(turn=1, flags=flags)
for flag_id in accepted_flags:
    logger.log_accept(turn=1, flag_id=flag_id)
for flag_id in dismissed_flags:
    logger.log_dismiss(turn=1, flag_id=flag_id)
logger.log_a_final(a_final=a_final_set)

scores = score_session(Path("{log_path}"), "{session_id}", beta=0.5)
```

Print the scores block:
```
=== Session scores ===
  f_beta (β=0.5):        0.82
  precision:             0.91
  recall:                0.74
  time_to_coverage:      2     (turn at which all A_final names appeared in candidates)
  accept_precision:      0.80  (accepts / (accepts + dismisses))
  n_flags:               12
```

---

### Stage 5 — Live preview (browser)

**Explain:** The flags are persisted to Neo4j as a `(:FlagSession)-[:HAS_FLAG]->(:FlagItem)`
graph and rendered into a self-contained React HTML page. The page has two tabs — Flags
(sortable table) and Tearsheets (per-company cards with overview + LIVE NEWS headlines).

Ask once:

> "Open the live preview in your browser when this finishes?"
>
> - **Yes, open it**
> - **Just write the file, don't open**
> - **Skip the preview**

If anything but **Skip**, persist and render in one Python block (re-uses the `flags`,
`session_id`, and `as_of` variables already in scope from earlier stages):

```python
import os, subprocess
from neo4j import GraphDatabase
from blind_spot.flag_stream.persistence import persist_flags

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
)
try:
    n = persist_flags(
        driver, session_id="{session_id}", as_of=as_of,
        flags=flags, k={k}, d_e={d_e},
        database=os.getenv("NEO4J_DATABASE", "neo4j"),
    )
    print(f"persisted {{n}} flags to session '{{'{session_id}'}}'")
finally:
    driver.close()
```

Then run the preview (drop `--open` if the user picked "just write the file"):

```bash
python -m blind_spot.flag_stream.preview {session_id} --open
```

Print the resulting HTML path so the user can re-open it:
```
preview ready: /tmp/blind_spot_preview.html
```

**Tearsheets note:** Tearsheet cards (overview + LIVE NEWS) only populate if a tearsheet
has been attached to each FlagItem via `persist_tearsheet(...)`. The Polygon-backed
populator is not yet wired into this skill — for now, real runs will show the Flags tab
fully and the Tearsheets tab with "no overview attached" per card. Use the synthetic
seed for a full UI demo: `python -m blind_spot.flag_stream.seed_fake fake-2024-01-15`.

---

## Always

- Run all Python via `source .venv/bin/activate && python ...` (or activate once at the top
  and use the venv for all subsequent calls in the same shell session).
- Never hardcode credentials. WRDS auth reads from `~/.pgpass`; Neo4j credentials come from `.env`.
- Print a clear section header before each stage: `\n=== Stage N: <name> ===\n`
- If a script exits non-zero, print the stderr and ask the user how to proceed — don't silently
  continue or retry.
- Dates in all arguments use ISO format (YYYY-MM-DD).
- permno IDs always in canonical format: `permno:NNNNN` (no spaces around the colon).
