# Troubleshooting

Start with:

```bash
orichum doctor
orichum config paths
orichum context list
orichum stack list
orichum provider accounts
```

## Bound routes are not live

The selected stack references a provider/model route that CLIProxyAPI is not
currently advertising. Confirm provider login, account status, pool membership,
and live models:

```bash
orichum stack available
orichum stack show STACK
orichum provider accounts
orichum models resolve STACK
```

Create or update the stack with `orichum stack configure`. Existing logical
sessions keep their frozen routes.

## Inference gateway connection refused

This means the physical session could not reach its private Claudex translator
or one of the resident loopback services. Run `orichum doctor`, then inspect
service logs. Restart through a fresh `orichum` or `orichum resume SESSION_ID`
after the health check passes.

Linux and WSL logs:

```bash
journalctl --user -u orichum-cliproxy.service
journalctl --user -u orichum-headroom.service
journalctl --user -u orichum-route-proxy.service
```

## Wrong GitHub identity

Confirm the context and authenticated accounts:

```bash
orichum context list
gh auth status --hostname github.com
```

Set the expected identity with `orichum context update ROOT
--github-account ACCOUNT`. New physical sessions receive an isolated
`GH_CONFIG_DIR`; the global active account is not switched.

## Missing MCP

MCPs are intentionally conditional. Check that the project context has the
required Docker profile or palace and that `orichum doctor` finds the MCP
binaries.

LeanCTX is included only inside a Git repository. `orichum doctor` verifies the
managed binary and confirms that it advertises exactly Orichum's four allowed
tools. If the check fails, rerun `./install.sh`; Orichum does not depend on a
global LeanCTX setup or shell hook.

For Graphify, inspect the exact repository state:

```bash
orichum graph status .
```

The Graphify MCP is omitted when its central graph is missing, stale, invalid,
or does not match the current clean or dirty state. Session startup never
rebuilds it. Run `orichum graph .`, confirm the status is `current`, then start
a new session.

The same command handles every graph condition: it creates a missing graph,
incrementally updates a current graph, and repairs a stale or invalid graph
with a fresh code-only extraction. Repair builds in a staging directory,
validates the result, then activates it atomically. If extraction, validation,
or activation fails, Orichum preserves the old stale or invalid target for
diagnosis instead of replacing it with partial output.

## Wrong graph identity or no graph identity

Graph status reports `(invalid)` when a repository has no configured identity
and no usable fetch remote, or when multiple fetch remotes are ambiguous.
Inspect remotes, then set a stable override if needed:

```bash
git remote -v
orichum graph identity . --set github.com/xebia/X-ACE-UI
orichum graph .
```

Use `orichum graph identity . --clear` to return to remote-derived identity.
Changing identity selects a different central namespace; it does not move or
delete graphs stored under the old identity.

## Graph does not match after an edit or checkout

Dirty content selects an isolated working graph rather than the clean revision
graph. A commit or checkout changes the selected state and the installed Git
hook launches a detached refresh. Git returns before Graphify finishes, so
status can briefly show a missing graph:

```bash
orichum graph status .
orichum graph .
```

The explicit command waits for the refresh and is the deterministic recovery
path. It also reinstalls the marked post-commit and post-checkout hook sections
without replacing unrelated user hook content.

## Legacy repository-local Graphify output

Active Graphify output belongs in Orichum's private data directory. A
recognized repository-local `graphify-out` is migration input only. The next
`orichum graph .` migrates it transactionally when no central graph is active
for that state. If migration reports unknown or unsafe entries, preserve the
directory and inspect it; Orichum will not delete unrecognized data.

## Population appears slow

Population prints a stage, repository number, operation, and elapsed time.
Large first-time mining and graph extraction can take several minutes. Cancel
with Ctrl-C if necessary; the project mapping is committed only after successful
population. Re-run the same explicit command to continue from current tool
state.

## Installer port conflict

The installer reuses only verified Orichum-owned listeners. It does not replace
an unknown service. Interactive installation offers another port;
non-interactive installation selects the next available port and persists it.
