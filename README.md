# Orichum

> Run Claude Code with the models, accounts, project tools, memory, and
> specialist agents that fit each project.

Orichum is an independent harness for Claude Code. You continue working inside
Claude Code while Orichum prepares the right model stack, provider account,
project context, and optional tools for the directory you launched from.

Once setup is complete, daily use is simply:

```bash
cd ~/projects/my-app
orichum
```

## What Orichum does

- Runs GPT, Claude, Google, Kimi, and other configured models inside the
  familiar Claude Code interface.
- Lets you choose models and named provider accounts while handling safe
  same-family account recovery automatically.
- Loads the right GitHub identity, project tools, memory, and code graph from
  the directory where you start it.
- Keeps concurrent and resumed sessions isolated, while the controller chooses
  relevant context and specialist agents for each task.

You do not need to understand the routing architecture before using Orichum.
Start with one provider and one project; add the other capabilities only when
you need them.

## Install

Orichum supports macOS, Linux with a systemd user manager, and WSL2 with
systemd enabled.

```bash
git clone https://github.com/arvind9981/claudex-workflow.git orichum
cd orichum
./install.sh
```

The first install performs the complete setup and health check. Later
`./install.sh` runs quickly when everything is already verified and healthy.
Use `./install.sh --upgrade` when you want Orichum to check upstream releases,
upgrade managed tools, run their complete probes, and finish with the full
doctor check. See [Installation and upgrades](docs/installation.md) for
details, locations, port handling, and uninstall options.

To remove the Orichum runtime while keeping accounts, sessions, project
configuration, graphs, and Mempalace palaces for a later reinstall:

```bash
./install.sh --uninstall
```

Use `./install.sh --uninstall --purge` only when you also want to permanently
remove Orichum's saved configuration and data.

## Your first Orichum session

This walkthrough creates the smallest useful setup: one provider, one named
account, one model stack, and one project.

### 1. Connect one provider

Choose the provider you want to use first. This example authenticates a Claude
account through CLIProxyAPI:

```bash
orichum provider login claude
```

Other supported login types include `codex`, `antigravity`, and `kimi`.

### 2. Register the account

The login creates a credential file inside Orichum's private auth directory.
Find the data directory with:

```bash
orichum config paths
```

Look inside its `auth` directory and use the credential **filename**, not its
contents or full path, in the next command:

```bash
orichum provider account add \
  "Personal Claude" anthropic CREDENTIAL_FILE shared --priority primary
```

In this command:

- `Personal Claude` is the name shown in Orichum.
- `anthropic` is the configured provider.
- `CREDENTIAL_FILE` is the filename created by the login.
- `shared` is the account pool available to projects.
- `primary` gives this account first priority.

Confirm that the account is active:

```bash
orichum provider accounts
```

See [Providers and accounts](docs/providers-and-accounts.md) for other
providers and account-management commands.

### 3. Create a model stack

A stack assigns live models to the main controller and optional specialist
roles. Start the interactive wizard:

```bash
orichum stack configure
```

The wizard lists the models currently available through your registered
accounts. Choose a controller model, accept or change the specialist choices,
review the result, and save the stack.

Inspect saved stacks with:

```bash
orichum stack list
orichum stack show STACK
```

See [Model stacks](docs/model-stacks.md) for provider locks, account policies,
and role behavior.

### 4. Add a project

A context tells Orichum which settings belong to a parent directory and every
repository below it. This example adds `~/projects` using the shared account
pool:

```bash
orichum context add ~/projects --pool shared
```

This is a one-time foreground setup. Orichum discovers repositories, prepares
Mempalace and Graphify data, installs its Graphify refresh hooks, and saves the
context only after population succeeds.

Docker MCP Toolkit is optional. Add a profile only when the project needs one:

```bash
orichum context add ~/work --pool shared --docker work
```

Check the configured directory mappings:

```bash
orichum context list
```

See [Project contexts](docs/project-contexts.md) for GitHub identities,
multiple parent directories, repository discovery, and context maintenance.

### 5. Start Orichum

Enter a repository below the configured parent and launch:

```bash
cd ~/projects/my-app
orichum
```

Orichum resolves the project, prepares an isolated session with the selected
model, account, and relevant tools, then opens Claude Code. From this point,
work normally; the controller decides when project context or a configured
specialist is useful.

The Orichum status line keeps the active model, named account, route state,
context usage, and available provider limits visible. See
[Status line](docs/status-line.md).

If launch fails, start with:

```bash
orichum doctor
```

## Daily use

| What you want to do | Command |
|---|---|
| Start in the current project | `orichum` |
| Check project mappings | `orichum context list` |
| List or inspect stacks | `orichum stack list` / `orichum stack show STACK` |
| Check named accounts | `orichum provider accounts` |
| List sessions | `orichum sessions` |
| Inspect a session's routes | `orichum session routes SESSION_ID` |
| Resume a session | `orichum resume SESSION_ID` |
| Monitor context savings | `orichum leanctx stats`, `orichum leanctx watch`, or `orichum leanctx dashboard` |
| Refresh the current code graph | `orichum graph .` |
| Inspect graph state without changing it | `orichum graph status .` |
| Check the installation | `orichum doctor` |
| Upgrade Orichum | Run `./install.sh --upgrade` from the Orichum checkout |

The complete command map is in the [CLI reference](docs/cli-reference.md).

## Add capabilities when you need them

