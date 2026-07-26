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
