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
