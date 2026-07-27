# MCP integrations

Orichum creates a private strict MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| LeanCTX | Managed binary and project root are valid | Compact source context, graphs, task overview, durable knowledge, patches, and observational shell output |
| MCP_DOCKER | Project context declares a Docker profile | Project-specific Jira and other live-service tools |

## MCP_DOCKER profiles

The parent-directory context selects the profile:

```bash
orichum context add ~/xebia --docker xebia
orichum context add ~/complion --docker realtime
```

Repositories below that parent inherit the same profile. A context without
`--docker` simply omits MCP_DOCKER.

Profile tools may include create, update, comment, delete, and transition
operations. Claude Code approval and the live service's authorization still
apply. Orichum does not activate or switch the user's global Docker MCP
profile; it passes the selected profile directly to that session's gateway.

## Isolation and approvals

- The MCP file belongs to one verified physical session.
- LeanCTX is jailed to the resolved root.
- LeanCTX project data is shared for cross-session graph and knowledge recall;
  configuration, events, state, and cache are session-private.
- Docker profile and GitHub identity are frozen into the session policy.
- Concurrent sessions may use different profiles, accounts, and repositories
  without changing global state.

LeanCTX advertises exactly eleven tools. Read, search, tree, expansion, graph,
impact, callgraph, knowledge, and overview are preapproved. `ctx_patch` and
`ctx_shell` retain normal approval because they edit text or execute commands.
The universal `ctx_call` gateway and LeanCTX autonomy, daemon, proxy, provider,
and global-hook features remain disabled.

MCP_DOCKER operations retain normal approval. This keeps one deterministic
code-and-memory surface while preventing cross-project external-service
leakage.
