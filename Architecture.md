# Architecture — Finance GBrain (Hosted MemoryAgent), Layer 1

This document describes **Layer 1**: a live, hosted, persistent knowledge base
("finance gbrain") an LLM agent runtime can always reach. Layer 1 is the build.
Crucially, Layer 1 also ships the **interface seams** that Layers 2 and 3 require —
so those layers plug in later without re-migrating the store.

---

## 1. The three layers (scope boundary)

| Layer | What it is | This effort builds… |
|---|---|---|
| **1. Memory / knowledge + seams** | Hosted, generalized, semantic-search store + MCP tools, **plus the seams below** | **Everything** |
| 2. Control plane | "Claude prompts change agent behavior 24/7" | Only the **seams** (§9) — not the agent loops |
| 3. Fleet orchestration | Multiple agents of each type running at once | Only the **seams** (§9) — not the supervisor |

> Design rule: Layer 1 is a **memory + event + coordination service**. It exposes the
> *primitives* Layers 2/3 consume (config pages, change events, compare-and-set, leases),
> but it does **not** run agent control loops or supervise processes. Building those into
> the memory server is a category error.

---

## 2. System diagram (Layer 1 + the seams)

```
   LAYER 2 (later)            LAYER 3 (later)
   behavior control          fleet of agents (N per type)
   ┌───────────────┐         ┌───────────────────────────┐
   │ Claude writes │         │ momentum#1 momentum#2 ...  │
   │ strategy page │         │ risk#1  risk#2  ...        │
   └──────┬────────┘         └─────────────┬─────────────┘
          │ put_page(kind=strategy)        │ subscribe / claim / CAS
          │ (rw token)                     │ (per-agent tokens)
          ▼                                ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │  MEMORYAGENT SERVICE   (always-on container, single instance for MVP)     │
   │  ───────────────────────────────────────────────────────────────────────│
   │  ① Auth: read-only | read-write | per-agent identity                      │
   │  ② FastMCP app (uvicorn ASGI, stateless_http=True)                        │
   │  ③ Knowledge tools:  put_page · get_page · search · create_link           │
   │  ④ SEAMS for L2/L3:                                                        │
   │       • events:  publish-on-write  +  subscribe(namespace[,kind])         │
   │       • concurrency:  put_page(expected_version=N)  → CAS / 409           │
   │       • coordination:  claim(key,owner,ttl) · release(key,owner)          │
   │       • attribution:  written_by, agent_type, agent_instance_id           │
   │  ⑤ Indexer: text → embedding ──────────────┐                              │
   │  ⑥ /health  /docs                          │                              │
   └───────────────┬────────────────────────────┼──────────────────────────────┘
                   │ Bolt+TLS                    │ HTTPS (embed)
                   ▼                             ▼
     ┌──────────────────────────────┐   ┌──────────────────────────────┐
     │  Neo4j Aura (52b4f6d4)        │   │  DigitalOcean Gradient AI     │
     │  - :Page nodes (+ :Memory)    │   │  - embeddings: bge-m3 (1024)  │
     │  - vector index 1024/cosine   │   │  - LLM: openai-gpt-oss-120b   │
     │  - :Event log  · :Lease nodes │   │    (optional summaries)       │
     │  - graph links/traversal      │   └──────────────────────────────┘
     └──────────────────────────────┘
                   ▲ keep-alive ping (cron) — prevents Aura Free auto-pause
```

The event log and lease nodes live **in Neo4j** (durable), not in process — so the
seams survive restarts and work once Layer 3 runs multiple agents (and, later, replicas).

---

## 3. Components & where they live

| Component | Implementation | Hosted where |
|---|---|---|
| MemoryAgent (MCP server) | `FinAgents/memory/memory_server.py` — FastMCP, `app = mcp.streamable_http_app()`, stateless | Always-on container (Fly.io recommended) |
| Graph + vector store | Neo4j Aura `52b4f6d4` (pages, vector index, event log, leases) | Neo4j Aura (managed) |
| Embeddings | `bge-m3` (1024-dim) via OpenAI-compatible API | DigitalOcean Gradient AI |
| LLM (optional, brain-side summaries) | `openai-gpt-oss-120b` | DigitalOcean Gradient AI |
| MCP client | `FinAgents/memory/interface.py` (streamable-http) | Agent runtime |
| Event bus / coordination | Neo4j-backed (durable); `realtime_stream_processor` formalized | In the service + Neo4j |
| Keep-alive / backups | Cron (host scheduler / GitHub Action) | Free |

