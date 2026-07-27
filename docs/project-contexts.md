# Project contexts

A project context maps a parent directory to its model stack, account pools,
GitHub identity, Docker MCP profile, Mempalace palace and wing, and discovered
repositories.

The parent directory does not need to be a Git repository. Launches from any
nested repository inherit the longest matching configured parent.

## Add a context

```bash
orichum context add ~/xebia \
  --docker xebia --github-account athevar-xebia

orichum context add ~/personal --pool shared
```

Docker is optional. A context without an MCP_DOCKER profile still receives its
stack, account pool, memory, LeanCTX, and GitHub identity settings.

Adding a context is a one-time foreground operation. Orichum:

1. discovers a repository at the root or independent repositories below it;
2. follows declared Git submodules;
3. skips duplicate linked worktrees of the same repository;
4. mines each repository into the selected Mempalace wing;
5. saves the context only after population succeeds.

Progress and elapsed time are printed by default.

## Maintain contexts

```bash
orichum context list
orichum context validate
orichum context populate ~/xebia
orichum context update ~/personal \
  --pool shared --no-docker --github-account arvind9981
orichum context remove ~/personal
orichum context remove ~/personal --yes
```

Run `populate` when repositories were added after initial setup or when you
explicitly want to refresh durable project memory. LeanCTX builds live source
indexes and graphs lazily inside each physical session; it needs no project
population command or Git refresh hook.

See [Memory and code intelligence](memory-and-code-graph.md) for worktrees,
multiple repositories, and the LeanCTX/Mempalace boundary.

When `githubAccount` is configured, Orichum creates an isolated
account-specific `GH_CONFIG_DIR` from an existing `gh auth` login. Concurrent
projects therefore do not change the machine-wide active GitHub account.
