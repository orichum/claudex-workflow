# Memory and code intelligence

Orichum separates current code from durable memory:

| Question | Source |
|---|---|
| What does the current checkout contain? | LeanCTX |
| What calls this symbol or depends on this change? | LeanCTX graph, impact, and callgraph tools |
| What decision or convention did this project establish earlier? | Mempalace |

## Initial project setup

`orichum context add` and `orichum context populate ROOT` discover repositories,
skip duplicate linked worktrees, and mine the outer repository sources into the
configured Mempalace wing. Population is explicit and foreground-only; progress
and elapsed time are shown.

```bash
orichum context add ~/projects --pool shared
orichum context populate ~/projects
```

Population does not build code graphs. A new physical session starts with a
private empty LeanCTX state and builds only the index or graph needed by actual
tool calls.

## During a session

- LeanCTX reads the live checkout and owns source search, relationships,
  symbols, call paths, and impact analysis.
- Mempalace is consulted only when durable project history matters.
- Mempalace hooks bind each call to the verified project wing.
- No source, graph, or memory payload is injected into every prompt.

This keeps the workflow current and token-efficient: LeanCTX returns bounded
live context, while Mempalace avoids repeatedly rediscovering durable
decisions.

## Worktrees and multiple repositories

LeanCTX state is bound to the physical session's resolved root. A session
started inside a worktree reads that worktree. A session started inside another
clone reads that clone; no absolute-path graph artifact is shared between
them.

When launched from a configured parent such as `~/xebia`, LeanCTX is jailed to
that parent and can inspect repositories below it. Enter a specific repository
before launching when you want the narrowest index and best context efficiency.

## Maintenance

```bash
orichum context populate ~/projects
orichum context validate
orichum leanctx stats
orichum doctor
```

Repopulate Mempalace after adding repositories or when you intentionally want
to refresh durable project memory. LeanCTX needs no manual refresh command.
