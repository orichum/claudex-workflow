# LeanCTX

LeanCTX is Orichum's live source-context layer. It gives the controller compact
file reads, structural outlines, source discovery, project trees, bounded
exploration, lossless expansion, and compressed shell output without replacing
Claude Code's native tools.

## What Orichum enables

Each physical session gets one headless stdio MCP process with exactly:

- `ctx_read`
- `ctx_delta`
- `ctx_search`
- `ctx_glob`
- `ctx_tree`
- `ctx_outline`
- `ctx_explore`
- `ctx_expand`
- `ctx_shell`

Orichum pins the process to the active Git repository and stores its config,
cache, state, and data under that session's private run directory. Concurrent
sessions therefore do not share LeanCTX state. Orichum preapproves the eight
read-only context tools. `ctx_shell` stays under Claude Code's normal tool
approval because it executes commands. Index construction is capped at two
threads per session to reduce contention when several sessions index at once.

## What Orichum does not enable

Orichum does not use LeanCTX's setup or wrapping commands, global rules, shell
hooks, daemon, request proxy, graph, memory, provider connectors, autonomous
features, editing tools, or universal `ctx_call` gateway.

Those responsibilities already belong elsewhere:

| Need | Authority |
|---|---|
| Current source reads, search, exploration, and compressed observational shell output | LeanCTX |
| Repository relationships and impact | Graphify |
| Durable decisions and conventions | Mempalace |
| Request-path compression | Headroom |
| Models, accounts, sessions, and policy | Orichum |

## Exactness and fallback

Compressed context is for understanding. Before editing, the controller must
retrieve the exact current bytes with a raw or fresh LeanCTX read, or use
Claude Code's native read tool. Decisive test and verification output remains
raw.

If LeanCTX is missing, unsafe, or the launch directory is not inside a Git
repository, Orichum omits the MCP. The session continues with Claude Code's
native read and search tools.

## Verify

```bash
orichum doctor
```

The doctor checks Orichum's managed binary with a real MCP handshake and
rejects any advertised tool outside the fixed nine-tool contract. It deliberately
does not run LeanCTX's global doctor because Orichum does not use LeanCTX's
global integration mode.
