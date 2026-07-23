# Claudex workflow

A portable Claude Code daily driver that routes stable controller roles to any
models exposed by CLIProxyAPI.

**Supported:** macOS 13+ · glibc Linux · WSL2 with systemd

## Why use it

- **One entry point:** `claudex-gpt` starts the isolated controller and its project-aware tools.
- **Provider-agnostic roles:** stable agent identities are resolved to the first
  available model in the selected stack before launch.
- **Automatic delegation:** the controller stays inline for ordinary work and selects bounded specialists only when they materially improve the result.
- **Project-aware MCPs:** Docker MCP, MemPalace, and Graphify are exposed only when the current workspace needs them.
- **Repeatable upgrades:** `./install.sh` stages and verifies workflow-owned updates before activation, with rollback on a failed transaction.

## How a request flows

Every `claudex-gpt` process is a client of one persistent Claudex translation proxy
owned by this workflow. The proxy outlives individual terminal sessions
and forwards selected model calls through Headroom and CLIProxyAPI. Headroom
runs with `--lossless` and `--code-aware`; `HEADROOM_OUTPUT_SHAPER=0`,
`HEADROOM_VERBOSITY_AUTOTUNE=0`, and `HEADROOM_EFFORT_ROUTER=0` prevent it from
lowering effort, reshaping output, or truncating workers.

```mermaid
flowchart LR
    A["claudex-gpt"] --> B["Resolve project context"]
    B --> C["Choose project modelStack or global defaultStack"]
    C --> D["Read live CLIProxyAPI model catalogue"]
    D --> E["Resolve controller and ordered agent candidates"]
    E --> F["Create private run directory"]
    F --> G["Write context.json, mcp.json, effective-models.json"]
    G --> H["Generate session-private controller plugin"]
    H --> I["Launch Claudex with selected controller"]
    I --> J["Claude Code dispatches fixed role agents"]
    J --> K["Anthropic wire request"]
    K --> L["Headroom uses generated context limit"]
    L --> M["CLIProxyAPI translates to selected provider"]
```

<details>
<summary>Plain-text flow</summary>

```text
claudex-gpt
  -> resolve project context
  -> choose project modelStack or global defaultStack
  -> read live CLIProxyAPI model catalogue
  -> resolve controller and ordered agent candidates
  -> create private run directory
  -> write context.json, mcp.json, effective-models.json
  -> generate session-private controller plugin
  -> launch Claudex with selected controller
  -> Claude Code dispatches fixed role agents
  -> Anthropic wire request
  -> Headroom uses generated context limit
  -> CLIProxyAPI translates to selected provider
```

</details>

The checked-in `balanced` stack preserves the familiar Sol, Terra, Sonnet, and
Opus defaults. Those names are defaults, not routing logic: CLIProxyAPI routes
each resolved model ID to its configured provider.

## What happens automatically

You describe the task normally; workflows are not manually invoked. Stable role
IDs keep orchestration independent of provider branding, while delegation
remains selective to avoid unnecessary token use.

```mermaid
flowchart TD
    Task["User task"] --> Decide{"Controller evaluates scope and risk"}
    Decide -->|"Small or latency-sensitive"| Inline["Controller stays inline"]
    Decide -->|"Repository reconnaissance"| Explore["repository-explorer"]
    Decide -->|"Independent verification"| Verify["repository-verifier"]
    Decide -->|"Correctness critique"| Critic["correctness-critic"]
    Decide -->|"High-risk adjudication"| Advise["architecture-advisor"]
    Decide -->|"Authorized isolated implementation"| Worker["implementation-worker"]
    Inline --> Integrate["Controller integrates and verifies"]
    Explore --> Integrate
    Verify --> Integrate
    Critic --> Integrate
    Advise --> Integrate
    Worker --> Integrate
```

| Stable role | Portable default | Purpose and authority |
| --- | --- | --- |
| Controller | Sol (`gpt-5.6-sol`) | High-effort controller and normal writer; works inline by default. |
| `repository-explorer` | Terra (`gpt-5.6-terra`) | Bounded read-only repository reconnaissance. |
| `repository-verifier` | Terra (`gpt-5.6-terra`) | Bounded independent verification. |
| `correctness-critic` | Sonnet (`claude-sonnet-5`) | Read-only model-diverse correctness and regression criticism. |
| `architecture-advisor` | Opus (`claude-opus-4-8`) | Reserved for high-risk read-only adjudication of security, authentication, concurrency, migration, irreversible architecture, or conflicting evidence. |
| `implementation-worker` | Sol (`gpt-5.6-sol`) | Isolated implementation only with explicit authorization, a written plan, a clean committed baseline, and an exact disjoint path boundary. |

