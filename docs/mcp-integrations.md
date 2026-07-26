# MCP integrations

Orichum creates a private minimal MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| MCP_DOCKER | Context has a Docker profile and Docker is available | Project-specific Jira and other live-service tools |
| Mempalace | Context palace exists and passes ownership checks | Durable project recall and bounded memory writes |
| Graphify | A current central graph exactly matches the repository state | On-demand code-structure query before broad raw search |

## MCP_DOCKER profiles

The project context names the profile:

```bash
orichum context add ~/xebia --docker xebia
orichum context add ~/complion --docker realtime
```

Every repository below that parent inherits the matching profile. A project
without a Docker profile simply omits MCP_DOCKER; it is not forced to use a
global read-only profile.

Create, update, comment, delete, transition, and other write tools remain
available when the selected Docker profile exposes them. Claude Code's normal
tool approval and the live service's own authorization still apply.

## Isolation

The generated MCP file belongs to one verified session context and is launched
with strict MCP configuration. Mempalace inputs are rewritten to the verified
project wing. For Graphify, the matching graph in private central storage is a
validated source, not the MCP's live target. Each physical session copies its
bytes into private `run_dir/graph.json` with mode `0600`, records the digest in
immutable context, and points `mcp.json` at that snapshot.

Session startup does not build or refresh a graph. Materialization retries once
against the latest stable validated binding. If it obtains a stable match, the
physical run snapshots the graph and includes Graphify. If no valid binding
exists or instability persists, it creates the physical session without
Graphify. Run `orichum graph .` explicitly and start or resume into a new
physical run when a graph is needed. An existing physical session keeps its
immutable snapshot even if the central graph is later replaced. Queries are
still on demand after Graphify is loaded; no graph payload is injected into
every prompt.

This avoids cross-project profile, memory, and graph leakage while allowing
multiple projects, clones, worktrees, and sessions on one machine.
