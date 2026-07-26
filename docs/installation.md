# Installation and upgrades

## Supported hosts

- macOS on Apple Silicon or x86-64
- Linux on arm64 or x86-64 with a systemd user manager
- WSL2 with systemd enabled

Required commands are `bash`, `curl`, `gh`, `git`, `jq`, `python3` 3.10 or
newer, `rg`, `tar`, `uv`, and Claude Code. Linux and WSL also require `ss`,
normally supplied by `iproute2`.

The host Python only bootstraps installation. Orichum commands and services use
an isolated, uv-managed CPython 3.14.x.

## Install or upgrade

```bash
git clone https://github.com/arvind9981/claudex-workflow.git orichum
cd orichum
./install.sh
```

Every run is an idempotent upgrade and reconciliation pass. It:

1. validates the focused configuration and controller plugin;
2. installs the newest available CPython 3.14 patch privately;
3. installs or upgrades CLIProxyAPI, Claudex, Headroom, Mempalace, and
   Graphify;
4. probes required CLIProxyAPI and MCP behavior;
5. installs or reconciles the three resident loopback services;
6. preserves valid configuration and authentication;
7. runs `orichum doctor` and reports the final locations and ports.

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
| Managed Python versions | `~/.local/share/orichum/python/` |
| Stable private Python | `~/.local/share/orichum/bin/orichum-python` |
| Logical session state | `~/.local/share/orichum/state/` |

Use `ORICHUM_CONFIG_HOME`, `ORICHUM_DATA_HOME`, and `ORICHUM_CACHE_HOME` to
relocate these roots. Values must be absolute.

## Services

The resident services are CLIProxyAPI, Headroom, and the Orichum route proxy.
Each active physical session also owns a small Claudex translation proxy that
ends with that session.

On Linux and WSL:

```bash
journalctl --user -u orichum-cliproxy.service
journalctl --user -u orichum-headroom.service
journalctl --user -u orichum-route-proxy.service
```

On every platform:

```bash
orichum doctor
orichum config paths
```

The installer never changes the system Python, shell profiles, or another
project's environment. Upgrade staging is transactional: an unsuccessful
upgrade restores the prior managed service state.
