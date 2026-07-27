# Project contexts

A project context maps a parent directory to its model stack, account pools,
GitHub identity, and optional Docker MCP profile.

The parent does not need to be a Git repository. Launches from any nested
repository inherit the longest matching configured parent.

## Add a context

```bash
orichum context add ~/xebia \
  --docker xebia --github-account athevar-xebia

orichum context add ~/personal --pool shared
orichum context add ~/work --model-stack balanced \
  --pool xebia --pool shared
```

Docker is optional. Adding a context validates the directory and saves the
mapping immediately—there is no repository mining or population step. LeanCTX
builds live source indexes, graphs, and project knowledge lazily as sessions
use them. Repeat `--pool` to set an ordered fallback list. Omit `--model-stack`
to inherit the configured default stack.

## Maintain contexts

```bash
orichum context list
orichum context validate
orichum context update ~/personal \
  --pool shared --no-docker --github-account arvind9981
orichum context update ~/personal --inherit-model-stack --no-github-account
orichum context remove ~/personal
orichum context remove ~/personal --yes
```

Repositories added below a configured parent inherit the mapping
automatically. No context refresh command or Git hook is required.

When `githubAccount` is configured, Orichum creates an isolated
account-specific `GH_CONFIG_DIR` from an existing `gh auth` login. Concurrent
projects therefore do not change the machine-wide active GitHub account.

See [Memory and code intelligence](memory-and-code-graph.md) for LeanCTX
project identity, worktrees, and shared durable knowledge.
