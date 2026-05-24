# GBrain Tutorial — basic commands

A hands-on quickstart for the Finance GBrain MemoryAgent (Layer 1): a persistent,
semantic-search knowledge store backed by Neo4j Aura + DigitalOcean `bge-m3` embeddings.

You interact with it two ways:
- **CLI** (`gbrain_cli.py`) — quickest way to poke at the brain from a terminal.
- **MCP** — register it with Claude Code so the tools show up natively.

Config is read from the project `.env` (DigitalOcean + Neo4j). Nothing else to set up.

---

## Core concepts (30 seconds)

- **Page** — the unit of knowledge: `title`, `body`, `tags`, plus a `kind`.
- **slug** — the page's address within a namespace (auto-derived from the title, e.g.
  "Buffett moat checklist" → `buffett-moat-checklist`).
- **namespace** — an isolation boundary (e.g. `default`, `research`, `strategies`).
  Search and get are scoped to one namespace; they never bleed across.
- **kind** — `knowledge` (default), `strategy`, or `config`. Search can filter by it.
- **version** — bumps automatically each time you re-`put` the same slug.
- **links** — directed `LINKS_TO` edges between pages (the graph part).

---

## CLI quickstart

Run from the repo (uses the project venv):

```bash
cd FinAgents/memory
PY=../../.venv/bin/python   # or: python3
```

### 1. Put a page
```bash
$PY gbrain_cli.py put "Buffett moat checklist" \
  "Durable competitive advantage, pricing power, consistently high ROE across periods." \
  --ns demo --kind strategy --tags buffett,moat
```
→ returns JSON: `{ "slug": "buffett-moat-checklist", "version": 1, ... }`

```bash
$PY gbrain_cli.py put "Owner earnings" \
  "Net income + D&A - maintenance capex - working capital changes." \
  --ns demo --kind knowledge
```

### 2. List what's in a namespace
```bash
$PY gbrain_cli.py list --ns demo
#   owner-earnings           v1  [knowledge]  Owner earnings
#   buffett-moat-checklist   v1  [strategy]   Buffett moat checklist
```

### 3. Link two pages
```bash
$PY gbrain_cli.py link buffett-moat-checklist owner-earnings --ns demo
# {"linked": 1, ...}
```

### 4. Semantic search (by meaning, not keywords)
```bash
$PY gbrain_cli.py search "pricing power and competitive advantage" --ns demo
#   0.829  buffett-moat-checklist   [strategy]   Buffett moat checklist
#   0.733  owner-earnings           [knowledge]  Owner earnings
```
Filter by kind:
```bash
$PY gbrain_cli.py search "valuation" --ns demo --kind knowledge --limit 3
```

### 5. Fetch one page (with its links)
```bash
$PY gbrain_cli.py get buffett-moat-checklist --ns demo
# { "slug": ..., "kind": "strategy", "version": 1, "links": ["owner-earnings"], ... }
```

### 6. Versioning — re-put the same slug
```bash
$PY gbrain_cli.py put "Buffett moat checklist" "UPDATED: also require operating margin stability." --ns demo --kind strategy
# version -> 2
```

### 7. Clean up a namespace
```bash
$PY gbrain_cli.py clear --ns demo
# deleted N pages from namespace 'demo'
```

> All command output is plain JSON / text on **stdout**; diagnostics go to **stderr**.

---

## Inspect in Neo4j Browser

Open the Aura console for instance `52b4f6d4`, make sure the **database selector shows
`52b4f6d4`** (not the default `neo4j` — it doesn't exist on this instance), then:

```cypher
MATCH (p:Page {namespace:'demo'}) RETURN p
```
Confirm the vectors are stored:
```cypher
MATCH (p:Page {namespace:'demo'})
RETURN p.slug, size(p.embedding) AS dims, p.embedding_model   // dims = 1024, bge-m3
```

---

## Use it from Claude Code (MCP)

Register the stdio MCP server once:
```bash
claude mcp add memory -- python FinAgents/memory/memory_server.py
```
Now `put_page`, `get_page`, `search`, and `create_link` are first-class tools in any
Claude Code session — backed by the same always-on Aura brain. (Diagnostics stay on
stderr, so the stdio protocol channel is clean.)

---

## Command reference

| CLI | MCP tool | Purpose |
|---|---|---|
| `put "<title>" "<body>" [--ns --kind --tags --slug]` | `put_page` | create/update a page (upsert, embeds, bumps version) |
| `get <slug> [--ns]` | `get_page` | fetch a page + its links |
| `search "<query>" [--ns --kind --limit]` | `search` | namespace-scoped semantic search |
| `link <from> <to> [--ns]` | `create_link` | directed page→page link |
| `list [--ns]` | — | list pages in a namespace (CLI helper) |
| `clear [--ns]` | — | delete all pages in a namespace (CLI helper) |
