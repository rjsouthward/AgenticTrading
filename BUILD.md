# Blind Spot v0.5 — Build Specification

> A morning **copilot** for a financial analyst. Given the day's news and the analyst's
> own resolved name list, it surfaces names the analyst is at risk of *omitting* — names
> the market is moving on that are structurally connected to the analyst's world but
> absent from their list. **Copilot, not detector.**

This document is the build backbone. Operational guardrails live as Claude Code skills
under `.claude/skills/` (`point-in-time-discipline`, `entity-resolution`, `run-pipeline`)
and auto-load when relevant. Read them before touching any data source.

---

## Status (as of demo)

The seven original build tasks are all implemented (see §10). Two design changes
landed *after* the original spec, both motivated by discovering the OptionMetrics
ceiling silently capped `as_of`:

1. **Lane B is now bar-driven, options-optional** (see §6). The candidate generator was
   originally specified as an OptionMetrics → IV-rank pipeline, which made an OM data
   load the binding `as_of` ceiling and silently dropped non-optionable names from the
   universe. The implementation routes salience through a composite **attention score**
   built from daily bars (realized-vol spike, abnormal volume, idiosyncratic dislocation,
   overnight gap); IV/straddle is folded in as one *enrichment* term where coverage
   exists, never as a gate.
2. **A `BarSource` seam** isolates the data origin from the signal layer. v0.5 ships
   with two implementations:
   - `WrdsBarSource` (CRSP `dsf`/`dsi`) — the source the eval harness needs for
     survivorship-free historical replay (and for the entity-graph load).
   - `PolygonBarSource` (Polygon.io REST API) — the production source for
     *current* daily bars. Includes a token-bucket rate limiter, Bearer-auth headers
     (the API key never lives in URLs), and a CRSP-bootstrapped permno→ticker cache.

Concrete validation, both paths:
- **CRSP path** at `as_of=2025-01-01` (above the OM ceiling) with options enrichment off:
  Lane B produced a full 117-candidate ranking from bar signals alone. The original
  OM-gated pipeline produced 0.
- **Polygon path** at `as_of = today`: Lane B returned 180 candidates from yesterday's
  close, ~2 minutes for the full 200-name universe at Starter-tier (100 req/min) rate.
  Zero WRDS calls in the second-and-later runs (the permno→ticker map is cached as JSON).
  Top of the ranking: HPE / GTLB / ZS / SNOW / OKTA / HUBS / HPQ — the day's
  SaaS-and-enterprise-tech move surfaced from raw bars without any human input.

The rest of the original spec (entity graph, expansion, flagger, eval) shipped
substantively as designed. fbrain seed conventions and a few bug fixes are folded
into §7 and §10.

---

## 0. Scope

**In scope (v0.5, shipped):**
- ✅ Canonical identifier layer (`permno` hub).
- ✅ Entity graph: competitor + vertical + named customer-supplier + co-movement edges.
- ✅ Bar-driven candidate generator with options enrichment (was originally "options-based";
  see §6 for the rationale behind the change).
- ✅ A `BarSource` seam so the same Lane B runs against CRSP (eval) or Polygon (production).
- ✅ fbrain-seeded structural expansion → per-analyst universe.
- ✅ Flagger: surface the complement (market-lit ∧ structurally-near ∧ absent-from-list).
- ✅ Eval harness: precision-favoring Fβ + time-to-coverage vs the analyst list.
- ✅ Full JSONL trajectory logging (substrate for any later RL or detector work).

**Explicitly OUT of scope for v0.5 (do not build, do not scaffold beyond a stub):**
- RL training of any policy. A **frozen frontier model** is the traversal/retrieval policy.
- The thesis graph (interface stub only; returns empty).
- Detector mode / ex-post realized-importance scoring.
- FactSet Revere supply chain (not licensed — see §3).
- Any "match the commentary" prose-grading reward.

**Non-negotiable principles (the "why," locked):**
1. Label = the analyst's own resolved set `A_final`. We are reproducing *their* attention faster, not predicting the market.
2. **Precision over recall everywhere** (Fβ with β < 1). A false flag spends the analyst's attention at 6am; that is the scarcest resource in the loop.
3. **Structure is assembled from priors + rough models, never learned.** The graph is the *environment*; the only learnable thing (later, not now) is the traversal *policy*, and only against the verifiable copilot objective.
4. **Point-in-time correctness is a hard invariant**, not a feature. Every value must be knowable strictly before the session timestamp `T0`. See the `point-in-time-discipline` skill.

