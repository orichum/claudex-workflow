# Installation, reconciliation, and upgrades

## Supported hosts

The release-accepted host configurations are:

- macOS on Apple Silicon (native acceptance)
- Linux on x86-64 with systemd (native acceptance)
- WSL2 on x86-64 with systemd (contract acceptance)

The installer also recognizes macOS x86-64 and Linux arm64. Those paths use
the same guarded installer logic but have not completed native release
acceptance, so they are best-effort rather than release-gated targets.

Required commands are `bash`, `curl`, `gh`, `git`, `jq`, `python3` 3.10 or
newer, `rg`, `tar`, `uv`, and Claude Code. Linux and WSL also require `ss`,
normally supplied by `iproute2`.

The host Python only bootstraps installation. Orichum commands and services use
an isolated, uv-managed CPython 3.14.x.

## Install

```bash
git clone https://github.com/orichum/claudex-workflow.git orichum
cd orichum
./install.sh
```

The first run performs the complete installation. It:

1. validates the focused configuration and controller plugin;
2. installs the newest available CPython 3.14 patch privately;
3. installs or upgrades CLIProxyAPI, Claudex, and LeanCTX;
4. probes required CLIProxyAPI behavior and the exact bounded MCP surfaces;
5. installs or reconciles the shared loopback services;
6. preserves valid configuration and authentication;
7. runs `orichum doctor` and reports the final locations and ports once a
   provider route is available.

Without a logged-in provider, installation completes in
`pending-provider-login` state and prints the login command instead of failing
the full route check.

## Fast reconciliation or explicit upgrade

For normal maintenance, run:

```bash
./install.sh
# Fast reconciliation; no upstream checks when verified and healthy.
```

An unchanged healthy installation targets completion in about 10 seconds.
Orichum verifies its private install-state manifest, checks owned services and
critical runtime readiness, and reuses matching components. A missing or
damaged component is repaired without upgrading unrelated tools. Fresh
installations automatically use the complete path. Repairs can take longer
than the fast-path target.

The installer preserves an existing `~/.config/orichum/model-stacks.json`
because it may contain user-created stacks. Repository default-model changes
apply automatically only to fresh installations. Existing users can review and
adopt newer defaults without losing custom stacks through:

```bash
orichum stack available
orichum stack configure
```

Install and uninstall share one per-user lifecycle lock at
`~/.local/state/orichum/install.lock`. Even when `ORICHUM_DATA_HOME` is
relocated, two processes cannot concurrently replace the same launcher or user
services. The lock directory exists only while a lifecycle operation is active.

To deliberately resolve current external releases and upgrade every managed
runtime, run:

```bash
./install.sh --upgrade
# Resolve releases, run complete probes, and run the full doctor.
```

Verified state is stored at
`~/.local/share/orichum/state/install-state.json`. The private manifest contains
component identities and digests, not secrets. Do not edit it; the installer
discards invalid state and safely reconciles the installation.

If a preferred port belongs to an existing Orichum service, the installer
reconciles and reuses it. It does not overwrite an unknown process. Interactive
installation offers another port; non-interactive installation selects the
next available port.

## Installed locations

| Purpose | Default |
|---|---|
| Command | `~/.local/bin/orichum` |
| Editable configuration | `~/.config/orichum/` |
| Binaries, auth, logs, and service state | `~/.local/share/orichum/` |
| Managed LeanCTX binary | `~/.local/share/orichum/bin/lean-ctx` |
| Managed Python versions | `~/.local/share/orichum/python/` |
| Stable private Python | `~/.local/share/orichum/bin/orichum-python` |
| Logical session state | `~/.local/share/orichum/state/` |
| Verified install state | `~/.local/share/orichum/state/install-state.json` |

Use `ORICHUM_CONFIG_HOME`, `ORICHUM_DATA_HOME`, and `ORICHUM_CACHE_HOME` to
relocate these roots. Values must be absolute.

## Services

The shared resident services are CLIProxyAPI and the Orichum route proxy. Each
active physical session also owns its Claudex translation proxy. Together,
these are the three services on a request path.

On Linux and WSL:

```bash
journalctl --user -u orichum-cliproxy.service
journalctl --user -u orichum-route-proxy.service
```

On every platform:

```bash
orichum doctor
orichum config paths
```

The installer never changes the system Python, shell profiles, or another
project's environment. Upgrade staging is transactional: an unsuccessful
upgrade restores the prior managed binaries and service state. Orichum installs
LeanCTX directly from its verified release asset; it never runs LeanCTX
`wrap`, `setup`, `onboard`, `init`, or proxy commands.

## Uninstall

Run uninstall from the Orichum checkout:

```bash
./install.sh --uninstall
```

This stops and removes only verified Orichum-owned services, removes the
`orichum` launcher, and deletes replaceable managed runtime files. It preserves:

- provider credentials and named accounts;
- model and project configuration;
- Claude and Orichum session state;
- LeanCTX project knowledge and graphs.

That preserved state is reused if you run `./install.sh` again.

To also permanently delete Orichum's data and configuration roots:

```bash
./install.sh --uninstall --purge
```

Purge removes saved Orichum credentials, sessions, project configuration, and
Orichum-managed LeanCTX data. It does not delete the repository checkout.

Neither mode uninstalls standalone Claude Code, CLIProxyAPI, Claudex, LeanCTX,
or uv installations. If a service definition or launcher with an
Orichum name is not verifiably owned by this setup, uninstall stops before
changing anything.
