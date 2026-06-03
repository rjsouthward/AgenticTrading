# Blind Spot v0.5

A morning copilot for financial analysts. Given the day's news and the analyst's own name list, it surfaces names the analyst is at risk of **omitting** — names the market is moving on that are structurally connected to the analyst's world but absent from their list.

**Copilot, not detector.** The goal is to reproduce the analyst's attention faster, not to predict the market.

---

## How it works

Three lanes run in parallel at session start (`T0`):

```
  analyst seeds (fbrain) ──→ ENTITY GRAPH ──→ U_analyst (expanded neighbourhood)
                                                        │
  OptionMetrics snapshot ──→ LANE B ──→ Candidate list  │
   (IV rank + straddle)                                  │
                                                        ▼
                                              FLAGGER: market-lit
                                              ∧ in U_analyst
                                              ∧ absent from analyst list
                                                        │
                                                        ▼
                                              top-k FLAGS with reason paths
```

**Lane B** (analyst-independent) ranks the full universe by implied volatility elevation — IV rank within a sector × size bucket, with a straddle-based implied move for named catalysts. It runs over the whole universe and logs everything.

**Flagger** takes the complement: names that Lane B lights up, that are in the analyst's structural neighbourhood, but that the analyst hasn't named. Each flag carries an entity path and a human-readable reason that leads with the most explainable edge (named supply-chain relationship > product-market peer > co-moving).

**Precision over recall.** A false flag costs the analyst's attention at 6am. The whole system uses Fβ with β = 0.5 to bias toward fewer, surer flags.

---

## Entity graph

The structural neighbourhood is built from four edge types in Neo4j (`:Security` nodes, separate from the fbrain page DAG):

| Relationship | Source | Direction | Weight |
|---|---|---|---|
| `:COMPETES_WITH` | Hoberg-Phillips TNIC | undirected | cosine similarity in product space |
| `:VERTICAL` | Hoberg-Phillips VTNIC | undirected | vertical relatedness score |
| `:SUPPLIES` | Compustat Historical Segments | directed (supplier → customer) | revenue fraction; `as_of` = SEC filing date |
| `:COMOVES_WITH` | CRSP daily returns | undirected | trailing partial correlation (VW market stripped) |

All edges use CRSP `permno` as the canonical identifier (`"permno:14593"` format). Every value is point-in-time — knowable strictly before `T0`.

---

## Prerequisites

