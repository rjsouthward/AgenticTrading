# Deploy — Finance GBrain Layer 1

How to take the MemoryAgent from local to a **live, always-reachable hosted instance**,
what **you (the human) must set up ahead of time**, and the deployment constraints that
keep the **Layer 2/3 seams** (events, leases, per-agent identity) working as the system grows.

---

## 0. Prerequisites you must provide (READ FIRST)

Only **you** can do these (accounts, billing, secrets, decisions). Blocking = ⛔.

### Accounts & credentials
- [x] **DigitalOcean Gradient key** — in `.env` (`doo_v1_…`, gitignored). Tier 403s `gpt-4o`/Anthropic.
- [x] **Neo4j Aura instance** `52b4f6d4` — creds in `.env`.
- [ ] **(Optional — fleet only) Hosting account** for the HTTP container — e.g. **Fly.io**.
  **Not required for the Layer 1 demo** (stdio + always-on Aura). Needed only for the Layer 3
  headless fleet. Note: Fly **trial machines stop after 5 min without a credit card** — add
  billing for an always-on HTTP instance. (flyctl is installed; `agentictrading` app exists.)
- [x] **Generate API tokens** — `MEMORY_API_TOKEN_RW` + `MEMORY_API_TOKEN_RO` generated and in `.env`.
  - 🔌 **Per-agent tokens (for L3 later):** plan to mint one token per agent identity so writes
    can be attributed (`written_by`). For the Layer 1 demo, the two scope tokens suffice.

### Decisions
- [x] **Neo4j always-on strategy: CHOSEN → A (Aura Free + keep-alive ping).** Free; node caps;
  no backups → weekly export cron is required (see §6). Upgrade to Aura Professional later if needed.
- [ ] **Backup destination** (Aura Free has none): private git repo or object-storage bucket.

### Local tooling
- [x] Python virtualenv (`.venv`) with `requirements.txt`.
- [ ] **Docker Desktop** locally — optional (Fly can build remotely).
- [ ] **Git remote** for this repo if using git-push deploys.

> **Blocking minimum (revised — gbrain stdio model):** **none for the Layer 1 demo.** Default
> transport is stdio against the always-on Aura brain, so **Fly is now optional (fleet-only)**.
> Tokens ✅ and Neo4j strategy ✅ are done. Fly billing/auth matters *only* for the hosted HTTP
> fleet (Layer 3) — the 5-minute trial-stop we hit is irrelevant to the stdio demo.

---

## 1. Environment variables

`.env` locally (gitignored); **host secrets** in prod (never in the image).

| Variable | Purpose | Source |
|---|---|---|
| `OPENAI_API_KEY` | DigitalOcean model access key | `.env` ✅ |
| `OPENAI_BASE_URL` | `https://inference.do-ai.run/v1` | `.env` ✅ |
| `NEO4J_URI` | `neo4j+s://52b4f6d4.databases.neo4j.io` | `.env` ✅ |
| `NEO4J_USERNAME` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | Aura creds (`52b4f6d4`) | `.env` ✅ |
| `MEMORY_API_TOKEN_RW` / `MEMORY_API_TOKEN_RO` | bearer tokens | **you generate** |
| `MEMORY_AGENT_TOKENS` (later) | 🔌 per-agent identity→token map (L3 attribution) | **you generate, later** |

---

## 2. Run locally

**Default — stdio MCP (gbrain model):** one local process sharing the always-on Aura brain.

```bash
# register with Claude Code (or any stdio MCP client)
claude mcp add memory -- python FinAgents/memory/memory_server.py
# or run directly (MCP_TRANSPORT defaults to stdio; reads .env)
python FinAgents/memory/memory_server.py
```

In stdio mode the lifespan runs **once per process** (init is a startup singleton) and all
diagnostics go to **stderr** — stdout is the JSON-RPC channel.

**HTTP mode (hosted fleet only):**

```bash
cd FinAgents/memory
MCP_TRANSPORT=http uvicorn memory_server:app --host 0.0.0.0 --port 8000
curl localhost:8000/health
```

HTTP `/mcp` is streamable-HTTP/stateless. Change events (once built) are **poll-based**
(`get_events(namespace, since_cursor)`), not a streaming subscribe.

---

## 3. Containerize (Phase 5)

`Dockerfile` (repo root):

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY FinAgents/ ./FinAgents/
WORKDIR /app/FinAgents/memory
EXPOSE 8000
CMD ["uvicorn", "memory_server:app", "--host", "0.0.0.0", "--port", "8000"]
```

Embeddings use the DO API (no local model) → light image; no `torch`/`sentence-transformers` in prod.

---

## 4. Deploy to Fly.io

```bash
fly auth login
fly launch --no-deploy                 # set internal_port = 8000 in fly.toml
fly secrets set \
  OPENAI_API_KEY=... OPENAI_BASE_URL=https://inference.do-ai.run/v1 \
  NEO4J_URI=neo4j+s://52b4f6d4.databases.neo4j.io NEO4J_USERNAME=52b4f6d4 \
  NEO4J_PASSWORD=... NEO4J_DATABASE=52b4f6d4 \
  MEMORY_API_TOKEN_RW=... MEMORY_API_TOKEN_RO=...
fly deploy
curl https://<app>.fly.dev/health
```

Health check → `/health`. Client: `MCP_SERVER_URL=https://<app>.fly.dev/mcp` + header
`Authorization: Bearer <MEMORY_API_TOKEN_RW>`.

### ⚠️ Scaling constraint that protects the L2/L3 seams
**Run a single instance for the MVP.** The change-event/lease seams are designed to be
**Neo4j-backed (durable)** precisely so multiple *agents* (Layer 3) can use them — but the
in-process stream-processor state in the server is **not** shared across **server replicas**.
Do not scale the MemoryAgent past one container until that state is fully externalized to
Neo4j/Redis. Many agents against **one** server instance is fine and is the L3 MVP target.

---

## 5. Keep-alive (Aura Free anti-pause) — Phase 6

Cron every few hours curling `/health` (GitHub Action, Fly scheduled machine, or uptime pinger)
keeps the app and Aura Free awake — important once Layer 2 agents poll/subscribe 24/7.

---

## 6. Backups — Phase 6

Aura Free has no backups. Weekly cron exports the graph (APOC `apoc.export.cypher` or Cypher
dump) — **including `:Page`, `:Event`, `:Lease`** — to your private git repo / bucket.

---

## 7. Security checklist (before public exposure)

- [ ] Bearer-token middleware live; unauth → 401.
- [ ] Write/prune require **read-write**; search/get accept **read-only**.
- [ ] 🔌 Per-agent tokens stamp `written_by` (L3 attribution).
- [ ] Secrets only as host secrets / `.env` — never committed, never in argv.
- [ ] **Single server instance** (in-memory stream state not yet replica-safe).
- [ ] `/health` the only unauthenticated route.

---

## 8. Quick reference — live resources

| Resource | Value |
|---|---|
| DigitalOcean inference | `https://inference.do-ai.run/v1` |
| Chat model | `openai-gpt-oss-120b` |
| Embedding model | `bge-m3` (1024-dim, cosine) |
| Neo4j Aura URI | `neo4j+s://52b4f6d4.databases.neo4j.io` |
| Neo4j database | `52b4f6d4` |
| MCP endpoint (prod) | `https://<host>/mcp` |
| Health endpoint | `https://<host>/health` |
| Seam tools (Layer 1) | `put_page` · `get_page` · `search` · `create_link` · `subscribe` · `claim`/`release` · CAS via `expected_version` |