---

## 1. Architecture

Three lanes plus a composer:

```
                          ┌─────────────────────────────┐
  analyst prompt + news → │  LANE A: copilot retrieval   │ → names the analyst reaches
   (Exa, not yet wired)   │  (frozen model, agentic)     │
                          └─────────────────────────────┘
                          ┌─────────────────────────────┐
  BarSource @ T0       →  │  LANE B: candidate generator │ → ranked salient names
   (CRSP now;             │  composite attention score   │   over the universe
    Polygon later)        │  + optional OM enrichment    │
                          └─────────────────────────────┘
        seeds (fbrain) ─→ expand over ENTITY GRAPH ─→ U_analyst (per-analyst universe)
                          ┌─────────────────────────────┐
                          │   FLAGGER (composer)         │ → top-k flags, by confidence,
                          │   complement within U_analyst│   each with an explanation path
                          └─────────────────────────────┘
```

- **Lane A** is the analyst-facing copilot retrieval (their own search, scored vs `A_final`). Frozen model. *Not wired in v0.5 — Exa ingestion deferred.*
- **Lane B** is analyst-independent and runs over the broad universe; it logs the *full* ranked list (this is the detector substrate) but only its complement-within-`U_analyst` is surfaced. It is **bar-driven** and decoupled from OptionMetrics — see §6 for why.
- **Generate wide, flag narrow.** Lane B scans broad; the flagger filters to the analyst's expanded neighborhood. The universe is a *parameter*, not a global decision.

