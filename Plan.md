# Plan — Finance GBrain Layer 1 (Hosted MemoryAgent + L2/L3 seams)

Build order for a **research-demo-grade, always-reachable MemoryAgent** that also ships
the **interface seams** Layers 2 and 3 need (see `Architecture.md` §9). Scope is Layer 1:
we build the *primitives*, not the agent loops or the supervisor.

Legend: ✅ done · 🔜 next · ⛔ blocked-on-prereq · ⬜ todo · 🔌 seam for L2/L3

---

## Phase 0 — Foundation (DONE)

- ✅ Migrate LLM provider OpenAI → DigitalOcean; standardize on `openai-gpt-oss-120b`.
- ✅ Env-driven `OPENAI_*` + `NEO4J_*`; Aura `52b4f6d4` wired & verified.
- ✅ Embeddings default = DO `bge-m3` (1024-dim) with local fallbacks.
- ✅ Vector index `memory_embedding_index` (1024/cosine) ensured at startup.
- ✅ Transport → **stdio-default** (HTTP opt-in via `MCP_TRANSPORT=http`); fixed lifespan teardown
  crash; diagnostics routed to stderr (clean stdio protocol channel). Verified locally vs Aura.

---

## Phase 1 — DB-backed semantic search (✅ CORE DONE — cleanup pending)

- ✅ **Write path:** embed `content_text` via bge-m3 in `store_memory`; `SET m.embedding`, `embedding_model`, `embedding_dim`.
- ✅ **Resilience:** on embed failure, store node + `needs_embedding=true` (write never fails).
- ✅ **Query path:** new `UnifiedDatabaseManager.vector_search()` → `db.index.vector.queryNodes('memory_embedding_index', …)`; `semantic_search_memories` tool rewritten to use it.
- ✅ Retire `memory_index.pkl` as the search path (removed the in-process `index_memory` call).
- ✅ Latent bugs fixed (surfaced once components were enabled): relative-import fallbacks for
  indexer/stream in `unified_database_manager`; `StreamProcessor.stop_processing()` (was `.shutdown()`)
  in both `close()` and the lifespan; `hasattr` guards on the not-yet-implemented reactive event calls.