Heavy investigation and review workflows activate only for independent parallel investigations, repeated analysis across at least eight items, or a high-impact cross-check. They use bounded output schemas, never nest delegation, and report degraded or missing specialists instead of silently retrying.

Bundled skills and Ultracode are disabled. Frontend Design is included locally and loaded only for new UI or material visual redesigns.

## Install and upgrade

Required: Claude Code, `curl`, `jq`, `git`, Python 3.10+, `rg`, `tar`, and
`uv`. Docker with Docker MCP Toolkit is optional unless a workspace uses a
Docker profile.

| Platform | Supported host | Service manager |
| --- | --- | --- |
| macOS | macOS 13 or newer | per-user LaunchAgents |
| Linux | glibc-based distribution | systemd user services |
| WSL | WSL2 with systemd enabled | systemd user services |

Linux and WSL also require `ss` from the `iproute2` package for loopback
listener ownership checks.

```bash
git clone https://github.com/arvind9981/claudex-workflow.git
cd claudex-workflow
./install.sh
claudex-login codex
claudex-login claude
./install.sh
claudex-doctor
```

The first installer run lays down verified binaries and provider-login commands. After both logins, the second run deliberately discovers the available models, publishes one coherent generated configuration, and reconciles the persistent translation proxy. Only the installer may publish that configuration or change workflow-owned services.

The installer resolves current Claudex and CLIProxyAPI releases, verifies their published SHA-256 digests, installs Headroom into the workflow's private data directory, upgrades the user-level MemPalace and Graphify tools through `uv`, synchronizes declared Claude Code plugins, and reconciles workflow-owned services. MemPalace and Graphify must complete real MCP initialization and expose the controller's required tools before installation can succeed. It does not patch upstream package source or installed package files.

It also derives private `headroom/config/models.json` context limits from the
registry in the exact CLIProxyAPI release being installed. Kimi, Google, and
other provider limits are not pinned or hand-maintained here. When the registry
contains the same model ID more than once, the lowest upstream context limit
wins. Unchanged metadata does not restart Headroom. Changed metadata is
preflighted with the staged runtime and is rolled back transactionally if
activation fails.

To update an existing checkout:

```bash
git pull --ff-only
./install.sh
```

The installer is safe to rerun. An upgrade can restart a changed or unhealthy owned proxy and may interrupt active Claudex sessions; specifically, it may interrupt one in-flight request. Finish active sessions first when practical.

Defaults are CLIProxyAPI `8317`, Headroom `8787`, and the shared Claudex translation proxy `13456`. A healthy service is reused only when its service definition, manager PID, loopback listener, and workflow data paths prove that this workflow owns it. If an unrelated listener occupies a requested port, an interactive installation shows the collision and prompts with an available alternative. Without an explicit override, a non-interactive installation logs the collision and automatically selects an available port. An occupied explicit override fails rather than being silently rewritten:

```bash
CLAUDEX_CLIPROXY_PORT=18317 \
CLAUDEX_HEADROOM_PORT=18787 \
CLAUDEX_PROXY_PORT=13457 \
./install.sh
```

All three selected ports are saved privately in `service-ports.json` under `CLAUDEX_DATA_DIR` and used by every launcher, generated configuration, health hook, discovery command, and diagnostic. An unknown listener is never stopped or adopted: without an override the installer selects another port, while an occupied explicit override fails. A foreign service definition is never overwritten and always fails closed.

| Service | macOS LaunchAgent | Linux/WSL systemd user unit |
| --- | --- | --- |
| Claudex translation proxy | `com.user.claudex-translation-proxy` | `claudex-translation-proxy.service` |
| Headroom | `com.user.claudex-headroom` | `claudex-headroom.service` |
| CLIProxyAPI | `com.user.claudex-cliproxy` | `claudex-cliproxy.service` |

### Upgrade transaction

Installer runs are serialized. Staged binaries, generated model mappings, service definitions, and upgraded Python tools are verified before active state is replaced.