**Stack:**
- Frontier LLM (Claude) — stand-in for the eventual trained policy; powers Lane A retrieval and any extraction.
- **WRDS** — historical/eval bars, fundamentals, entity-resolution joins (see §3). Pin the WRDS MCP for agentic/interactive queries; use the `wrds` Python package for the deterministic pipeline.
- **Neo4j** (reuse fbrain's instance) — entity graph + seed store, as a *separate labeled subgraph* from fbrain's page DAG.
- **fbrain MCP server** (`blind_spot/fbrain/mcp_server.py`) — the analyst's persistent knowledge base; provides the seed list via `pull_seeds_from_fbrain`.
- **Polygon.io** — production-current daily bars. *Adapter built, deferred until after the demo (§12).*
- **Exa.ai** — news ingestion for Lane A. *Deferred.*

---

## 2. Canonical data model

Everything keys off a single canonical id. **Use CRSP `permno` as the hub.** All edge sources
and the analyst list resolve to `permno` *before* any set operation or join. See the
`entity-resolution` skill for the linking tables.

```python
from dataclasses import dataclass
from datetime import datetime, date

CanonicalId = str  # "permno:14593" style; permno is the hub

@dataclass(frozen=True)
class Candidate:
    canonical_id: CanonicalId
    implied_move: float | None     # ATM straddle move at the catalyst expiry; None when no event/chain
    iv_rank: float | None          # IV vs the name's own trailing window, in [0,1]; None without OM
    measure: str                   # "straddle" | "attention"
    salience: float                # within-bucket (sector × size) normalized rank used to order
    coverage: bool                 # True for every bar-backed name (was: optionable+liquid only)
    as_of: datetime                # T0; every field knowable strictly before T0
    # Added in the bar-driven refactor (see §6):
    attention: float = 0.0         # composite z-score across bar signals (pre-bucket-rank)
    has_options: bool = False      # True when OM enrichment was available for this name
    components: dict[str, float] | None = None   # per-signal z-scores, for logging / later RL

@dataclass(frozen=True)
class Flag:
    canonical_id: CanonicalId
    salience: float
    on_entity_frontier: bool
    on_thesis_frontier: bool       # always False in v0.5
    entity_path: list[str] | None  # the hops, e.g. ["NVDA","datacenter buildout","<cooling name>"]
    thesis_path: list[str] | None  # None in v0.5
    reason: str                    # human-readable; leads with the named-dyad edge when present

@dataclass(frozen=True)
class Edge:
    src: CanonicalId
    dst: CanonicalId
    kind: str                      # "competitor" | "vertical" | "supplier_customer" | "comovement"
    weight: float                  # continuous; semantics per kind (see §4)
    directed: bool                 # supplier_customer is directed; competitor/comovement symmetric
    as_of: date                    # for supplier_customer: the FILING date, not the fiscal date
    provenance: str                # "tnic" | "vtnic" | "compustat_segment" | "crsp_comovement"
    source_span: str | None        # for named dyads: the disclosing text → reason string
```

`A_final` is a **literal list of names** (per the analyst). Resolve each to `canonical_id`;
log with timestamps so end-of-session ordering is available for time-to-coverage and (optional)
conviction tiers. Do not invent tiers if the source list is unordered.

---

## 3. Data sources (confirmed against Stanford WRDS entitlement)

> WRDS table/library names below are **starting points** — confirm exact schema via the WRDS
> MCP reference tools (`wrds_mcp_reference_tools`, subscribed) or the WRDS web docs before relying on them.

| Layer | Source | WRDS product (status) | Notes |
|---|---|---|---|
| Daily bars (Lane B signal, eval) | **CRSP daily** via `WrdsBarSource` | `crsp_a_stock.dsf` / `dsi` (**Yes**) | Survivorship-free; eval/backtest path. ~2-month update lag — never use for current sessions. |
| Daily bars (Lane B signal, prod) | **Polygon.io** via `PolygonBarSource` | external (Starter+ tier, $29/mo) | Adjusted bars; yesterday-current; **the production source**. Permno→ticker map bootstrapped from CRSP once, cached to `data/permno_ticker_map.json`. |
| Competitor edges | Hoberg-Phillips **TNIC** | external free download | text-based, continuous similarity, annual vintages |
| Vertical relatedness | Hoberg-Phillips **VTNIC** | external free download | BEA I-O + 10-K text. *Loader implemented; flat files not yet sourced.* |
| Named customer-supplier | **Compustat Historical Segments** | `comp_segments_hist_daily` (**Yes**) | Cohen-Frazzini source; revenue-weighted, directed; large-customer/small-supplier bias |
| Customer→id resolution | **WRDS Supply Chain linking** | `wrdsapps_link_supplychain` (**Yes**) | resolves segment customer names to ids |
| Co-movement | **CRSP daily** | `crsp_a_stock` (**Yes**), CCM `crsp_a_ccm` (**Yes**) | point-in-time, survivorship-free |
| Options / IV enrichment | **OptionMetrics IvyDB US** | `optionm_all` (**Yes**) | vol surface → implied move + IV rank. *Now optional enrichment, not a gate — see §6.* |
| OptionMetrics↔CRSP | linking suite | `wrdsapps_link_crsp_optionm` (**Yes**) | secid ↔ permno |
| Identifier hub | **CRSP/Compustat Merged** | `crsp_a_ccm` (**Yes**) | gvkey ↔ permno |
| Analyst coverage/estimates | **IBES** | `tr_ibes` (**Yes**) | universe definition; future consensus/detector |
| Catalyst/event calendar | **Capital IQ Key Developments** | `ciq_keydev` (**Yes**) | structured corporate events. *Not yet wired into events arg of `generate()`.* |
| Parent/subsidiary | **WRDS Subsidiaries** | `wrdsapps_subsidiary` (**Yes**) | entity-resolution aid |
| As-filed (point-in-time) | **As-Filed Financials** | `contrib_as_filed_financials` (**Yes**) | substitute for missing Compustat PIT |
| Filing dates | **WRDS SEC platform** | `wrdssec_all`, `wrds_sec_search` (**Yes**) | for stamping segment edges to filing date |
| Agentic WRDS access | **WRDS MCP** | `wrds_mcp_access` (**Yes**) | pin for Claude Code; guardrails via the two skills |

**The `BarSource` seam:** Lane B does not directly query any vendor. It calls a
`get_bars(permnos, start, end) → DataFrame` / `get_market(start, end) → Series` protocol
defined in `blind_spot/market_data.py`. v0.5 ships **both** `WrdsBarSource` (CRSP, for
historical and eval paths) and `PolygonBarSource` (Polygon.io, for production-current
runs); they're 100% interchangeable from the generator's perspective. The signal layer
(`blind_spot/signals.py`) is a pure function of the bars frame, so the same composite
attention score is produced whichever source feeds it.

**NOT available (do not design around these):**
- `factset_revere_supply_chain` — **No** (only `factsamp_revere` trial/sample). No reverse-disclosure breadth. Named layer = Compustat Segment only.
- `comp_pit` (Compustat Point-in-Time) — **No**. Mitigate with `contrib_as_filed_financials` + filing dates from the SEC platform (see the `point-in-time-discipline` skill).

**Dropped from the earlier free-path plan:** the EDGAR + LLM customer-disclosure scrape. Compustat Segments replaces it directly. (Keep WRDS SEC full-text search available only for filing-date stamping, not for re-deriving dyads.)

---

## 4. The entity graph

Reuse fbrain's Neo4j. **Create the market entity graph as a separate labeled subgraph**
(e.g. node label `:Security`, relationship types `:COMPETES_WITH`, `:VERTICAL`,
`:SUPPLIES`, `:COMOVES_WITH`) so fbrain's page-DAG convention does not apply — supply chains
have cycles and competitor/co-movement edges are symmetric.

Edge kinds and weight semantics:

- **`competitor` (TNIC):** undirected, weight = TNIC cosine similarity in product space (continuous). The primary structural backbone. Threshold the similarity to set expansion depth `d_e`.
- **`vertical` (VTNIC):** undirected/relatedness, weight = vertical relatedness score. Catches read-through across the supply chain in product space.
- **`supplier_customer` (Compustat Segment):** **directed** supplier→customer, weight = disclosed revenue fraction. Sparse but **named and explainable** — these carry `source_span`. `as_of` = **filing date**, not `datadate` (see skill).
- **`comovement` (CRSP daily):** undirected, weight = trailing partial correlation (strip the common factor; raw correlation just re-discovers sector betas). Low weight. The only layer that catches an *emerging* edge before it prints in filings/TNIC.

**Explainability hierarchy (drives which edge a flag cites):** named `supplier_customer` > `competitor`/`vertical` similarity > `comovement`. When a flag has a named-dyad edge, lead the `reason` with it; otherwise say "product-market peer" / "co-moving" honestly.

---

## 5. The thesis graph (stub only)

Interface present, returns empty in v0.5. The `Flag.on_thesis_frontier` field exists and is
always `False`; the composer's agreement logic (§7) is written so the thesis layer is a later
*fill-in*, not a refactor. When built later: unsupervised clustering of mention-context
embeddings / factoring the name×theme co-mention matrix — **representation learning, not RL**.
Keep `d_t` (thesis depth) shallow when it exists; thesis edges are soft and herding-prone.

---

## 6. Candidate generator (Lane B)

Pure function of a point-in-time **bar snapshot**. Knows nothing about the analyst.

### Why the design changed from the original spec

The original spec routed the candidate generator through OptionMetrics: pull IV surface
at `T0`, rank by trailing IV elevation within a sector×size bucket. Building and running
it surfaced two structural problems:

1. **OM was a universe gate, not just a signal.** The first step of `generate()` resolved
   permno → secid via `wrdsapps_link_crsp_optionm.opcrsphist`. Names without options
   coverage (small-caps, recent IPOs, foreign listings) were silently dropped from the
   universe entirely — not flagged as uncovered, *gone*. The flagger downstream then
   never even saw them.
2. **OM data lag became the `as_of` ceiling.** WRDS OM loads lag months behind real time.
   The pipeline ran fine at historical `as_of` dates near the load vintage, then
   silently produced **zero candidates** the moment `as_of` crossed the OM ceiling.
   For a "morning copilot" whose whole point is *current* attention, this was fatal.

Both fail silently — exactly the kind of footgun §9 warns about.

### The shape that shipped

Lane B is split into a **bar source**, a **signal layer**, and a **composer**:

```
BarSource.get_bars(universe, T0-W, T0-1)  ──► daily OHLCV + ret + shrout
                                                │
                                                ▼
                                       signals.py (pure)
                                       ├── realized_vol_spike     (short_W RV ÷ long_W RV)
                                       ├── abnormal_volume        (z-score of dollar vol)
                                       ├── dislocation            (|ret − β·mkt| latest day)
                                       └── gap                    (|open − prev_close|)
                                                │
                                                ▼
                          composite = Σ wᵢ · zscore_x(signalᵢ)   ◄── + iv term if OM exists
                                                │
                                                ▼
                          within-bucket rank (SIC division × mktcap quintile)
                                                │
                                                ▼
                                          Candidate list (sorted)
```

Every signal is a function of *daily bars*, so the source is fungible. CRSP for eval,
Polygon for production — see §3 and §12.

### IV is now optional enrichment, never a gate

OptionMetrics is still pulled when `enrich_with_options=True` and `wrds_conn` is provided:
ATM IV at `T0` → trailing IV rank → contributes one z-scored term to the composite.
Straddle-based implied move is still computed for named catalysts (the `events` arg).
But the *universe* is whatever the `BarSource` returns, the *salience* is well-defined
without OM, and a name's `Candidate.has_options` field records whether OM enrichment
was available — for logging and later analysis, not for filtering.

### Bucketing inputs also moved off OM

Original `generate()` pulled market cap from `optionm_all.secprd` (close × shrout). The
shipped version reads close and shrout from the same `BarSource` frame, so market-cap
quintile bucketing covers every bar-backed name — including the non-optionable ones.
SIC division still comes from CRSP `stocknames` (point-in-time, via `nameenddt`
window). When `wrds_conn` is not provided at all, every name lands in the default
SIC bucket, which is honest, but a reduced-resolution view.

### `salience` semantics, unchanged

```
salience(c) = rank_within_bucket(composite(c)) / |bucket|     ∈ (0, 1]
```

The bucketing kills fat-tail domination by structurally-high-vol names (small-cap /
biotech / SPAC) and lets a calm mid-cap industrial that's having an unusual day rank
ahead of an always-volatile biotech that isn't. `coverage` is still a first-class output:
it now means "bar-backed" (true for every candidate); the orthogonal `has_options` flag
records IV availability.

### Catalyst-driven `implied_move`, unchanged

For a name with a dated catalyst, implied move remains the model-free ATM straddle
breakeven from the first expiry after the event, off the OptionMetrics vol surface
at `T0`:

$$\text{implied\_move} = \frac{C_{\text{ATM}} + P_{\text{ATM}}}{S}\bigg|_{\text{first expiry after catalyst},\ t=T_0}$$

`S` is now read from the `BarSource` close (used to be from `optionm_all.secprd`),
so a missing OM straddle no longer also corrupts the denominator. Names with a
straddle measure carry `measure="straddle"`; the rest carry `measure="attention"`.

### Determinism

`generate(universe, as_of, bar_source=..., wrds_conn=..., enrich_with_options=True,
window_days=252, weights=...) -> list[Candidate]`, ranked desc by `salience`,
**deterministic** given the bar vintage and the WRDS snapshot — the snapshot-reproducibility
unit test (§8) covers the bar-only path, which is the production path.

---

## 7. Expansion + flagger

**Seeds:** pull the analyst's persistent seeds from **fbrain** (names/themes engaged across
sessions), snapshotted **as of before `T0`** (writing back today's session before snapshotting
is a lookahead leak — see skill). Weight seed origins by engagement recency/depth; stale seeds
expand tighter or become blind-spot territory themselves.

