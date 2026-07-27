# LeanCTX

LeanCTX is Orichum's only live code-context and code-intelligence layer. It
provides compact reads, search, trees, expansion, repository graphs, impact
analysis, callgraphs, anchored text patches, and compressed observational shell
output.

## Fixed session contract

Every physical session receives one headless LeanCTX MCP with exactly:

- `ctx_read`
- `ctx_search`
- `ctx_tree`
- `ctx_expand`
- `ctx_graph`
- `ctx_impact`
- `ctx_callgraph`
- `ctx_patch`
- `ctx_shell`

Orichum validates that exact surface during installation and doctor checks,
then builds a temporary one-file graph, resolves its symbol, and runs impact
analysis. Unexpected, missing, or non-functional tools fail readiness instead
of silently changing model behavior.

The process is pinned to the active Git repository. A launch from a configured
multi-repository parent is pinned to that verified parent. Config, cache, index,
graph, and state remain under the physical session's private run directory.
Concurrent sessions do not share or mutate each other's LeanCTX state.

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

## Exactness and fallback

Compressed context is for understanding. Supported text edits use an anchored
`ctx_read` followed by `ctx_patch`. `ctx_shell` is observational; state-changing
Git, package, service, deployment, and infrastructure commands use native
`Bash`.

Native `Read`, `Edit`, and `Write` remain available for unsupported formats,
binary files, exact verification, or a LeanCTX failure. They are fallbacks, not
parallel optimizers.

Orichum disables LeanCTX's autonomous gateway, global shell hooks, daemon,
provider connectors, memory, request proxy, and universal `ctx_call` surface.

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

Select a physical run when needed:

```bash
orichum leanctx list
orichum leanctx stats --run run.mrds3ghq
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

Without `--run`, Orichum uses the current attached run or the newest run for
the current project. It does not cross project boundaries or substitute an
older run merely because it has more activity.

## Verify

```bash
orichum doctor
```

Doctor performs a real MCP handshake with the managed binary and verifies the
nine-tool contract against an isolated temporary fixture. It does not index
your project or launch a model session.
