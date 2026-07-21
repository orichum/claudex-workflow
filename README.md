# Claudex workflow

A portable Claude Code daily-driver that uses both GPT and Claude models:

```text
Claude Code -> Claudex -> Headroom -> CLIProxyAPI
                                      |-> Codex OAuth (GPT)
                                      `-> Claude OAuth (Claude)
```

The main model is `gpt-5.6-sol` at high effort. It decides automatically
whether to work inline or use a bounded specialist. `sonnet` and `opus` map to
real Claude models; GPT aliases remain `fast`, `balanced`, and `powerful`.

## Install

Supported installer paths:

- macOS 13+, Apple Silicon or Intel, using a user LaunchAgent
- glibc Linux, ARM64 or x86-64, using `systemd --user`
- WSL2 with systemd enabled; WSL1 is rejected

Prerequisites: Claude Code, `curl`, `jq`, `git`, `python3` 3.10+, `rg`, `tar`,
and `uv`. Docker with the Docker MCP Toolkit is optional but required for the
project-specific Docker profiles.

```bash
git clone https://github.com/arvind9981/claudex-workflow.git
cd claudex-workflow
./install.sh
claudex-login codex
claudex-login claude
./discover-models.sh
claudex-doctor
```

`install.sh` resolves the current stable CLIProxyAPI and Claudex GitHub
releases and verifies their GitHub-published SHA-256 digests. It installs
MemPalace, Graphify, and Headroom through `uv` only when their commands are
missing. Re-running it upgrades CLIProxyAPI and Claudex without background
auto-updates.

For package upgrades you control explicitly:

```bash
uv tool upgrade headroom-ai
uv tool upgrade mempalace
uv tool upgrade graphifyy
./install.sh
```

## Daily use

```bash
claudex-gpt          # GPT controller with mixed GPT/Claude specialists
claude-headroom      # native Claude Code through Headroom
claudex-doctor       # local configuration and service checks
```

Inside `claudex-gpt`, `/model opus` selects `claude-opus-4-8`; use
`/model gpt-5.6-sol` to return to Sol. Do not set
`CLAUDE_CODE_SUBAGENT_MODEL` globally because each specialist has its own model.

Paid smoke requests are never part of installation. Run them explicitly:

```bash
./smoke-test.sh gpt
./smoke-test.sh claude
./smoke-test.sh controller
```

## Project routing and MCPs

[`controller/project-context.json`](controller/project-context.json) maps
top-level workspace directories, not individual repositories. The supplied
mapping means every repository below `~/xebia` uses Docker profile `xebia` and
every repository below `~/complion` uses Docker profile `realtime`. Add one
entry only when you add another top-level workspace.

Each `claudex-gpt` session generates a strict MCP configuration from its launch
directory:

- Docker MCP: `docker mcp gateway run --profile <mapped-profile>`
- MemPalace: the mapped palace, with its normal writable tool surface
- Graphify: only when the current Git repository has `graphify-out/graph.json`

The controller uses Graphify before broad codebase search, consults MemPalace
when prior decisions matter, and can save concise durable decisions without a
manual workflow command. Docker create, update, comment, delete, and transition
tools remain available when the selected profile exposes them. Claude Code's
normal permission system remains the write authority; destructive or
high-impact external writes require confirmation.

## Third-party package boundary

This repository does not patch CLIProxyAPI, Claudex, Headroom, MemPalace,
Graphify, Docker MCP Toolkit, Claude Code, or their installed package files.
Integration is done through generated configuration, launchers, and small
workflow-owned adapters. If an upstream package lacks a capability, add a
separate adapter here or accept the limitation—do not modify the package.

The Frontend Design skill under `controller/plugin/skills/frontend-design/` is
vendored with its license and provenance and is treated as read-only upstream
content.

## Local state

OAuth credentials, downloaded binaries, generated configuration, logs, and
backups stay in Git-ignored `runtime/`, `bin/`, `logs/`, and `backups/` paths.
The repository contains no credential material. `claudex-gpt` also uses an
isolated `CLAUDE_CONFIG_DIR`, so it does not replace normal Claude settings.

`./rollback.sh` disables only the workflow-owned `claudex-gpt` launcher link;
it deliberately preserves credentials, services, package data, project files,
MemPalace data, and Graphify graphs.

## Upstream projects

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [Claudex](https://github.com/StringKe/claudex)
- [Claudex documentation](https://claudex.space/en/)