**fbrain ↔ entity-graph bridge.** fbrain pages are tagged in free-form Markdown; the
canonical permno needed for the entity graph is recovered from page **tags**.
Convention: tag any page about a security with the **primary ticker** (uppercase, 1–5
chars), e.g. `["NVDA", "semiconductors"]`. `pull_seeds_from_fbrain` (in
`blind_spot/flagger.py`):

1. Cypher `MATCH (p:Page) WHERE p.updated_at < $T0` against the fbrain Neo4j namespace
   (point-in-time filter).
2. Regex-filter tags through `^[A-Z]{1,5}$` to isolate ticker-like strings.
3. Resolve each ticker → permno via `entity_resolution.resolve_batch` on CRSP
   `stocknames`, **point-in-time at `T0`** (handles ticker reuse correctly).
4. Weight each seed by `exp(-Δdays · ln2 / 180) · log1p(in_degree + 1)` — exponential
   recency decay with a 6-month half-life, multiplied by log-scaled fbrain in-link count
   as an engagement proxy. When the same canonical_id resolves from multiple pages, the
   highest-weight record wins (deduplication).

A ticker tag that doesn't resolve at `T0` is logged and dropped — small-caps not in CRSP
stocknames, recent IPOs, sector tags that happen to look ticker-shaped (`AI`, `HBM`,
`OSAT`). The filter is conservative: drop quietly, never fabricate a permno.

