# Blind Spot v0.5 — Build Specification

> A morning **copilot** for a financial analyst. Given the day's news and the analyst's
> own resolved name list, it surfaces names the analyst is at risk of *omitting* — names
> the market is moving on that are structurally connected to the analyst's world but
> absent from their list. **Copilot, not detector.**

This document is the build backbone. Two operational guardrails live as Claude Code skills
under `.claude/skills/` (`point-in-time-discipline`, `entity-resolution`) and auto-load when
relevant. Read them before touching any data source.

---

## 0. Scope

**In scope (v0.5, ~few-day build):**
- Canonical identifier layer (`permno` hub).
- Entity graph: competitor + vertical + named customer-supplier + co-movement edges.
- Options-based candidate generator (implied move / IV rank) over a universe.
- gbrain-seeded structural expansion → per-analyst universe.
- Flagger: surface the complement (market-lit ∧ structurally-near ∧ absent-from-list).
- Eval harness: weighted precision-favoring F-score + time-to-coverage vs the analyst list.
- Full trajectory logging (this is the substrate for any later RL or detector work).

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
   (Exa ingestion)        │  (frozen model, agentic)     │
                          └─────────────────────────────┘
                          ┌─────────────────────────────┐
  market snapshot @ T0  → │  LANE B: candidate generator │ → ranked salient names
   (OptionMetrics)        │  (analyst-independent)       │   over the universe
                          └─────────────────────────────┘
        seeds (gbrain) ─→ expand over ENTITY GRAPH ─→ U_analyst (per-analyst universe)
                          ┌─────────────────────────────┐
                          │   FLAGGER (composer)         │ → top-k flags, by confidence,
                          │   complement within U_analyst│   each with an explanation path
                          └─────────────────────────────┘
```

- **Lane A** is the analyst-facing copilot retrieval (their own search, scored vs `A_final`). Frozen model.
- **Lane B** is analyst-independent and runs over the broad universe; it logs the *full* ranked list (this is the detector substrate) but only its complement-within-`U_analyst` is surfaced.
- **Generate wide, flag narrow.** Lane B scans broad; the flagger filters to the analyst's expanded neighborhood. The universe is a *parameter*, not a global decision.

**Stack:**
- Frontier LLM (Claude) — stand-in for the eventual trained policy; powers Lane A retrieval and any extraction.
- **WRDS** — market/fundamental data (see §3). Pin the WRDS MCP for agentic/interactive queries; use the `wrds` Python package for the deterministic pipeline.
- **Neo4j** (reuse fbrain's instance) — entity graph + seed store, as a *separate labeled subgraph* from fbrain's page DAG.
- **Exa.ai** — news ingestion for the day's commentary.

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
    implied_move: float | None     # ATM straddle move for the catalyst expiry; None if no liquid chain
    iv_rank: float | None          # IV vs the name's own trailing window, in [0,1]
    measure: str                   # "straddle" | "iv_rank" | "iv_level"
    salience: float                # within-bucket (sector x size) normalized rank used to order
    coverage: bool                 # True only if optionable + liquid enough to trust the number
    as_of: datetime                # T0; every field knowable strictly before T0

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
| Competitor edges | Hoberg-Phillips **TNIC** | external free download | text-based, continuous similarity, annual vintages |
| Vertical relatedness | Hoberg-Phillips **VTNIC** | external free download | BEA I-O + 10-K text |
| Named customer-supplier | **Compustat Historical Segments** | `comp_segments_hist_daily` (**Yes**) | Cohen-Frazzini source; revenue-weighted, directed; large-customer/small-supplier bias |
| Customer→id resolution | **WRDS Supply Chain linking** | `wrdsapps_link_supplychain` (**Yes**) | resolves segment customer names to ids |
| Co-movement | **CRSP daily** | `crsp_a_stock` (**Yes**), CCM `crsp_a_ccm` (**Yes**) | point-in-time, survivorship-free |
| Options / IV signal | **OptionMetrics IvyDB US** | `optionm_all` (**Yes**) | vol surface → implied move + IV rank with history |
| OptionMetrics↔CRSP | linking suite | `wrdsapps_link_crsp_optionm` (**Yes**) | secid ↔ permno |
| Identifier hub | **CRSP/Compustat Merged** | `crsp_a_ccm` (**Yes**) | gvkey ↔ permno |
| Analyst coverage/estimates | **IBES** | `tr_ibes` (**Yes**) | universe definition; future consensus/detector |
| Catalyst/event calendar | **Capital IQ Key Developments** | `ciq_keydev` (**Yes**) | structured corporate events |
| Parent/subsidiary | **WRDS Subsidiaries** | `wrdsapps_subsidiary` (**Yes**) | entity-resolution aid |
| As-filed (point-in-time) | **As-Filed Financials** | `contrib_as_filed_financials` (**Yes**) | substitute for missing Compustat PIT |
| Filing dates | **WRDS SEC platform** | `wrdssec_all`, `wrds_sec_search` (**Yes**) | for stamping segment edges to filing date |
| Agentic WRDS access | **WRDS MCP** | `wrds_mcp_access` (**Yes**) | pin for Claude Code; guardrails via the two skills |

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

Pure function of a point-in-time market snapshot. Knows nothing about the analyst.

For a name with a dated catalyst, implied move is the model-free ATM straddle breakeven from
the expiry bracketing the event, off the OptionMetrics vol surface at `T0`:

$$\text{implied\_move} = \frac{C_{\text{ATM}} + P_{\text{ATM}}}{S}\bigg|_{\text{first expiry after catalyst},\ t=T_0}$$

Raw IV level is confounded (small-cap/biotech carry structurally high IV), so rank by
**elevation relative to the name's own history** — now computable because OptionMetrics gives
the IV time series:

$$\text{iv\_rank} = \frac{\text{IV}_{T_0} - \min_W \text{IV}}{\max_W \text{IV} - \min_W \text{IV}}$$

`salience` = cross-sectional rank of that elevation **within a sector × size bucket** (kills
fat-tail domination by a couple of earnings names and the structural-IV confound). `coverage`
is a first-class output: a `None` implied_move (no liquid chain) is logged, never errored, and
gates flagging.

`generate(universe, as_of, events) -> list[Candidate]`, ranked desc by `salience`,
**deterministic** given the snapshot vintage (this determinism is a unit test — see §8).

---

## 7. Expansion + flagger

**Seeds:** pull the analyst's persistent seeds from gbrain (names/themes engaged across
sessions), snapshotted **as of before `T0`** (writing back today's session before snapshotting
is a lookahead leak — see skill). Weight seed origins by engagement recency/depth; stale seeds
expand tighter or become blind-spot territory themselves.

**Expansion** (entity layer only in v0.5):

$$U_{\text{analyst}} = \text{expand}_{\text{entity}}(\text{seeds},\, d_e)$$

Use **structural** edges (TNIC/VTNIC/segment), not embedding nearest-neighbor — embeddings find
the obvious look-alikes; structural traversal finds connected-but-not-similar (the read-through
blind spot). `d_e` is the precision/reach knob: shallow ≈ "watch my beat," deeper ≈ "watch my
world." Default `d_e = 2`.

**Flagger** — surface the complement within the expanded universe:

```python
def flag_blind_spots(candidates, a_final, universe, k):
    # complement: market-lit ∧ in the analyst's expanded world ∧ absent from their list
    absent = [c for c in candidates
              if c.canonical_id in universe
              and c.canonical_id not in a_final
              and c.coverage]
    # confidence = agreement across frontiers + salience (thesis frontier always False in v0.5)
    return rank_by_confidence(absent)[:k]
