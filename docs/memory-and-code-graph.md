# Memory and code graph

Mempalace and Graphify solve different problems:

- Mempalace recalls durable project decisions and conventions.
- Graphify describes current repository structure and relationships.

## Initial population

`orichum context add` or an explicit `orichum context populate ROOT` discovers
independent repositories, follows declared submodules, skips duplicate linked
worktrees, mines repository content into Mempalace, builds Graphify graphs, and
installs Graphify's Git hooks.

Population is a foreground, explicit operation. Progress and elapsed time are
visible; it is not a resident indexing service.

Generated `graphify-out` data is excluded from Mempalace mining. This avoids
embedding a large generated graph back into memory. It does not remove code
structure from the workflow: Graphify remains available through its own MCP.

## During a session

- The controller recalls Mempalace only when prior decisions or durable
  conventions matter.
- A hook binds every Mempalace call to the verified project wing.
- Graphify is the first structural query when a valid graph exists.
- Broad graph or memory payloads are not injected automatically into every
  prompt.

This on-demand design keeps the tools useful without paying their schema and
retrieval cost on unrelated tasks.

## Maintenance

Graphify's installed Git hooks maintain normal repository changes. Run a full
population again only after adding repositories or when intentionally
refreshing the entire context:

```bash
orichum context populate ~/xebia
orichum context validate
orichum doctor
```