```mermaid
flowchart LR
    Run["Run install.sh"] --> Lock["Acquire installer lock"]
    Lock --> Stage["Stage updates"]
    Stage --> Verify{"Verify staged state"}
    Verify -->|"Pass"| Publish["Publish ports and model config"]
    Publish --> Activate["Reconcile persistent services"]
    Activate --> Healthy["Owned PID + loopback + model ready"]
    Verify -->|"Fail"| Restore["Restore prior state"]
    Activate -->|"Failure"| Restore
```

Unchanged healthy services remain running. Before a Headroom cutover, the private runtime must become healthy on an unused loopback port, so cold initialization happens while the existing route still serves traffic. A legacy generic Headroom service is migrated only when its configuration and state paths match this workflow exactly. If activation fails, the installer restores the previous owned service, ports, generated model configuration, and private package state; it exits nonzero if recovery cannot be proven healthy. The Claudex proxy, Headroom, and CLIProxyAPI share this transactional boundary.

`claudex-gpt` never starts, stops, or repairs the shared proxy. Before creating session state it requires the owned loaded definition, the service-manager PID on the exact loopback port, and the configured controller model in `/v1/models`. On failure it exits with `run claudex-doctor`; existing sessions do not own or terminate the proxy lifetime.

After success, the **Installation locations** summary prints the checkout, data directory, launcher links, actual runtime binaries, service files, selected ports, and whether each service was installed, migrated, reconciled, or reused.

Normal `HUP`, `INT`, and `TERM` interruptions release owned locks. An uncatchable `SIGKILL` can leave model publication fail-closed; inspect the exact `model-config/publication.lock` or `model-config/endpoint.lock` under the workflow data directory before removing it manually.

There are no background auto-updaters and no repository-managed version pins. Rerun the installer whenever you want current compatible releases.

## Daily use

```bash
claudex-gpt          # selected controller with bounded role-based specialists
claude-headroom      # native Claude Code through Headroom
claudex-headroom perf                       # workflow Headroom performance, last 7 days
claudex-headroom perf --hours 24 --format json
claudex-doctor       # configuration, dependency, model, and service checks
```

`claudex-headroom` reads only this workflow's private Headroom state and leaves
any global Headroom installation untouched.

Do not set `CLAUDE_CODE_SUBAGENT_MODEL` globally—the selected stack and
session-private controller plugin own specialist selection.

## Provider-agnostic model stacks

All routing policy lives in one version-controlled file:
`controller/model-routing.json`. A stack names one controller model and ordered
candidate arrays for the five fixed roles shown above. Model IDs are opaque:
copy the exact ID reported by the live CLIProxyAPI catalogue; do not infer or
shorten it.

To add Kimi or Google OAuth models:

```bash
claudex-login kimi
claudex-models list          # copy the actual Kimi model ID

claudex-login antigravity
claudex-models list          # copy the actual Google model IDs

$EDITOR controller/model-routing.json
claudex-models validate
./install.sh
```

Add each exact ID to the `controller` field or to one or more ordered candidate
arrays in `controller/model-routing.json`. `claudex-models validate` fails if
the controller is unavailable or if any role exhausts its candidates. Custom
API-key and OpenAI-compatible providers remain configured in CLIProxyAPI; once
their IDs appear in `claudex-models list`, routing treats them the same way.
Credentials and provider tokens never belong in Git.

Select a stack for one registered workspace root:

```bash
claudex-context update ROOT --model-stack NAME
claudex-context update ROOT --inherit-model-stack
```

The first command writes the project `modelStack`. The second clears that
override so the project inherits the global `defaultStack`. New launches and
resumed processes reload project context, routing policy, and the live model
catalogue. An already-running process keeps its immutable mapping. Each launch
writes private `context.json`, `mcp.json`, and `effective-models.json` files and
generates a session-private plugin, so multiple concurrent sessions can safely
use different stacks.

The controller does not fall back: if its configured model is absent, launch
stops before session state is used. Agent candidates fall back in declared order
only during pre-launch resolution; an already-running agent is never switched
mid-request.

## Manage Claudex plugins

Claudex plugins are declared once in `controller/plugins.json` and installed in
the workflow's isolated Claude configuration. The declaration is portable;
marketplace checkouts, plugin packages, configuration, and credentials remain
private under `CLAUDEX_DATA_DIR/claude-config`.

