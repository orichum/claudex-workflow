# LeanCTX

LeanCTX is Orichum's live source-context layer. It gives the controller compact
file reads, source search, project trees, lossless expansion, approved text
patches, and compressed shell output without replacing Claude Code's native
tools.

## What Orichum enables

Each physical session gets one headless stdio MCP process with exactly:

- `ctx_read`
- `ctx_search`
- `ctx_tree`
- `ctx_expand`
- `ctx_patch`
- `ctx_shell`

Orichum pins the process to the active Git repository and stores its config,
cache, state, and data under that session's private run directory. Concurrent
sessions therefore do not share LeanCTX state. Orichum preapproves the four
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

## Monitor LeanCTX

From a configured project, inspect the current Orichum run. Outside a live
session, Orichum selects the newest run that has recorded LeanCTX activity:

```bash
orichum leanctx stats
orichum leanctx watch
orichum leanctx dashboard
```

`stats` prints a savings snapshot, `watch` opens LeanCTX's live terminal
monitor, and `dashboard` opens the authenticated Observatory on
`127.0.0.1`. The dashboard runs in the foreground and stops with Ctrl+C;
Orichum does not install another service. Press `q` to leave the terminal
monitor.

List physical runs or select one explicitly:

```bash
orichum leanctx list
orichum leanctx list --limit 50
orichum leanctx list --all
orichum leanctx stats --run run.mrds3ghq
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

The list shows the newest 20 physical runs by default. Use `--limit` for a
different bound or `--all` for the complete history. Without `--run`, Orichum
uses the current physical run when that identity is available. Otherwise it
selects the newest run with recorded activity for the current project, falling
back to that project's newest run only when none has activity. It never crosses
project boundaries. A physical `run.*` ID identifies one LeanCTX process and
its metrics; it is different from the logical `oc-s-*` session ID used by
`orichum resume`. Historical physical runs remain selectable with `--run`.

The web dashboard opens a browser by default. Use `--open none` to print its
local URL without opening one. It uses a temporary configuration copy, so
dashboard configuration changes are nonpersistent and cannot mutate or
invalidate a live session. Metrics, events, and activity still come from the
selected run.

## Verify

```bash
orichum doctor
```

The doctor checks Orichum's managed binary with a real MCP handshake and
rejects any advertised tool outside the fixed six-tool contract. It deliberately
does not run LeanCTX's global doctor because Orichum does not use LeanCTX's
global integration mode.
