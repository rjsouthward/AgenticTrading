# Claude Code — Kickoff Prompt for Blind Spot v0.5

> Paste the section below as your first message to Claude Code in the fbrain repo.
> (Or keep this file in the repo and tell Claude Code to read it.)

---

You are building **Blind Spot v0.5**, a morning copilot for a financial analyst that surfaces
names they're at risk of omitting from their daily list. Before writing any code, read the full
spec set in this repo:

1. `BUILD.md` — the build backbone (architecture, data sources, contracts, build order, invariants).
2. `.claude/skills/point-in-time-discipline/SKILL.md` and `.claude/skills/entity-resolution/SKILL.md` — the two operational guardrails; they auto-load, but read them now so you understand them.
3. `gbrain_seed_interface.md` — the task-6 dependency, **including its §0 correction to BUILD.md §4** (Blind Spot owns its own graph store; fbrain is a read-only seed source).

## Operating rules (non-negotiable — these fail silently if ignored)

- **Point-in-time correctness** on every dated pull (IV, returns, segments, fundamentals, TNIC vintages, seeds). Every value must be knowable strictly before the session timestamp `T0`. Add a determinism test (same vintage in → identical out) for anything touching dated data. Follow the `point-in-time-discipline` skill.
- **One canonical id = CRSP `permno`.** Resolve every name/ticker/vendor id to `permno` before any join or set operation. Unresolved → `None`, never a guess. Follow the `entity-resolution` skill.
- **WRDS via the `wrds` Python package**, not an MCP — the pipeline must be reproducible SQL.
- **Precision over recall everywhere** (Fβ, β < 1). A false flag spends the analyst's attention.
- **A frozen frontier model is the policy. Do NOT train any RL** in v0.5.
- **Do not build out-of-scope items:** no thesis graph beyond a stub, no detector mode, no EDGAR scrape (Compustat Segments replaces it), no FactSet Revere (not licensed).
- **Do not invent** the items flagged for human input (BUILD.md §11, seed spec §7). Surface them and ask me.

## First actions, in order

**Task 0 — repo prep.** Audit the `AgenticTrading` code for reusable WRDS / market-data connectors; salvage those, then remove the rest so agentic search isn't polluted by unrelated trading code. Confirm `.claude/skills/` is in place.

**Task 0.5 — read `FinAgents/memory/memory_server.py`** and report back:
- Does it stamp pages/versions with a wall-clock timestamp? (Decides backtest fidelity.)
- Is the backing store a real Neo4j (direct Cypher) or custom/embedded (read via MCP, or add a capability)?
- Is there a way to enumerate all pages in a namespace? (`search` + `get` are not exhaustive.)
Reconcile `gbrain_seed_interface.md` to what you find; flag any mismatch.

**Then STOP and propose a plan for Task 1** (the `permno` resolution layer + a WRDS smoke test: connect via the `wrds` package and pull a tiny CRSP/CCM sample). Wait for my confirmation before writing pipeline code.

## Cadence

Work in small, reviewable increments: plan → confirm → build → test. After each task in BUILD.md §10, show what you built and the test that proves it (especially the point-in-time determinism tests) before moving on. Don't run ahead of the build order — task 1 (`permno` hub) gates everything.

## Decisions I owe you (ask before they block you)

- `A_final` format — confirmed a literal name list; does it carry ordering/conviction for tiers, or is it flat?
- Default expansion depth `d_e` — "watch my beat" (small) vs "watch my world" (larger). I'll decide after we see flag precision.
- OptionMetrics coverage of the target universe (how much of it Lane B can speak to).
- Namespace/analyst model — v0.5 is single-analyst (one fbrain namespace); confirm which namespace.
- The `memory_server.py` timestamp finding from Task 0.5 (it decides whether we can backtest).
