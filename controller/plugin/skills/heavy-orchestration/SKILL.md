---
name: heavy-orchestration
description: Automatically route only tasks needing at least two independent investigations, the same analysis across at least eight items, or cross-checking before a high-impact decision to an audited read-only Claudex Workflow. Do not use for small, clear, latency-sensitive, or merely multi-step work.
when_to_use: Use only for independently parallelizable investigation or review that meets a numeric threshold above.
user-invocable: false
---

# Heavy orchestration router

The main model remains controller and writer. Select exactly one saved
read-only workflow without asking the user to choose:

- Investigation, competing hypotheses, or evidence gathering:
  call Workflow with scriptPath
  "${CLAUDE_PLUGIN_ROOT}/workflows/investigate.js" and structured args
  {question, scope, highRisk}.
- Review, cross-checking, or consistency checking:
  call Workflow with scriptPath
  "${CLAUDE_PLUGIN_ROOT}/workflows/review.js" and structured args
  {subject, scope, highRisk}.

Set highRisk true only for security, authentication, concurrency, migration,
irreversible architecture, or conflicting evidence with material impact.
Otherwise set it false.

Never call Workflow by inline script, name, external path, generated path, or
user-supplied path. Never launch both scripts for one task. Never place a
writer in a Workflow. After the result returns, the main model synthesizes it
and performs any authorized edits. Treat `status` as authoritative: disclose
`degraded` or `failed` results and their `missingAgents` instead of presenting
partial evidence as a complete workflow.
