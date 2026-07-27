# Subagents

Orichum uses a subagent-driven workflow without requiring manual workflow
commands. The controller decides when a role-specific specialist will
materially reduce uncertainty or controller context.

## Policy

- Bounded, clear tasks stay inline.
- Independent repository investigation can use an explorer.
- Verification can use a separate verifier.
- Correctness and architecture specialists are reserved for relevant risk.
- The controller remains the sole writer and synthesizes all findings.
- Generic agents and arbitrary workflows are denied.
- Ultra effort is not the default; controller effort is high.

The installed controller plugin contains audited agent definitions and saved
workflow scripts. A `PreToolUse` hook rejects undeclared agent types and
arbitrary workflow bodies or names. This prevents a model from bypassing the
declared roles while keeping specialist reasoning and tool access intact.

Every specialist reuses the session's project-jailed LeanCTX MCP:

- explorers, verifiers, critics, and architects receive only bounded read,
  search, tree, expansion, graph, impact, and callgraph tools;
- the implementation worker also receives anchored patching, `ctx_shell` for
  noisy observation, native edits, and native `Bash` for mutations or
  interactive/streaming processes;
- project overview and durable knowledge remain controller-owned, avoiding
  repeated orientation calls and concurrent memory writes;
- raw native read/search tools are not exposed to specialists, so repository
  context does not silently bypass compression.

Session materialization and resume verify this tool contract together with each
role's frozen model. A modified or outdated agent definition is rejected before
the session continues.

Runtime limits live in `runtime.json`:

```json
{
  "controller": {
    "effort": "high",
    "maxToolUseConcurrency": 3,
    "maxSubagentsPerSession": 24
  }
}
```

These values bound fan-out and concurrency; they do not truncate a worker's
response. Defining a specialist in a model stack makes it available, not
mandatory on every request.
