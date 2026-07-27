# MCP integrations

Orichum creates a private strict MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| LeanCTX | Managed binary and project root are valid | Live reads, search, tree, graph, impact, callgraph, patches, and observational shell output |
| MCP_DOCKER | Project context declares a Docker profile | Project-specific Jira and other live-service tools |
| Mempalace | Project palace passes ownership checks | Durable project recall and bounded memory writes |

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
apply. Orichum does not activate or switch the user's global Docker MCP profile;
the selected profile is passed directly to that session's gateway command.

## Isolation

- The MCP file belongs to one verified physical session.
- LeanCTX is jailed to the resolved root and stores all state in
  `run_dir/leanctx`.
- Mempalace calls are bound to the verified project wing.
- Docker profile and GitHub identity are frozen into the session policy.
- Concurrent sessions may use different profiles, accounts, repositories, and
  LeanCTX indexes without changing global state.

LeanCTX advertises exactly nine tools. Its read, search, tree, expansion,
graph, impact, and callgraph tools are preapproved. `ctx_patch` and `ctx_shell`
retain normal approval because they edit text or execute commands. The
universal `ctx_call` gateway and LeanCTX autonomy, daemon, proxy, memory,
provider, and global-hook features are disabled.

Mempalace read tools are preapproved after the wing-binding hook validates
them. Mempalace writes and MCP_DOCKER operations retain normal approval.

This avoids cross-project profile, memory, and code-context leakage while
keeping one deterministic live-code surface.
