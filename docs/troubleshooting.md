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
required Docker profile and that `orichum doctor` finds the managed LeanCTX
binary.

LeanCTX is included for every configured project, including a multi-repository
parent such as `~/xebia`. Run `orichum leanctx list`; the default view contains
only attached runs. Use `orichum leanctx list --all` to inspect incompatible
historical runs. If a required session says `ATTACHED no`, rerun `./install.sh`
and start a new physical session; existing session MCP files are immutable.
`orichum doctor` also verifies that the product-managed controller policy is
current and that LeanCTX advertises exactly Orichum's eleven allowed tools.
Orichum does not depend on a global LeanCTX setup or shell hook.

## LeanCTX has no activity or graph results

Confirm that the current physical run is attached:

```bash
orichum leanctx list
orichum leanctx stats
orichum doctor
```

If `--all` shows `ATTACHED no`, reinstall and start a new physical session;
existing session MCP files are immutable. If the run is attached but has no
events, the model has not called a LeanCTX tool. Graph and impact indexes are
built lazily, so an unused session correctly reports zero activity.

If a graph or impact call fails, verify that Orichum was launched from the
intended repository or configured parent. Start a new session from the narrower
repository root when a multi-repository parent produces too much scope.

## Prior project knowledge is missing

Confirm that the current run is attached and that Orichum was launched from
the intended repository:

```bash
orichum leanctx list
orichum doctor
```

LeanCTX scopes knowledge by project identity. Repositories with the same Git
remote share durable knowledge even when cloned elsewhere. Unrelated
repositories and configured parent directories remain separate.

## Installer port conflict

The installer reuses only verified Orichum-owned listeners. It does not replace
an unknown service. Interactive installation offers another port;
non-interactive installation selects the next available port and persists it.