- ✅ Acceptance MET: stored docs return correctly cosine-ranked from Neo4j (moat query → moat docs; crypto query → BTC).
- ⬜ Cron backfill for `needs_embedding=true` (deferred — for re-embedding pre-existing nodes).
- ⬜ Drop `scikit-learn` / `scipy` (deferred cleanup — requires making the indexer's sklearn imports lazy).

---

## Phase 2 — GBrain generalization (✅ DONE — `:Page` + neutral tools)

- ✅ `:Page` with `page_key` (=`namespace::slug`) uniqueness constraint (edition-portable) + a
  dedicated 1024/cosine vector index `page_embedding_index`.
- ✅ Seam-ready fields on every page: `kind`, `version` (bumped on upsert), `written_by`,
  `agent_type`, `agent_instance_id`, `source`, `trust` (🔌 enables L2/L3 + injection boundary).
- ✅ Manager methods + MCP tools:
  - `put_page(title, body, namespace, slug?, tags, kind, links, source, trust)` → upsert via MERGE on `page_key`, embeds title+body.
  - `get_page(slug, namespace)` → page + outgoing links.
  - `search(query, namespace, limit, kind?)` → namespace-scoped vector search.
  - `create_link(from_slug, to_slug, namespace)` → `(:Page)-[:LINKS_TO]->(:Page)`.
- ✅ `namespace` threaded through every query — **isolation verified** (demo vs other don't bleed).
- ✅ Acceptance MET: generic page round-trips; per-namespace + per-kind search; version bumps; links traverse.

---

## Phase 3 — Security (must precede public exposure)

- ⬜ Bearer-token middleware on the Starlette `app`.
- ⬜ Scopes: **read-only** (search/get) · **read-write** (put/link/prune).
- 🔌 ⬜ **Per-agent identity:** tokens map to an identity → stamped into `written_by` (enables L3 attribution).
- ⬜ Tokens from env; 401 on missing/invalid; `/health` stays unauthenticated.
- ✅ Acceptance: unauth → 401; ro can't write; rw can; writes record `written_by`.

---

## Phase 4 — L2/L3 interface seams (the forward-compat layer)

Build the **primitives** only; consumers come in Layers 2/3.

- 🔌 ⬜ **Config-as-pages (L2):** support `kind="strategy"|"config"`; `get_page` serves an
  agent's current behavior by stable slug.
- 🔌 ⬜ **Compare-and-set (L2/L3):** `put_page(expected_version=N)` → bump `version`, return
  **409** on mismatch (atomic swap / safe multi-writer).
- 🔌 ⬜ **Change events (L2/L3):** publish-on-write to a durable `:Event` log; expose
  **poll-based** `get_events(namespace, since_cursor)` — NOT WebSocket push (simpler, stateless-
  friendly, curl-debuggable, drops the `websockets` dep). Backed by Neo4j.
- 🔌 ⬜ **Claim / lease (L3):** atomic `claim(key, owner, ttl)` + `release(key, owner)` via
  single-transaction MERGE on `:Lease`; TTL expiry.
- ⬜ Constraints for `:Event`/`:Lease` added to idempotent startup schema-init.
- ✅ Acceptance: CAS rejects stale writes (409); a subscriber receives an event on `put_page`;
  two concurrent `claim` calls — exactly one wins.

> Why in Layer 1: these are *store-level contracts*. Adding `version`/`kind`/events/leases
> after the graph is populated is a painful migration; adding them now is nearly free.

---

## Phase 5 — Containerize & deploy (see `Deploy.md`)

- ⬜ Dockerfile (uvicorn `memory_server:app`, port 8000; light image — embeddings via API).
- ⛔ Provision hosting (Fly.io account + CLI) — **user prerequisite**.
- ⬜ Push host secrets (`OPENAI_*`, `NEO4J_*`, `MEMORY_API_TOKEN_*`, per-agent tokens).
- ⬜ Deploy; point MCP client at `https://<host>/mcp`.
- ✅ Neo4j always-on strategy decided: **Aura Free + keep-alive ping** (Phase 6 cron required).
- ⚠️ **Single instance only** until the event/lease seams are confirmed durable across replicas.
- ✅ Acceptance: `/health` green; remote `search` works with a token.

---

## Phase 6 — Durability & ops

- ⬜ Keep-alive cron (anti Aura-Free pause).
- ⛔ Weekly backup cron → private git repo or object storage — **user provides destination**.
- ⬜ Minimal structured logging for store/search/events; surface in `/health`.

---

## Milestones

1. **M1 — Local semantic brain:** Phases 1–2 (DB-backed search + `:Page` tools). ✅ DONE +
   validated via a real MCP **stdio handshake** (`initialize` + `tools/list` + `tools/call`).
2. **M2 — Secured + seam-ready:** Phases 3–4 (auth + CAS/events/leases/attribution).
3. **M3 — Live demo:** Phase 5 (always-reachable hosted MemoryAgent).
4. **M4 — Durable:** Phase 6.

After M2, Layer 2 can be built **without touching Layer 1**: an agent loop just calls
`get_page(kind=strategy)` + `subscribe()`; Layer 3 just calls `claim()` + CAS + per-agent tokens.

---

## Cost (research-demo MVP)

| | Shoestring (recommended) | Solid always-on |
|---|---|---|
| Compute | Fly.io ~$3/mo | Render $7/mo |
| Neo4j | Aura **Free $0** + keep-alive | Aura Pro ~$65/mo |
| Tokens (embeddings + LLM) | **< $1/mo** | **< $5/mo** |
| **Total** | **≈ $3–5 / month** | **≈ $70 / month** |

Tokens are not the Layer 1 cost driver; the always-on database is.

---

## Explicitly out of scope (consumers of the seams — built in L2/L3)

- Agent control loops that hot-reload behavior from `subscribe()` / `get_page()`.
- The behavior-swap policy, rollback orchestration.
- Process spawning/supervision; how many instances per type; work-partitioning policy.
- Full version history, reranking, chunking, horizontal **server** replicas, RBAC beyond scopes+identity.
