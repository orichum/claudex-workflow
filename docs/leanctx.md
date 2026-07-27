# LeanCTX

LeanCTX is Orichum's only code-context, code-intelligence, and durable
project-knowledge layer. It provides compact reads, search, trees, expansion,
repository graphs, impact analysis, callgraphs, task orientation, cross-session
knowledge, anchored text patches, and compressed observational shell output.

## Fixed session contract

Every physical session receives one headless LeanCTX MCP with exactly:

- `ctx_read`
- `ctx_search`
- `ctx_tree`
- `ctx_expand`
- `ctx_graph`
- `ctx_impact`
- `ctx_callgraph`
- `ctx_knowledge`
- `ctx_overview`
- `ctx_patch`
- `ctx_shell`

Orichum validates that exact surface during installation and doctor checks,
then exercises a temporary one-file graph, symbol resolution, impact analysis,
task overview, and read-only knowledge recall under the production storage
topology. Missing tools or failures in those representative code-intelligence
and memory paths fail readiness instead of silently changing model behavior.

The process is pinned to the active Git repository. A launch from a configured
multi-repository parent is pinned to that verified parent. Graphs, knowledge,
archives, and aggregate statistics use one Orichum-owned shared LeanCTX data
root. Configuration, events, state, and cache remain private to the physical
session. Concurrent sessions can recall the same project knowledge without
sharing mutable session state.

## How the graph works

LeanCTX builds its index and property graph lazily when graph, impact, or
callgraph context is first requested. Orichum does not run a separate graph
command, precompute repository output, install Git refresh hooks, or write
generated graph files into the checkout.

Use the graph tools naturally through the controller:

- symbol and relationship questions route to `ctx_graph`;
- change-risk questions route to `ctx_impact`;
- callers and callees route to `ctx_callgraph`.

The controller does not choose between LeanCTX and another graph engine.

## Durable knowledge

`ctx_overview` gives the controller a task-aware project map plus a compact
wake-up briefing. `ctx_knowledge` recalls or records project-scoped facts,
decisions, conventions, outcomes, and gotchas across sessions. Automatic
capture is enabled, but the controller policy forbids storing raw source,
command output, transient speculation, or routine recaps as durable knowledge.

LeanCTX identifies a repository independently of its checkout path, preferring
its explicit project ID and Git remote identity. Clones and worktrees of the
same repository therefore reuse the same graph and knowledge store.

## Exactness and fallback

Compressed context is for understanding. Supported text edits use an anchored
`ctx_read` followed by `ctx_patch`. Use `ctx_shell` for noisy observation such
as Git status/diff/log, tests, linters, builds, Terraform plans, and Docker or
Kubernetes inspection. Use `ctx_shell(raw=true)` for one exact diagnostic
follow-up when compressed output is insufficient.

State-changing Git operations, package installation or upgrades, service
lifecycle, deployments, infrastructure apply, authentication, and interactive
or streaming commands use native `Bash`. Orichum does not run the same command
through both paths by default.

Native `Read`, `Edit`, and `Write` remain available for unsupported formats,
binary files, exact verification, or a LeanCTX failure in the controller.
Specialists use the stricter LeanCTX surface; the implementation worker retains
native edits and Bash but not a second raw repository-reading path.

Orichum disables LeanCTX's autonomous gateway, global shell hooks, daemon,
provider connectors, request proxy, and universal `ctx_call` surface.

## Monitor savings

From a project:

```bash
orichum leanctx stats
orichum leanctx watch
orichum leanctx dashboard
```

`stats` prints a snapshot, `watch` opens the terminal observatory, and
`dashboard` starts the authenticated local web observatory in the foreground.
Stop it with Ctrl+C.

The `SOURCE`, `RETURNED`, `SAVED`, and `REDUCTION` columns describe LeanCTX tool
payloads recorded by the selected physical run only. They are not aggregate
project totals, whole-session provider-token, or billing metrics. A dash means
the called tools did not emit source-compression counters.

Select a physical run when needed:

```bash
orichum leanctx list
orichum leanctx stats --run run.mrds3ghq
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

Without `--run`, Orichum uses the current attached run or the newest run for
the current project. It does not cross project boundaries or substitute an
older run merely because it has more activity.

`list` hides incompatible historical physical runs by default. Use `--all`
when diagnosing an older session contract.

## Verify

```bash
orichum doctor
```

Doctor performs a real MCP handshake with the managed binary and verifies the
eleven-tool contract against an isolated temporary fixture. It does not index
your project or launch a model session.
