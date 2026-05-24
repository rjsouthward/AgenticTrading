---
description: Save to / search / load the finance GBrain (Neo4j memory). Usage: /gbrain <save|add|search|get|link> ...
argument-hint: <save|add|search|get|link> ...
allowed-tools: Read, mcp__memory__put_page, mcp__memory__get_page, mcp__memory__search, mcp__memory__create_link
---

You are operating the **finance GBrain** — a persistent knowledge base exposed by the MCP
server registered as `memory` (tools: `put_page`, `get_page`, `search`, `create_link`).

**Prerequisite:** the server must be registered once with
`claude mcp add memory -- python FinAgents/memory/memory_server.py`.
If the `mcp__memory__*` tools are unavailable, tell the user to run that command and stop.

Parse the request from this input: **$ARGUMENTS**

Choose the action from the first word:

- **add `<path-to-file.md>` [--ns NS] [--kind KIND]**
  1. `Read` the file at the given path.
  2. Derive `title` from the first `# H1` heading, else the filename (no extension).
  3. Choose `kind`: use `--kind` if given; otherwise infer — `strategy` if the doc reads
     like a playbook/checklist of rules, else `knowledge`.
  4. Call `put_page(title, body=<full file contents>, namespace=NS or "default", kind,
     tags=<a few derived from headings>, source=<the path>)`.
  5. Report the resulting slug + version.

- **save `<text…>` [--ns NS]** — derive a concise title and call `put_page` with the text as body.

- **search `<query…>` [--ns NS] [--kind KIND]** — call `search`; show results as
  `score  slug  [kind]  title`, ranked best-first.

- **get `<slug>` [--ns NS]** — call `get_page`; show the page and its links.

- **link `<from-slug> <to-slug>` [--ns NS]** — call `create_link`.

Default namespace is `default` unless `--ns` is given. After any write, confirm what was
stored (namespace, slug, version). Keep responses concise.