**Expansion** (entity layer only in v0.5):

$$U_{\text{analyst}} = \text{expand}_{\text{entity}}(\text{seeds},\, d_e)$$

Use **structural** edges (TNIC/VTNIC/segment), not embedding nearest-neighbor — embeddings find
the obvious look-alikes; structural traversal finds connected-but-not-similar (the read-through
blind spot). `d_e` is the precision/reach knob: shallow ≈ "watch my beat," deeper ≈ "watch my
world." Default `d_e = 2`.

**Flagger** — surface the complement within the expanded universe:

```python
def flag_blind_spots(candidates, a_final, seeds, driver, k=20, d_e=2):
    # complement: market-lit ∧ in the analyst's expanded world ∧ absent from their list
    absent = [c for c in candidates
              if c.canonical_id in U_analyst
              and c.canonical_id not in a_final
              and c.coverage]
    top = sorted(absent, key=lambda c: -c.salience)[:k]
    # For each top candidate, surface the shortest path back to *some* seed for the reason
    paths = batched_shortest_path(seeds=seed_ids, targets=[c.canonical_id for c in top])
    return [Flag(..., entity_path=paths[c.canonical_id], reason=build_reason(...))
            for c in top]
```

The shipped implementation batches a single Cypher query per top-k:

```cypher
UNWIND $target_ids AS target_cid
MATCH (target:Security {canonical_id: target_cid})
MATCH (seed:Security) WHERE seed.canonical_id IN $seed_ids
MATCH path = shortestPath(
    (seed)-[:COMPETES_WITH|VERTICAL|SUPPLIES|COMOVES_WITH*1..3]-(target)
)
WITH target_cid, path ORDER BY length(path) ASC
WITH target_cid, collect(path)[0] AS shortest
RETURN target_cid,
       [n IN nodes(shortest) | n.canonical_id] AS node_path,
       [r IN relationships(shortest) | {kind: type(r), source_span: r.source_span,
                                        weight: r.weight}] AS edges
```

