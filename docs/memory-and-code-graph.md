# Memory and code intelligence

LeanCTX is Orichum's single project-context layer:

| Question | LeanCTX capability |
|---|---|
| What does the current checkout contain? | Compact read, search, tree, and expansion |
| What calls this symbol or depends on this change? | Graph, impact, and callgraph |
| What decision or convention did this project establish earlier? | Project-scoped knowledge |
| What should I inspect first for this task? | Task-aware overview and wake-up briefing |

There is no separate mining or graph-generation command. Add the directory
mapping once:

```bash
orichum context add ~/projects --pool shared
```

LeanCTX indexes live source lazily when a tool needs it. Its knowledge runtime
captures compact session signals and the controller records only confirmed,
durable decisions, conventions, outcomes, and gotchas. Raw source, command
logs, and conversational recaps are not stored as durable knowledge.

## Storage and isolation

- Data that should survive sessions—graphs, knowledge, archives, and aggregate
  statistics—lives under Orichum's shared LeanCTX data root.
- Configuration, state, events, and cache belong to one physical session.
- Knowledge and graphs are scoped by LeanCTX's project identity. Git remote
  identity takes precedence over the checkout path, so clones and worktrees of
  the same repository resolve to the same project store.
- A launch from a configured multi-repository parent is jailed to that parent.
  Enter a specific repository before launching for the narrowest context.

This split provides cross-session recall without allowing one session to alter
another session's configuration or event stream.

## During a session

The controller calls `ctx_overview` once for meaningful project work to obtain
a compact task-oriented map and wake-up briefing. It uses `ctx_knowledge` only
when prior decisions matter or when a durable, confirmed fact should survive
future sessions.

Source understanding stays live: LeanCTX reads the current checkout rather than
replaying stored code. Native file tools remain available for unsupported
formats and exact fallback.

## Verify and monitor

```bash
orichum doctor
orichum leanctx list
orichum leanctx stats
orichum leanctx watch
```

`doctor` verifies the managed MCP contract. The monitoring commands show
session-local events and measured token savings. A new unused session correctly
shows no activity until the controller calls a LeanCTX tool.
