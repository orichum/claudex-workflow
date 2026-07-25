# Orichum

> One local control plane for Claude Code, multiple model families, multiple
> accounts, project-aware tools, and efficient specialist agents.

Orichum keeps Claude Code as the interactive shell while routing controllers
and specialists through a validated model stack. A project can select its own
accounts, GitHub identity, Docker MCP profile, memory palace, and code graph.
Sessions remain isolated and resumable.

```bash
orichum
```

## Why Orichum

- Use GPT, Claude, Google, Kimi, and future configured model families.
- Assign models to controller and specialist roles with an interactive wizard.
- Use multiple accounts without changing the machine-wide active login.
- Resolve project tooling automatically from the launch directory.
- Recover once to a frozen, compatible account route without replaying work.
- Keep one controller as the sole writer; delegate bounded read-only work only
  when it is useful.
- Reduce prompt overhead with lossless Headroom optimization.

## Installation

Supported hosts are macOS, Linux with a systemd user manager, and WSL2 with
systemd enabled.

```bash
git clone https://github.com/arvind9981/claudex-workflow.git orichum
cd orichum
./install.sh
```

The installer is also the upgrader. It reconciles Orichum-owned services,
installs current upstream tool releases, validates the control plane, and runs
the final doctor check itself.

Read [Installation and upgrades](docs/installation.md) before installing on a
new machine.

## Usage

Authenticate a provider, register its credential, build a stack, and add a
project:

```bash
orichum provider login claude
orichum provider account add \
  "Personal Claude" anthropic CREDENTIAL_FILE shared --priority primary
orichum stack configure
orichum context add ~/work --pool shared
cd ~/work/my-repository
orichum
```

Inspect what Orichum will use:

```bash
orichum config paths
orichum context list
orichum stack list
orichum sessions
orichum doctor
```

## How a session flows

```mermaid
flowchart LR
    U["You"] --> O["Orichum CLI"]
    O --> C["Claude Code"]
    C --> X["Private Claudex translator"]
    X --> H["Headroom optimization"]
    H --> R["Session-aware route proxy"]
    R --> P["CLIProxyAPI"]
    P --> M["Selected provider account and model"]
```

The launch directory selects the longest matching project context. That context
selects a stack and account pools. Orichum freezes the resulting route into an
immutable logical session, prepares only the relevant MCPs and controller
plugin, then launches Claude Code.

See [Architecture](docs/architecture.md) for service ownership, security
boundaries, and the complete request path.

## Feature guides

| Capability | What it covers |
|---|---|
| [Installation and upgrades](docs/installation.md) | Platforms, prerequisites, locations, ports, services, and upgrade behavior |
| [Providers and accounts](docs/providers-and-accounts.md) | Login, credential registration, account names, pools, priority, and multi-account use |
| [Model stacks](docs/model-stacks.md) | Interactive stack creation, controller and agent roles, provider locks, and validation |
| [Project contexts](docs/project-contexts.md) | Directory matching, GitHub identity, Docker profiles, initial population, and updates |
| [Sessions](docs/sessions.md) | Start, inspect, resume, fork, immutable bindings, and concurrent sessions |
| [Routing and failover](docs/routing-and-failover.md) | Route selection, account rollover, retry limits, cooldowns, and cross-family handoff |
| [Subagents](docs/subagents.md) | Automatic delegation policy, audited roles, limits, and sole-writer behavior |
| [Plugins](docs/plugins.md) | Declare, synchronize, update, inspect, and remove Claude Code plugins |
| [MCP integrations](docs/mcp-integrations.md) | MCP_DOCKER, per-session MCP configuration, relevance, and approval boundaries |
| [Memory and code graph](docs/memory-and-code-graph.md) | Mempalace, Graphify, population, hooks, and token-conscious retrieval |
| [Headroom](docs/headroom.md) | What is compressed, what is disabled, service placement, and savings measurement |
| [Configuration](docs/configuration.md) | Focused JSON files, private state, environment overrides, and validation |
| [Troubleshooting](docs/troubleshooting.md) | Doctor checks, service logs, route errors, identity issues, and recovery |
| [CLI reference](docs/cli-reference.md) | Command map and common inspection commands |

## Architecture principles

- Loopback-only services and private runtime state.
- Exact project and session bindings verified before launch and resume.
- One writer; specialists are bounded and read-only.
- Same-family account recovery only; family changes require an explicit fork.
- No source patches to CLIProxyAPI, Claudex, Headroom, Mempalace, or Graphify.
- Configuration contains declarations and credential references, never secrets.

## References

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [Claudex](https://claudex.space/en/)
- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Headroom](https://github.com/chopratejas/headroom)
- [Mempalace](https://github.com/MemPalace/mempalace)
- [Graphify](https://github.com/Graphify-Labs/graphify)