```bash
# The first plugin from a marketplace declares its portable source.
claudex-plugin add github@claude-plugins-official \
  --source anthropics/claude-plugins-official

# Later plugins from the same marketplace do not need --source.
claudex-plugin add another-plugin@claude-plugins-official

claudex-plugin list
claudex-plugin sync       # install missing plugins and upgrade existing ones
claudex-plugin update     # explicit alias for sync
claudex-plugin remove github@claude-plugins-official
```

`add` validates and atomically updates the repository declaration before
synchronizing the isolated installation. `remove` targets only the exact
declared plugin. `sync` registers or updates marketplaces, installs missing
plugins, updates installed plugins to the marketplace's current release, and
enables them. It does not silently uninstall undeclared machine-local plugins.
An empty declaration performs no network work.

Every `./install.sh` run performs the same sync, so a fresh clone converges and
later installer runs upgrade declared plugins without repository version pins.
Plugin code itself is never modified. Review a plugin before declaring it:
always-loaded skills, hooks, agents, and schemas can increase token usage or
change session behavior. All installed plugin agents remain subject to the
orchestration allowlist, and plugin-provided MCP servers remain subject to
strict per-session MCP configuration.

## Manage workspace contexts

Map a top-level parent such as `~/xebia` or `~/complion` once; every repository below it inherits that mapping. The parent itself does not need to be a Git repository.

```bash
claudex-context list
claudex-context add ~/work/acme --docker acme
claudex-context add ~/work/no-docker
claudex-context populate ~/work/acme
claudex-context update ~/work/acme --docker acme-prod --wing production
claudex-context remove ~/work/acme       # prompts for REMOVE
claudex-context remove ~/work/acme --yes
claudex-context validate
```

`add` requires an existing root. `--docker` is optional; when omitted, Docker MCP is not added to that project's strict session configuration. Unless overridden, `add` creates `~/.mempalace/palaces/<root-name>` with `<root-name>` as the wing. It validates first, mines each outermost canonical repository into the configured MemPalace wing, initializes or updates code-only Graphify in every current repository and submodule, installs Graphify Git hooks, and commits the mapping only after success. If no Git repository exists, MemPalace mines the configured root. Context mutations are locked and written atomically; duplicate, overlapping, symlinked, or unsafe paths are rejected before filesystem changes.

`populate` is the explicit, idempotent refresh for repositories cloned later. It uses Graphify `--code-only`; Graphify Git hooks then maintain current code after Git events. MemPalace is not mined on every commit, and population does not run as a service. During MemPalace mining, the workflow excludes `graphify-out/` in memory so generated graphs are not embedded back into project memory. It does not create or edit project ignore files or patch MemPalace. Existing contexts should run `claudex-context populate ~/work/acme` once after upgrading to this feature.

Repository identity comes from Git's common directory, not just its filesystem path. Discovery skips linked worktrees when their primary checkout is already in the context root, preventing the same repository from being mined and graphified twice. A linked worktree remains eligible when it is the only checkout inside the configured root; if several linked checkouts are present without the primary, one deterministic checkout represents the repository. Real submodules remain separate repositories.

Keep linked worktrees beside canonical repositories, as in `parent/.worktrees/repository-branch`, rather than inside a repository that MemPalace must scan. MemPalace has no dynamic CLI exclusion flag, so a skipped worktree nested inside a canonical memory source aborts before MemPalace starts. This fail-closed guard avoids duplicate drawers without editing the project's `.gitignore`, `mempalace.yaml`, or upstream package code.

Repository discovery and elapsed heartbeats are visible by default; there is
no `--verbose` mode to enable. Long operations identify their source, palace,
wing, repository, planned action, and elapsed time, followed by graph and hook
verification:

```text
[discover] found 2 repositories
[discover] skipped linked worktree api-fix — same repository as api
[mempalace 1/2] mining /home/me/work/acme/api into /home/me/.mempalace/palaces/acme; wing acme
[mempalace 1/2] mining — 00:10 elapsed
[graphify 1/2] updating api
[graphify 1/2] graph validated
[graphify 1/2] hooks installed and verified
```

Successful upstream tool output remains captured so project content does not
spill into the terminal. A failed tool still returns its bounded diagnostic
tail after the already completed stages.