- **Python 3.12+** with a virtual environment
- **Neo4j** (Community or Enterprise) running locally on `bolt://localhost:7687`
- **WRDS** subscription with access to: `crsp_a_stock`, `crsp_a_ccm`, `comp_segments_hist_daily`, `wrdsapps_link_supplychain`, `optionm_all`, `wrdsapps_link_crsp_optionm`, `wrdssec_all`
- **WRDS credentials** stored in `~/.pgpass` (see [WRDS pgpass setup](https://wrds-www.wharton.upenn.edu/pages/support/programming-wrds/programming-python/))
- **TNIC3 flat files** from [Hoberg-Phillips Data Library](https://hobergphillips.tuck.dartmouth.edu/) placed in `tnic3_data/`

---

## Installation

```bash
git clone <repo-url>
cd fbrain
python -m venv .venv
source .venv/bin/activate
pip install -e ".[pipeline]"
```

Copy `.env.example` to `.env` and fill in your credentials:

```bash
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password
NEO4J_DATABASE=neo4j
WRDS_USERNAME=your_wrds_username
```

WRDS authentication uses `~/.pgpass` for non-interactive credential lookup — no passwords in `.env`. The format is:

```
wrds-pgdata.wharton.upenn.edu:9737:wrds:your_wrds_username:your_password
```

---

## Building the entity graph (one-time setup)

Run these scripts once to populate the Neo4j entity graph. Each is idempotent (`MERGE` writes).

```bash
# 1. TNIC competitor edges (download TNIC3 flat files first)
python run_tnic_load.py

# 2. Compustat customer-supplier edges (FY2022 example)
python run_segment_load.py

# 3. CRSP co-movement edges (trailing 252-day partial correlations)
python run_comovement_load.py
```

Each script logs progress and prints a stats summary on completion. Expect the co-movement load to take several minutes for a large universe — CRSP `dsf` is queried in bulk.

---

## Daily workflow

### 1. Generate candidates (Lane B)

```python
from datetime import date
from blind_spot.candidate_generator import generate

candidates = generate(
    universe=["permno:14593", "permno:10000", ...],  # your coverage universe
    as_of=date(2023, 12, 29),
    events={"permno:14593": date(2024, 1, 10)},      # optional catalyst dates
    wrds_conn=conn,
    window_days=252,
)
# Returns list[Candidate] sorted descending by salience
```

`Candidate` fields: `canonical_id`, `implied_move` (straddle/S at catalyst expiry), `iv_rank` (position in trailing window), `measure` (`"straddle"` | `"iv_rank"` | `"iv_level"`), `salience` (within sector×size bucket), `coverage`, `as_of`.

### 2. Pull seeds and flag blind spots

```python
from blind_spot.flagger import pull_seeds_from_fbrain, flag_blind_spots

# Seeds = fbrain pages with ticker tags updated before T0
seeds = pull_seeds_from_fbrain(driver, as_of=date(2023, 12, 29), wrds_conn=conn)

# Analyst's resolved name list for today
a_final = {"permno:14593", "permno:20000"}

flags = flag_blind_spots(
    candidates=candidates,
    a_final=a_final,
    seeds=seeds,
    driver=driver,
    k=20,
    d_e=2,          # expansion depth (1 = direct peers only)
)
```

`Flag` fields: `canonical_id`, `salience`, `on_entity_frontier`, `on_thesis_frontier` (always `False` in v0.5), `entity_path` (canonical IDs from a seed to the flagged name), `reason` (human-readable, leads with named supply-chain edge when available).

**Seed convention:** tag fbrain pages with the primary ticker, e.g. `["NVDA", "semiconductors"]`. The flagger resolves uppercase 1–5 character tags to permnos via WRDS stocknames.

### 3. Log the session

```python
from pathlib import Path
from blind_spot.eval import SessionLogger

logger = SessionLogger(Path("logs/sessions.jsonl"), session_id="2023-12-29-ryan")
logger.log_candidates(turn=1, candidates=candidates, as_of=date(2023, 12, 29))
logger.log_flags(turn=1, flags=flags)

# As the session progresses:
logger.log_accept(turn=1, flag_id="permno:99999")
logger.log_dismiss(turn=1, flag_id="permno:11111")

# At end of session:
logger.log_a_final(a_final={"permno:14593", "permno:20000"})
```

### 4. Score a session

```python
from blind_spot.eval import score_session

scores = score_session(Path("logs/sessions.jsonl"), "2023-12-29-ryan", beta=0.5)
# {
#   "f_beta": 0.82,
#   "precision": 0.91,
#   "recall": 0.74,
#   "time_to_coverage": 2,       # turn at which all A_final names appeared in candidates
#   "accept_precision": 0.80,    # accepts / (accepts + dismisses)
#   "n_flags": 12,
#   ...
# }
```

---

## Running tests

```bash
# All offline unit tests (no credentials needed)
pytest

# Including live WRDS integration tests (requires ~/.pgpass)
pytest -m integration
```

157 tests pass offline across all seven modules.

---

## Project structure

```
blind_spot/
├── entity_resolution.py      # permno hub: gvkey/secid/ticker → canonical ID
├── graph_loader.py           # TNIC/VTNIC flat files → :COMPETES_WITH/:VERTICAL edges
├── segment_loader.py         # Compustat segments → :SUPPLIES edges (filing-date stamped)
├── comovement_loader.py      # CRSP returns → :COMOVES_WITH partial-correlation edges
├── candidate_generator.py    # OptionMetrics IV surface → Candidate list (Lane B)
├── flagger.py                # gbrain seeds → entity expansion → Flag list
├── eval.py                   # SessionLogger + Fβ / time-to-coverage scoring
├── fbrain/
│   └── mcp_server.py         # fbrain knowledge-base MCP server (:Page graph)
└── tests/
    ├── test_entity_resolution.py
    ├── test_graph_loader.py
    ├── test_segment_loader.py
    ├── test_comovement_loader.py
    ├── test_candidate_generator.py
    ├── test_flagger.py
    ├── test_eval.py
    └── test_wrds_smoke.py    # live integration tests (marked, excluded by default)

run_tnic_load.py              # one-off: load TNIC competitor edges
run_segment_load.py           # one-off: load Compustat segment edges
run_comovement_load.py        # one-off: load CRSP co-movement edges
run_candidate_generator.py    # demo: run Lane B against live data
run_flagger.py                # demo: run full pipeline against live data
```

---

## Key design principles

**Point-in-time correctness is a hard invariant.** Every value must be knowable strictly before the session timestamp `T0`. Specifically: OptionMetrics IV snapshot at `T0`; CRSP co-movement window ends before `T0`; TNIC uses the vintage available before `T0` (annual, available ~July); Compustat segment edges stamped to SEC filing date (not fiscal year-end); fbrain seeds snapshotted before the session writes back.

**Single canonical ID space.** Everything resolves to `permno` before any set operation or join. A silent ID mismatch corrupts the eval. The canonical format is `"permno:14593"`.

**Generate wide, flag narrow.** Lane B scans the full universe and logs the complete ranked list. The flagger filters to the analyst's expanded structural neighbourhood. The universe is a parameter.

**Precision over recall.** Fβ with β < 1. A false flag spends the analyst's attention at 6am — that is the scarcest resource in the loop.

---

## What's not in v0.5

- **Thesis graph** — interface stub present, always returns empty. `Flag.on_thesis_frontier` is always `False`. Planned as unsupervised clustering of mention-context embeddings.
- **RL traversal policy** — the frozen frontier model is the stand-in. The full trajectory logs written by `SessionLogger` are the dataset that makes later policy learning possible.
- **Lane A** (copilot retrieval) — analyst-facing LLM search scored against `A_final`. Exa.ai ingestion not yet wired up.
- **VTNIC edges** — `load_vtnic()` is implemented; data files not yet sourced.
- **FactSet Revere** — not licensed. Named supply-chain layer uses Compustat Segments only.
