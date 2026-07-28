# MCP integrations

Orichum creates a private strict MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| LeanCTX | Managed binary and project root are valid | Compact source context, graphs, task overview, durable knowledge, patches, and observational shell output |
| Atlassian | Project context contains Jira credentials | Project-specific Jira read and write tools |

The LeanCTX MCP is session-scoped. It is separate from Orichum's shared
LeanCTX wire proxy, which optimizes model requests and does not expose tools.

## Project-bound Jira

Add the project context, then run the interactive Jira configuration:

```bash
orichum context add ~/xebia --pool shared
orichum context jira ~/xebia

orichum context add ~/complion --pool shared
orichum context jira ~/complion
```

The command asks for a Jira URL, username, and API token and stores them
directly in the matching entry in private
`~/.orichum/config/projects.json`. Repositories below the parent inherit the
binding; a context with `atlassian: null` loads no Atlassian process or tool
schema.

The installed [mcp-atlassian](https://github.com/sooperset/mcp-atlassian)
server exposes Jira reads and writes, including create, update, comment,
delete, and transition operations. Claude Code approval and Jira permissions
still apply.

Use `orichum context list` to inspect configured Jira URLs without showing
tokens. Re-run `orichum context jira ROOT` to update credentials; submit an
empty token to keep the existing token. Use `orichum context jira ROOT
--remove` to stop loading Jira for new sessions. Start or resume a session
after a change so a fresh physical MCP process loads the current credentials.

## Isolation and approvals

- The MCP file belongs to one verified physical session.
- LeanCTX is jailed to the resolved root.
- LeanCTX project data is shared for cross-session graph and knowledge recall;
  configuration, events, state, and cache are session-private.
- The project Jira binding and GitHub identity are frozen into session policy.
- Concurrent sessions may use different Jira credentials and repositories
  without changing global state.

LeanCTX advertises exactly eleven tools. Read, search, tree, expansion, graph,
impact, callgraph, knowledge, and overview are preapproved. `ctx_patch` and
`ctx_shell` retain normal approval because they edit text or execute commands.
`ctx_shell` remains resident for finite commands and uses a session-local empty
allowlist override, so arbitrary CLI names do not require configuration.
LeanCTX's dangerous-pattern blocks, project jail, and secret redaction remain
active. Native Bash is deferred for interactive, streaming, long-running,
rejected, or unsupported cases.
The universal `ctx_call` gateway and LeanCTX autonomy, daemon, proxy, provider,
and global-hook features remain disabled.

Atlassian operations retain normal approval. The API token is loaded by the
session process at startup and is not copied into the session MCP file.

Orichum does not use Docker MCP Toolkit. Existing Docker profiles remain
external to Orichum and are neither read, changed, nor removed.
