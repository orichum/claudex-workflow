# MCP integrations

Orichum creates a private minimal MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| MCP_DOCKER | Context has a Docker profile and Docker is available | Project-specific Jira and other live-service tools |
| Mempalace | Context palace exists and passes ownership checks | Durable project recall and bounded memory writes |
| Graphify | Current repository has a valid `graphify-out/graph.json` | Code-structure query before broad raw search |

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
project wing. Graphify is bound to the current repository graph. This avoids
cross-project profile and memory leakage while allowing multiple projects and
sessions on one machine.