`build_reason` picks the **highest-information edge** on the path (per the §4
explainability hierarchy): a named `SUPPLIES` edge with `source_span` cites the
disclosing dyad ("Nokia is 26% of MRVL's FY2022 revenues"); otherwise it falls
back to "product-market peer via …" or "co-moving with …". The reason text is what
the analyst actually reads at 6am — the path is structural breadcrumbs in case they
want to drill down.

Composition is written for **agreement-as-confidence**: entity-frontier-only is medium
confidence today; entity ∧ thesis (later) is highest. Keep frontiers separate so the
*intersection* is computable when the thesis layer lands.

---

## 8. Eval harness

Three tiers, mapping onto the verifiability hierarchy:

1. **Snapshot reproducibility (deterministic, your test suite):** re-run `generate` on an archived vintage → identical ranking. This is the unit test that proves no lookahead.
2. **Copilot accuracy vs `A_final`:** precision-favoring Fβ (β < 1) of surfaced names against the analyst's resolved set, **and time-to-coverage** (turns/seconds until the candidate set covers `A_final`). Time-to-coverage is the metric that actually expresses "copilot" — a slow exhaustive retriever scores well on F alone.

$$F_\beta = (1+\beta^2)\,\frac{\text{prec}\cdot\text{rec}}{\beta^2\,\text{prec}+\text{rec}},\quad \beta = 0.5$$

