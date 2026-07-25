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
required Docker profile or palace, that the current repository has a valid
Graphify graph, and that `orichum doctor` finds the MCP binaries.

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