```

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

---

## 9. Hard invariants (guardrails)

These fail **silently** if violated — no exception, just a corrupted eval. Enforce in code and tests.

- **Point-in-time / as-of (see `point-in-time-discipline` skill):** every value knowable strictly before `T0`. Specifically: OptionMetrics IV snapshot at `T0`; CRSP co-movement window ends before `T0`; TNIC uses the vintage available before `T0`; Compustat Segment edges stamped to **filing date** (use as-filed + SEC filing dates, since `comp_pit` is unavailable); gbrain seeds snapshotted before the session writes back.
- **Single canonical id space (see `entity-resolution` skill):** `A_final` and all edge sources resolve to `permno` before any set op/join. A silent id mismatch corrupts the reward.
- **Precision weighting (β < 1):** flags cost the analyst's attention; bias the whole system toward fewer, surer flags.
- **Frame lock-in monitor:** track novelty at the expansion frontier across weeks. If `U_analyst` converges to a fixed point (seeds → flags → seeds), the tool has started confirming priors instead of finding blind spots. Guardrail, not a feature.

---

## 10. Build order (ordered tasks for Claude Code)

0. **Repo prep:** audit `AgenticTrading` code for reusable WRDS/market-data connectors → salvage; delete the rest so agentic search isn't polluted. Add the `.claude/skills/` from this bundle. Pin the WRDS MCP.
1. **`entity-resolution` layer:** `permno` hub + linking tables (CCM, optionm-crsp, subsidiaries, segment customer linking). Everything downstream depends on it.
2. **Graph load:** TNIC + VTNIC flat files → `:COMPETES_WITH` / `:VERTICAL` weighted edges in Neo4j. Working entity graph by end of task.
3. **Named dyads:** Compustat Segments → directed weighted `:SUPPLIES` edges, stamped to filing date, with `source_span`.
4. **Co-movement:** CRSP daily → low-weight `:COMOVES_WITH` partial-correlation edges, trailing window.
5. **Candidate generator (Lane B):** OptionMetrics vol surface → implied move + IV rank → within-bucket salience → `Candidate` list. Add the snapshot-reproducibility test.
6. **Expansion + flagger:** gbrain seed snapshot → structural `expand` → `flag_blind_spots`.
7. **Eval harness:** Fβ + time-to-coverage vs `A_final`; accept/dismiss capture; full logging.

Tasks 1–4 are plumbing assembled from priors and rough models. Tasks 5–7 are the product.

---

## 11. Open questions needing your input (don't let Claude Code invent these)

- **gbrain seed schema:** what exactly a "seed" record looks like coming out of the `/fbrain` Neo4j store, and the read API for snapshotting seeds at `T0`.
- **`A_final` format:** confirmed a literal name list; does it carry any ordering/conviction signal for tiers, or is it flat?
- **Universe scope for the default surfaced lane:** "watch my beat" (`d_e` small) vs "watch my world" (`d_e` larger). Ask your analyst(s) which kind of miss they'd rather live with — the blindside or the noise.
- **Options coverage of your universe:** fraction of names with liquid OptionMetrics chains (sets how much of the universe Lane B can speak to at all).
- **WRDS Revere later:** if you ever license `factset_revere_supply_chain`, the named layer gains reverse-disclosure breadth and pre-assigned ids — revisit §4.