---

## 4. Provider stack (decided)

- **LLM + embeddings:** DigitalOcean Gradient AI (OpenAI-compatible). Base URL
  `https://inference.do-ai.run/v1`; OpenAI SDK auto-reads `OPENAI_API_KEY` + `OPENAI_BASE_URL`.
  - Chat: **`openai-gpt-oss-120b`**. Embeddings: **`bge-m3`** (1024-dim, cosine).
  - **Tier limit:** `openai-gpt-4o` / `anthropic-*` return `403` (account tier, not key).
- **Datastore:** Neo4j (graph-native links + durable event log/leases for the seams).
  Aura DB = `52b4f6d4`.

---

## 5. Data model (Layer 1, seam-ready)

```
(:Page {
   id, slug, namespace,           # identity; unique (namespace, slug)
   kind,                          # "knowledge" | "strategy" | "config"   ← enables L2
   title, body, tags[],
   version,                       # monotonic int; bumped per write       ← enables CAS (L2/L3)
   embedding: float[1024],        # vector-indexed (bge-m3 / cosine)
   embedding_model, embedding_dim, needs_embedding,
   written_by, agent_type, agent_instance_id,   # provenance              ← enables L3
   created_at, updated_at
})
(:Page)-[:LINKS_TO]->(:Page)      # native graph links

(:Event {                          # durable change log                    ← enables L2/L3
   namespace, slug, kind, version, op, written_by, ts
})
(:Lease {                          # atomic coordination primitive          ← enables L3
   key, owner, expires_at
})
```

Existing trading nodes (`:Memory`, `:Agent`, `:Signal`, `:Strategy`) remain; `:Page`,
`:Event`, `:Lease` are additive.

---

## 6. Locked decisions (Layer 1)

1. **Embedding model/dim — LOCKED:** `bge-m3`, **1024-dim, cosine**. Swap to any other
   1024-dim model via re-embed only (no index rebuild). Never index at 384 or 768.
2. **Retrieval unit:** page-level for MVP; chunking later (additive).
3. **Tenancy:** mandatory `namespace` from day one.
4. **Identity/writes:** UUID `id` + `slug` unique within namespace; `put_page` upserts via
   `MERGE (namespace, slug)`; `version`/`updated_at` tracked.
5. **Auth:** bearer tokens — **read-only**, **read-write**, and **per-agent identity** (for L3 attribution).
6. **Embed-on-write resilience:** store even if embedding fails; `needs_embedding=true`; cron re-embeds.
7. **Interface:** MCP-primary; `/health` + debug `GET /search`.
8. **Schema as idempotent boot code:** constraints + vector index + event/lease constraints at startup.
9. **Backups:** weekly cron dump (Aura Free has none).

---

## 7. Current state (implemented)

- ✅ Provider migrated to DigitalOcean; LLM `openai-gpt-oss-120b`.
- ✅ `.env`-driven `OPENAI_*` + `NEO4J_*`; Aura `52b4f6d4` wired & verified.
- ✅ Embeddings default = DO `bge-m3` (1024) with local fallbacks.
- ✅ Vector index `memory_embedding_index` (1024/cosine) ensured at startup.

## 8. Known gaps

- ✅ Containerized + deployed to Fly (HTTP); **stdio transport works locally against Aura** (default).
- ✅ Per-session lifespan teardown crash fixed; **stdout kept clean** for stdio MCP.
- ⛔ Embeddings not yet persisted to Neo4j (still in `memory_index.pkl`).
- ⛔ `semantic_search_memories` uses in-process cosine, not `db.index.vector.queryNodes`.
- ⛔ No `:Page` generalization / neutral tools yet.
- ⛔ No auth on the HTTP MCP path (stdio is local-only → lower risk).
- ⛔ HTTP mode still re-inits resources per request (fleet path); **stdio (default) does not**.
- ⛔ Seams (§9) not yet built.