```mermaid
flowchart TD
    Add["claudex-context add parent"] --> Validate["Validate context"]
    Validate --> Repositories["Canonical repositories and submodules\nskip duplicate linked worktrees"]
    Repositories --> Memory["Outermost repositories → MemPalace\nshared configured wing"]
    Repositories --> Graphify["Per-repository Graphify init or update\n--code-only"]
    Graphify --> Hooks["Install Graphify Git hooks"]
    Memory --> Commit["Commit context mapping after success"]
    Hooks --> Commit
```

The normal `claudex-gpt` session-routing flow below is unchanged.

The bounded legacy migration is dry-run by default and never deletes its source:

```bash
python3 integrations/mempalace_migration.py
python3 integrations/mempalace_migration.py --execute
```

It is intentionally specific to the approved Xebia and Complion split. It validates source and destination identities, rejects aliases or unsafe targets, and resumes item-by-item after a partial target write without duplicates.

## Project MCP behavior

Start `claudex-gpt` anywhere below a registered workspace root. The longest matching root determines the project context, and the launcher generates a strict MCP configuration for that session only.

```mermaid
flowchart LR
    Cwd["Current directory"] --> Match["Longest workspace-root match"]
    Match --> Session["Strict per-session MCP config"]
    Session --> Docker["Mapped Docker profile (when configured)"]
    Session --> Memory["Bound MemPalace wing"]
    Session --> Graph{"graphify-out/graph.json exists?"}
    Graph -->|"Yes"| Graphify["Graphify MCP"]
    Graph -->|"No"| Skip["No Graphify schema"]
```

- **Docker MCP** runs the mapped `docker mcp gateway` profile when one is configured. Normal create, update, comment, delete, and transition tools remain available; Claude Code permissions still govern writes.
- **MemPalace** is bound to the verified palace and wing for that workspace. The controller consults it when durable decisions or project conventions matter rather than loading memory for every request.
- **Graphify** is exposed only when the current Git repository has `graphify-out/graph.json` and `graphify-mcp` is installed. Its official Git hook is checked automatically before the session.

No matching workspace context means project-specific Docker MCP and MemPalace are not added. Normal Claude configuration is never edited globally.

## State and safety

OAuth credentials, downloaded binaries, generated configuration, model generation, logs, and private session state live outside the checkout under `CLAUDEX_DATA_DIR`, or by default:

```text
${XDG_DATA_HOME:-$HOME/.local/share}/claudex-workflow
```

`claudex-gpt` uses an isolated `CLAUDE_CONFIG_DIR`, so normal Claude settings, plugins, and claude.ai login configuration are not replaced. Declared plugin packages remain in the isolated configuration rather than the checkout. Headroom's executable and Python environment live below `CLAUDEX_DATA_DIR/headroom`; existing global Headroom installations are left untouched. LaunchAgent and systemd service definitions live in their OS configuration directories.

The repository stores no credentials, generated archives, project memories, or Graphify graphs. There are no persistent backups. Destructive, production, authentication, and other high-impact external writes still require explicit authority.

## Diagnose, test, and rollback

Run `claudex-doctor` after installation or when a dependency, generated
configuration, service, project context, strict MCP fixture, or controller
contract needs checking. Install and doctor use only release metadata and a
non-billable sentinel for the Headroom-to-CLIProxyAPI boundary; they never send
a paid provider prompt. Run `claudex-context validate` after manually editing
context configuration.

Headroom applies compression at the generated context limit, but provider token
accounting may remain approximate. This approximate token accounting does not
mean the compression boundary is wrong. A real provider smoke request is always
explicit:

Trigger one only when you want end-to-end validation:

```bash
./smoke-test.sh gpt
./smoke-test.sh claude
./smoke-test.sh controller
```

`./rollback.sh` disables only the workflow-owned `claudex-gpt` launcher link. It retains credentials, services, package data, project files, MemPalace data, and Graphify graphs.

## Upstream projects

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [Claudex](https://github.com/StringKe/claudex)
- [Claudex documentation](https://claudex.space/en/)
- [Headroom](https://github.com/chopratejas/headroom)
- [MemPalace](https://github.com/MemPalace/mempalace)
- [Graphify](https://github.com/Graphify-Labs/graphify)
