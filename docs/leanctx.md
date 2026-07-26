# LeanCTX

LeanCTX is Orichum's live source-context layer. It gives the controller compact
file reads, structural outlines, source discovery, project trees, bounded
exploration, lossless expansion, approved text patches, and compressed shell
output without replacing Claude Code's native tools.

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
- `ctx_patch`
- `ctx_shell`

Orichum pins the process to the active Git repository and stores its config,
cache, state, and data under that session's private run directory. Concurrent
sessions therefore do not share LeanCTX state. Orichum preapproves the eight
read-only context tools. `ctx_patch` and `ctx_shell` stay under Claude Code's
normal tool approval because they change text or execute commands. Index
construction is capped at two threads per session to reduce contention when
several sessions index at once.

## What Orichum does not enable

Orichum does not use LeanCTX's setup or wrapping commands, global rules, shell
hooks, daemon, request proxy, graph, memory, provider connectors, autonomous
features, or universal `ctx_call` gateway.

Those responsibilities already belong elsewhere:

| Need | Authority |
|---|---|
| Current source reads, search, exploration, anchored patches, and compressed observational shell output | LeanCTX |
| Repository relationships and impact | Graphify |
| Durable decisions and conventions | Mempalace |
| Models, accounts, sessions, and policy | Orichum |

## Exactness and fallback

Compressed context is for understanding. An edit follows an anchored
`ctx_read` with `ctx_patch`, so the patch is based on current source context.
`ctx_shell` is for observational commands; use native `Bash` for state
changes. Native `Read`, `Edit`, and `Write` remain the fallback when LeanCTX is
missing, unsafe, or unsuitable. Decisive test and verification output remains
raw.

If LeanCTX is missing, unsafe, or the launch directory is not inside a Git
repository, Orichum omits the MCP. The session continues with Claude Code's
native read and search tools.

## Verify

```bash
orichum doctor
```

The doctor checks Orichum's managed binary with a real MCP handshake and
rejects any advertised tool outside the fixed ten-tool contract. It deliberately
does not run LeanCTX's global doctor because Orichum does not use LeanCTX's
global integration mode.
