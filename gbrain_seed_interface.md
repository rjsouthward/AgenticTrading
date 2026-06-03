# gbrain (fbrain) Seed Interface — Blind Spot v0.5

> Dependency for **task 6** (expansion + flagger). Bridges fbrain's knowledge base to the Blind
> Spot entity graph. Revised to match the *actual* fbrain: a markdown KB behind a memory MCP
> (`FinAgents/memory/memory_server.py`), not an entity-aware graph. Read alongside the
> `point-in-time-discipline` and `entity-resolution` skills.

---

## 0. Correction to BUILD.md §4 (apply this)

BUILD.md §4 said "reuse fbrain's Neo4j as a separate labeled subgraph." That assumed fbrain *is*
a Neo4j you control. It isn't — it's a memory MCP that **owns** its store. Corrected architecture:

- **Blind Spot owns its own graph store** (Neo4j) for `:Security` nodes, entity edges, and seeds.
- **fbrain is a read-only seed source**, reached through its memory MCP (and/or directly against
  its backing store for enumeration — see §7).
- The repo is still reused; only the *graph stores are separate concerns*. This keeps fbrain a
  clean KB and decouples Blind Spot from `memory_server.py` internals.

---

## 1. What fbrain actually gives you

A page is `(slug, version, title, body, kind, tags[], namespace)`, plus directed page→page
`link`s. There are **no security/entity nodes** — entities live in the markdown `body`. So the
bridge is not an edge fbrain already has; it's a **projection** Blind Spot computes by reading
page bodies, extracting entities, and resolving them to `permno`.

Useful fields, mapped to seed semantics:
- `kind ∈ {strategy, knowledge}` → conviction signal: `strategy` (playbooks/rules — committed views) outranks `knowledge` (general notes).
- `link` in-degree (how many pages point at a page) → page centrality / load-bearingness → prominence multiplier.
- `tags` (derived from headings) → cheap candidate **theme** seeds for the deferred thesis layer.
- `namespace` → the per-analyst scope. v0.5: one namespace = one analyst.

---

## 2. The point-in-time-critical invariant: append-only, timestamped projection

The bridge is materialized into Blind Spot's store as **append-only, timestamped** mention
records. Never mutate or delete them; the seed read filters `asserted_at < T0`, making the
snapshot correct regardless of when projection or session write-back happens.

