# Model stacks

A model stack assigns a controller model and ordered candidates for specialist
roles. It separates task roles from provider credentials, allowing the same
workflow to use GPT, Claude, Google, Kimi, or another declared family.

## Build a stack

```bash
orichum stack available
orichum stack configure
```

The wizard reads the live CLIProxyAPI model catalogue. For each role, it lets
you choose a live model and either:

- select automatically within one provider; or
- lock the route to one named account.

The final review validates the routes again before saving. The wizard can then
assign the stack to the longest matching project context for the current
directory.

## Inspect and validate

```bash
orichum stack list
orichum stack show STACK
orichum models list
orichum models stacks
orichum models resolve
orichum models resolve STACK
orichum models validate
```

Candidates in a role are ordered startup choices. Runtime fallback is separate:
session creation freezes an exact primary route and at most one compatible
fallback.

Portable stack definitions live in `model-stacks.json`. Machine-local named
account locks live privately in `stack-bindings.json`. Editing a stack does not
mutate existing sessions; start a new session or fork an existing one to use
the new definition.

The standard roles are controller, repository explorer, repository verifier,
correctness critic, architecture advisor, and implementation worker. Runtime
policy decides whether specialists are needed; defining them does not cause
automatic fan-out on every task.

## Shipped balanced stack

| Role | Ordered default | Why |
|---|---|---|
| Controller | GPT-5.6 Sol | Primary coordinator and sole writer |
| Repository explorer | GPT-5.6 Terra | Efficient bounded reconnaissance |
| Repository verifier | GPT-5.6 Terra | Independent read-only verification |
| Correctness critic | Claude Sonnet 5 through Anthropic | Strong routine review without paying Opus cost |
| Architecture advisor | Claude Opus 5 through Anthropic, then Claude Opus 4.6 Thinking through Antigravity | Highest configured architecture model per provider |
| Implementation worker | GPT-5.6 Sol | Strong execution inside an explicit ownership boundary |

Ordered candidates are evaluated only while creating a session. For example,
the architecture advisor uses Anthropic Opus 5 when that route is live and uses
the declared Antigravity candidate only when the first candidate cannot be
bound. The selected route is then frozen with the session; Orichum does not
silently change the model of an existing session.

These defaults are ordinary entries in `model-stacks.json`. Edit that file or
run `orichum stack configure` to change them; no agent definition needs to be
rewritten.