---

## 9. Layer 1 ↔ Layer 2/3 interface seams (the point of this revision)

These are **built in Layer 1** so the later layers attach cleanly. For each: what Layer 1
provides vs. what the consumer (built later) does.

| Seam | Layer 1 provides (now) | Consumer does (L2/L3, later) | Enables |
|---|---|---|---|
| **Config-as-pages** | Store/serve `kind=strategy|config` pages by stable `(namespace, slug)` | Agents `get_page` their behavior on each loop | L2 |
| **Change events** | `publish-on-write` to a durable `:Event` log + `subscribe(namespace[,kind])` stream | Agents subscribe and **hot-reload** behavior on change | L2, L3 |
| **Compare-and-set** | `put_page(expected_version=N)` → 409 on mismatch; `version` per page | Atomic strategy swap + rollback; safe multi-writer updates | L2, L3 |
| **Claim / lease** | Atomic `claim(key, owner, ttl)` / `release` backed by `:Lease` (single-tx MERGE) | Instances coordinate so only one handles a unit of work | L3 |
| **Identity / attribution** | `written_by`, `agent_type`, `agent_instance_id` on writes + events; per-agent tokens | Trace which instance did what; partition shared vs private memory | L3 |

**Critical forward-compat rule:** the event bus and leases are **Neo4j-backed (durable)**,
never in-process-only. The memory server is `stateless_http=True`, but its stream-processor
state is in-memory today → **run a single instance for the MVP**. Externalizing
coordination to Neo4j (done via these seams) is what later permits multiple agents and,
eventually, multiple server replicas without changing the tool contract.

**What is explicitly NOT in Layer 1:** the agent control loops, the behavior-reload logic,
process spawning/supervision, and work-partitioning policy. Layer 1 hands those layers the
primitives; it does not implement their behavior.

---

## 10. Transport & provider model (gbrain-aligned)

**Transport: stdio-first.** gbrain runs `gbrain serve` as a *local stdio* MCP server over a
remote Postgres — it centralizes the **database**, not the server. We adopt the same shape:
the MemoryAgent **defaults to stdio MCP** — one long-lived process per client, all sharing the
always-on Neo4j Aura brain. In stdio mode the lifespan runs once per process, so init is
effectively a startup singleton (no per-request re-init). HTTP (`MCP_TRANSPORT=http`) is
reserved for the hosted, headless Layer 3 fleet.

- **Default (dev / Claude Code / single agent):** `claude mcp add memory -- python memory_server.py`.
- **Fleet (Layer 3, 24/7 headless):** the containerized HTTP server on a host.
- **"Always reachable" is guaranteed by Aura's uptime, not a hosted container** — removing the
  always-on-container / billing / public-auth burden for the common case. (This is the lesson
  from the Fly deploy: hosting a public MCP server was self-imposed cost; Aura already is the
  always-on shared layer.)

**Provider / dependency reductions (lean-binary ethos):**
- The events seam is **poll-based** over the durable `:Event` log (`get_events(since=cursor)`),
  not WebSocket push → **`websockets` dropped**.
- Once search runs through Neo4j's vector index + DO `bge-m3`, the in-process `scikit-learn`
  cosine and `memory_index.pkl` are dead weight → **`scikit-learn` / `scipy` dropped**.
- Net: the three deps that broke the first Fly build all disappear.

**Trust boundary on ingested content.** Web/Exa-sourced pages carry `source` + `trust`
(`trusted`|`untrusted`); untrusted content is envelope-wrapped before being surfaced to an LLM
— a lightweight take on gstack's layered prompt-injection defense, relevant because Layers 2/3
will store web research in the brain.

**What does NOT transfer from gstack:** "no MCP" (that's gstack's *browser* tool; gbrain itself
uses MCP, so we keep it) and "no multi-user" (we deliberately want multi-agent → scoped tokens,
modeled on gbrain's per-repo trust triad).