**The one thing to confirm in `memory_server.py`:** does it stamp pages/versions with a
wall-clock timestamp?
- **If yes** → stamp each mention with the *page's* timestamp. You can then reconstruct historical seed states → backtesting of past sessions is sound.
- **If no** → stamp each mention with the *projection run time*. Correct for **live** use (today's seeds reflect everything known by now), but you cannot faithfully backtest a past session's universe. For v0.5 single-analyst live use this is acceptable; note it as a known limitation, not a silent leak.

Either way, mentions are immutable once written. See `point-in-time-discipline`.

---

## 3. The seed projection pass (replaces the old "ingestion hook")

A standalone ETL, run on a schedule or before a session — **not** a modification to `/fbrain`.

```python
def project_seeds(namespace: str, as_of: datetime) -> None:
    """fbrain -> Blind Spot seed store. Idempotent, append-only.
       1. Enumerate pages in `namespace` (see §7 for the enumeration path).
       2. For each page with timestamp < as_of: read its body (fbrain `get <slug>`).
       3. Extract entity + theme mentions from the body (LLM/NER).
       4. Resolve entity names -> permno via the entity-resolution skill.
          Unresolved -> store raw name with canonical_id = None (do NOT guess).
       5. Upsert a (:Security {canonical_id}) node in Blind Spot's store and CREATE an
          append-only (:Page {slug})-[:MENTIONS {asserted_at, prominence, kind}]->(:Security).
       6. Never mutate/delete prior :MENTIONS rows.
    """
```

`asserted_at` per §2. `prominence ∈ (0,1]` from `link` in-degree (or constant 1.0 in v0.5).
`kind` on the mention carries the page's `strategy|knowledge` for the conviction weight.

---

## 4. Read side — `read_seeds`

```python
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Seed:
    canonical_id: CanonicalId   # permno; resolves to a :Security node
    weight: float               # recency-decayed engagement, normalized within namespace, in (0,1]
    first_seen: datetime        # earliest mention asserted_at
    last_seen: datetime         # latest mention asserted_at strictly before T0
    n_pages: int                # distinct pages mentioning it before T0
    top_slug: str | None        # highest-prominence page slug -> provenance for the flag reason
    kind: str                   # "name" (entity expansion) | "theme" (thesis layer, later)

def read_seeds(namespace: str, as_of: datetime,
               half_life_days: float = 30.0,
               min_weight: float = 0.0) -> list[Seed]:
    """Snapshot the namespace's seeds as of strictly before `as_of` (= T0), from Blind Spot's
       materialized mention store. Filters asserted_at < as_of; computes the weight below;
       normalizes within namespace. MUST NOT read any mention asserted_at >= as_of."""
```

**Weight.** For entity `e`, over mentions `m` (from pages `p`) with `asserted_at < T0`:

$$w(e, T_0) \;=\; \sum_{m} \; \underbrace{2^{-\,\Delta t_m / h}}_{\text{recency decay}} \;\cdot\; \underbrace{c(\text{kind}_p)}_{\text{conviction}} \;\cdot\; \underbrace{\text{prominence}(p)}_{\text{link centrality}}$$

$\Delta t_m = T_0 - \texttt{asserted\_at}_m$ in days; $h$ = `half_life_days`;
$c(\text{strategy}) > c(\text{knowledge})$ (e.g. 1.0 vs 0.6). Normalize within namespace.

---

## 5. Signals that fall out for free

- **Dormancy → blind-spot territory.** High `n_pages`, old `last_seen`, low decayed `weight` = a thesis the analyst tended heavily then went quiet on. Widen `d_e` around dormant seeds specifically — this is how the design finds where they *stopped* looking, not just where they look now.
- **Themes stored now, used later.** `kind = "theme"` seeds (from `tags`/headings) feed the thesis layer when it lands. Scaffold, not refactor.
- **`strategy` pages as high-conviction anchors.** fbrain's own `kind` gives you the conviction weight for free — no extra labeling.

---

## 6. How it plugs into task 6

```python
seeds = read_seeds(namespace, as_of=T0, half_life_days=30)
U_analyst = expand_entity(seeds, d_e=2)        # structural traversal over the entity graph
flags = flag_blind_spots(candidates, a_final, universe=U_analyst, k=K)
```

Seed `weight` is a per-origin traversal prior; `top_slug` rides through to the flag `reason`
("connected to <name>, which you track in <page title>").

---

## 7. Task 0.5 — read `FinAgents/memory/memory_server.py` first

All remaining unknowns are answerable by reading your own memory server. Have Claude Code do this
before task 1, and reconcile this spec to what it finds:

- **Timestamps?** Does it stamp pages/versions with wall-clock time? (Decides backtest fidelity — §2.)
- **Backing store?** Real Neo4j (project + enumerate via direct Cypher) or a custom/embedded store (read via MCP tools, or add a capability)?
- **Enumeration?** The MCP exposes `search` (semantic, ranked — *not* exhaustive) and `get` (by slug). To project *all* seeds you need to list every slug in a namespace. If the backing store is queryable, enumerate there; otherwise add a `list_pages(namespace, since, until)` tool to `memory_server.py`.
- **Entity structure?** Confirmed: none today. Entities must be extracted from `body` at projection time (step 3 above), then resolved.
- **Analyst model?** Namespace-scoped, no `:Analyst` node. v0.5: one namespace = one analyst; `namespace` is parameterized so multi-analyst/consensus work drops in unchanged.