- **More accounts:** register additional credentials, name them, and set
  priorities for new-session selection and same-family recovery. See
  [Multi-account routing](docs/multi-account-usage.md).
- **More model families:** authenticate another provider and use
  `orichum stack configure` to add its live models. See
  [Model stacks](docs/model-stacks.md).
- **Resumes and family changes:** resume a frozen session or fork it with a
  bounded handoff onto another stack. See [Sessions](docs/sessions.md).
- **Memory and code graphs:** Mempalace recalls durable decisions; Graphify
  answers structural repository questions. Both are used on demand. See
  [Memory and code graph](docs/memory-and-code-graph.md).
- **Live source context:** LeanCTX gives the controller compact reads, search,
  trees, lossless expansion, and approved text patches while preserving
  native-tool fallback. See [LeanCTX](docs/leanctx.md).
- **Plugins:** declare and synchronize optional Claude Code plugins through
  Orichum. See [Plugins](docs/plugins.md).
- **MCP_DOCKER:** attach a project-specific Docker MCP Toolkit profile for Jira
  and other live-service tools. See [MCP integrations](docs/mcp-integrations.md).
- **Specialist agents:** let the controller delegate bounded exploration,
  review, architecture, or implementation work while keeping one writer. See
  [Subagents](docs/subagents.md).

## How Orichum fits together

```mermaid
flowchart LR
    P["Project directory"] --> O["Orichum"]
    O --> S["Isolated Claude Code session"]
    S --> M["Selected account and model"]
    O -. "loads only relevant context" .-> T["LeanCTX · Graphify · Mempalace · MCP_DOCKER"]
```

The directory where you run `orichum` selects the project configuration.
Orichum opens a private session using the chosen model and account, then makes
only the relevant project context available. You do not select a tool profile
or manually route each request.

Read [Architecture](docs/architecture.md) for service ownership, security
boundaries, session isolation, and the internal request path.

## If something is wrong

Run the bounded health check first:

```bash
orichum doctor
```

Then inspect the part of the setup involved:

```bash
orichum config paths
orichum provider accounts
orichum stack list
orichum context list
orichum sessions
orichum graph status .
```

The [Troubleshooting guide](docs/troubleshooting.md) covers unavailable routes,
connection failures, GitHub identity, missing MCPs, stale graphs, population
delays, and installer port conflicts.

## Documentation

| Guide | Use it for |
|---|---|
| [Installation and upgrades](docs/installation.md) | Platforms, prerequisites, locations, ports, services, and upgrades |
| [Providers and accounts](docs/providers-and-accounts.md) | Login, credentials, account names, pools, and priorities |
| [Multi-account routing](docs/multi-account-usage.md) | Multiple accounts from the same or different providers |
| [Model stacks](docs/model-stacks.md) | Interactive model selection, roles, and provider locks |
| [Project contexts](docs/project-contexts.md) | Directory mappings, identities, Docker profiles, and population |
| [Sessions](docs/sessions.md) | Start, inspect, resume, fork, and concurrent sessions |
| [Status line](docs/status-line.md) | Active model, account, failover state, context, and quota metrics |
| [Routing and failover](docs/routing-and-failover.md) | Route selection, cooldowns, rollover, and handoff boundaries |
| [Subagents](docs/subagents.md) | Automatic delegation, specialist roles, and the sole-writer policy |
| [Plugins](docs/plugins.md) | Add, update, synchronize, inspect, and remove plugins |
| [MCP integrations](docs/mcp-integrations.md) | MCP_DOCKER and per-session MCP configuration |
| [LeanCTX](docs/leanctx.md) | Compact source context, fallbacks, savings statistics, and live monitoring |
| [Memory and code graph](docs/memory-and-code-graph.md) | Mempalace, Graphify, hooks, worktrees, and retrieval |
| [Configuration](docs/configuration.md) | Focused files, private state, and environment overrides |
| [Architecture](docs/architecture.md) | Components, request flow, ownership, and security boundaries |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, diagnostics, and recovery |
| [CLI reference](docs/cli-reference.md) | The complete command map |
| [Release readiness](docs/release-readiness.md) | End-to-end acceptance evidence, supported boundaries, and known notices |
| [Efficiency and performance](docs/efficiency-and-performance.md) | Measured savings, latency, cost, cache, and resource usage |

## Built with

### Runs on

- [Claude Code](https://code.claude.com/docs/en/overview) — the interactive
  coding host.

### Integrates

- [Claudex](https://claudex.space/en/) — translates Claude Code requests for
  the selected model.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — provides
  provider authentication and model access.
- [LeanCTX](https://github.com/yvgude/lean-ctx) — provides compact live file,
  tree, and search context.
- [Mempalace](https://github.com/MemPalace/mempalace) — provides durable
  project memory.
- [Graphify](https://github.com/Graphify-Labs/graphify) — provides repository
  knowledge graphs.
- [Docker MCP Toolkit](https://docs.docker.com/ai/mcp-catalog-and-toolkit/get-started/)
  — provides project-specific external tools through Orichum's `MCP_DOCKER`
  integration.

Orichum is an independent project. It is not affiliated with or endorsed by
these upstream projects, and it integrates them without modifying their source
code.

## References

- [Claude Code LLM gateway configuration](https://docs.anthropic.com/en/docs/claude-code/llm-gateway)
- [Docker MCP Toolkit documentation](https://docs.docker.com/ai/mcp-catalog-and-toolkit/get-started/)
- [Orichum architecture](docs/architecture.md)