3. **Flag accept/dismiss precision:** same-session human label on each surfaced flag = `accepts / flags`. This is the cheap, same-day proxy that tunes `k` and β. (Ex-post realized importance is detector territory — log it, don't optimize it.)

**Log everything** — full trajectories, candidate lists, flags, accept/dismiss. This is the dataset that makes later RL of the traversal policy possible; without it there is nothing to roll out in or score against.

**Shipped harness (`blind_spot/eval.py`):**

```python
logger = SessionLogger(Path("logs/sessions.jsonl"), session_id="2024-06-14-ryan")
logger.log_candidates(turn=1, candidates=cands, as_of=as_of)
logger.log_flags(turn=1, flags=flags)
logger.log_accept(turn=1, flag_id="permno:59328")        # INTC
logger.log_dismiss(turn=1, flag_id="permno:18770")       # BBIO (pharma noise)
logger.log_a_final(a_final={"permno:18267", ...})        # end of session

scores = score_session(Path("logs/sessions.jsonl"), "2024-06-14-ryan", beta=0.5)
# → {"f_beta", "precision", "recall", "time_to_coverage",
#    "accept_precision", "n_flags", "n_accepts", "n_dismisses", ...}
```

JSONL is append-only and turn-aware. `aggregate_sessions(...)` macro-averages
across sessions (mean Fβ/precision/recall, median time-to-coverage) so a week
of usage rolls into one number for tuning `k` and the signal weights.

---

## 9. Hard invariants (guardrails)

These fail **silently** if violated — no exception, just a corrupted eval. Enforce in code and tests.

- **Point-in-time / as-of (see `point-in-time-discipline` skill):** every value knowable strictly before `T0`. Specifically: OptionMetrics IV snapshot at `T0`; CRSP co-movement window ends before `T0`; TNIC uses the vintage available before `T0`; Compustat Segment edges stamped to **filing date** (use as-filed + SEC filing dates, since `comp_pit` is unavailable); **fbrain `:Page` nodes filtered to `updated_at < T0` before seed extraction** — the seed pull happens at the start of the session, before the session writes back, and `pull_seeds_from_fbrain` enforces this in its Cypher.
- **Single canonical id space (see `entity-resolution` skill):** `A_final` and all edge sources resolve to `permno` before any set op/join. A silent id mismatch corrupts the reward. Ticker resolution uses CRSP `stocknames` with the **`nameenddt`** window column — the earlier `nameendt` typo silently broke every ticker→permno join until fixed.
- **Precision weighting (β < 1):** flags cost the analyst's attention; bias the whole system toward fewer, surer flags.
- **`BarSource` as the universe definition:** Lane B's universe is exactly the permnos with bars in [T0−W, T0−1]. Anything that filters earlier (the old OM gate) silently shrinks the universe in ways the analyst can't see. Keep the bar source as the single boundary.
- **Frame lock-in monitor:** track novelty at the expansion frontier across weeks. If `U_analyst` converges to a fixed point (seeds → flags → seeds), the tool has started confirming priors instead of finding blind spots. Guardrail, not a feature.

---

## 10. Build order

0. ✅ **Repo prep** — `AgenticTrading` cleared; `.claude/skills/` populated; WRDS MCP pinned.
1. ✅ **`entity-resolution` layer** (`blind_spot/entity_resolution.py`) — `permno` hub, CCM / OM-CRSP / subsidiaries / segment customer linking. Single point of resolution for every join. (Bug fix landed mid-build: `nameendt` → `nameenddt`.)
2. ✅ **Graph load: TNIC** (`run_tnic_load.py`, `blind_spot/graph_loader.py`) — competitor edges as `:COMPETES_WITH` with cosine-similarity weights. VTNIC loader implemented; flat files not yet sourced.
3. ✅ **Named dyads** (`run_segment_load.py`, `blind_spot/segment_loader.py`) — Compustat Segments → directed weighted `:SUPPLIES` edges, **stamped to SEC filing date**, with `source_span` for the human-readable reason.
4. ✅ **Co-movement** (`run_comovement_load.py`, `blind_spot/comovement_loader.py`) — CRSP daily → low-weight `:COMOVES_WITH` partial-correlation edges, trailing 252-day window, market factor stripped via OLS.
5. ✅ **Candidate generator (Lane B)** (`blind_spot/candidate_generator.py`) — **shipped as bar-driven composite attention with optional OM enrichment**, not the OM-gated form in the original spec. See §6. Snapshot-reproducibility test in `test_candidate_generator.py`.
6. ✅ **Expansion + flagger** (`blind_spot/flagger.py`) — `pull_seeds_from_fbrain` → BFS expansion at `d_e=2` → `flag_blind_spots` complement → per-flag explanation paths via Cypher `shortestPath`. (Bug fix mid-build: `resolve_batch` signature.)
7. ✅ **Eval harness** (`blind_spot/eval.py`) — `SessionLogger` (JSONL, append-only, turn-aware) + `score_session` (Fβ, time-to-coverage, accept_precision). 172 offline tests pass; integration tests gated behind `-m integration`.
8. ✅ **Lane B refactor (Phase 1)** — introduced `BarSource` protocol + `WrdsBarSource` (`blind_spot/market_data.py`), pure-function attention signals (`blind_spot/signals.py`), composite blend in `candidate_generator.generate`. Moves the `as_of` ceiling from "last OM load" to "last CRSP load," decouples non-optionable names from the universe. *Validated live: 117 candidates at `as_of=2025-01-01` (above OM ceiling).*
9. ✅ **`/run-pipeline` skill** (`.claude/skills/run-pipeline/SKILL.md`) — interactive orchestration of the four-stage daily workflow with explanations, suitable for the demo. The skill is the user-facing entry point that ties everything together.
10. ✅ **Polygon production source (Phase 2)** — `PolygonBarSource` and `build_permno_ticker_map` (in `blind_spot/market_data.py`) + `run_polygon_smoke.py`. Includes a thread-safe token-bucket rate limiter (`_RateLimiter`) that respects Polygon's plan ceiling and the `Retry-After` header on 429s. **API key travels in the `Authorization: Bearer` header**, not as a URL query param, so the secret never appears in HTTP access logs, transcripts, or 429 error messages. Live-validated at Starter tier (100 req/min): 196 tickers fetched in ~2 min, 0 rate-limit failures, 180 candidates produced. Second-and-later runs hit zero WRDS endpoints (the permno→ticker map caches to JSON).

Tasks 1–4 are plumbing assembled from priors and rough models. Tasks 5–8 are the product;
Task 9 is the demo surface; Task 10 makes the system production-current.
The original numbering survives for traceability with the spec.

---

## 11. Open questions

**Answered during the build:**

- **fbrain seed schema** → `:Page` nodes with uppercase-ticker tags + `updated_at`
  timestamp; weighted `exp(-Δdays/180·ln2) × log1p(in_degree+1)`; resolved point-in-time
  via CRSP `stocknames`. Tags that don't resolve are dropped quietly. (§7)
- **Options coverage of the universe** → no longer determinative. Lane B works without
  options; IV adds a soft term where coverage exists. The original concern dissolves.
- **`A_final` format** → flat literal name list of permnos for v0.5. No ordering, no
  conviction tiers. Conviction tiers could be a future field on the `log_a_final` event
  if the analyst's workflow makes them natural to capture.

**Still open:**

- **Universe scope for the default surfaced lane** — "watch my beat" (`d_e=1`, named
  edges only) vs "watch my world" (`d_e=2`, includes COMOVES). The shipped default is
  `d_e=2` with the SUPPLIES > COMPETES > COMOVES priority hierarchy in the reason text.
  An analyst session or two should validate; if they consistently dismiss
  `co-moving` flags, drop `:COMOVES_WITH` from the expansion (keep it in the graph).
- **TNIC noise across sector boundaries** — the demo run had MRVL pulling pharma names
  (BBIO, ARWR, IRWD) through TNIC product-text intermediaries (SPAC-era 10-K boilerplate
  overlap). Three options: (a) cap d_e=1 for COMPETES edges specifically, (b) require
  SIC-major-division match on TNIC traversals, (c) accept it as the cost of pure
  product-text similarity and rely on the dismiss feedback loop. Worth deciding before
  scaling sessions.
- **Catalyst events feed** — `events` arg to `generate()` exists but is unused in the
  demo. Capital IQ Key Developments (`ciq_keydev`) is licensed; wiring it up is one
  WRDS query and a permno→event-date dict-build.
- **VTNIC vintages** — loader present, data files not yet sourced. Low priority while
  TNIC covers competitor edges.
- **WRDS Revere later** — if `factset_revere_supply_chain` is ever licensed, the named
  layer gains reverse-disclosure breadth and pre-assigned ids — revisit §4.

---

## 12. What's still deferred

Tracked as concrete work items in [`TASKS.md`](TASKS.md). Summary:

### Thesis graph (designed, stub-only — original §5)

Interface present, returns empty. `Flag.on_thesis_frontier` always `False`. Planned as
unsupervised clustering of mention-context embeddings or factoring the name×theme
co-mention matrix — **representation learning, not RL**. The composer already keeps
entity and thesis frontiers separate so this lands as a fill-in, not a refactor.

### Lane A retrieval (not started)

Analyst-facing copilot search scored against `A_final`. Frozen Claude model + Exa.ai
news ingestion. The eval harness Fβ + time-to-coverage already supports it; the
candidate side is ready to consume retrieved names from a parallel lane.

### RL traversal policy

The point of the JSONL trajectory log. Once enough sessions accumulate, train a
traversal policy that picks which edge to walk next given (seeds, current frontier,
accepted/dismissed history). The frozen frontier model in v0.5 is the placeholder.

### Eval v0.6 — see [`TASKS.md` § Proposal: Eval v0.6](TASKS.md#proposal-eval-v06)

Five structural problems with the current scoring became visible during the demo run.
The proposal redefines `A_final` to exclude seeds, adds counterfactual lift,
frame-lock detection, explanation-quality capture, and decomposed time-to-coverage.

### Other small items (in `TASKS.md`)

- fbrain `put_page` ergonomic — `updated_at` is always `now()`; retro work requires manual backdating.
- WRDS 2FA cold-start — non-interactive shells can't clear 2FA on a fresh connect.
- Signal winsorization — the live smoke surfaced 10σ z-scores; clip at ±5σ before blending.
- VTNIC data files — loader implemented, files not yet sourced.
- Capital IQ Key Developments — wire `ciq_keydev` into the `events` arg for catalyst-driven straddle.
