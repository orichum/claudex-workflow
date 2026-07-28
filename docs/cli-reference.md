# CLI reference

Run `orichum --help` for the command map and
`orichum COMMAND [SUBCOMMAND] --help` for the authoritative options installed
on the current machine.

Installer modes are separate from the `orichum` command:

```bash
./install.sh
# Install completely when fresh; otherwise reconcile verified local state.

./install.sh --upgrade
# Check upstream releases, upgrade managed tools, and run the full doctor.

./install.sh --uninstall
# Remove managed runtimes while preserving accounts, sessions, and project data.

./install.sh --uninstall --purge
# Also permanently remove Orichum's saved configuration and private data.
```

| Command | Purpose |
|---|---|
| `orichum --version` | Print the installed Orichum release identity |
| `orichum` / `orichum run` | Start a project-aware session |
| `orichum config show` | Show the merged, redacted control plane |
| `orichum config validate` | Validate focused configuration |
| `orichum config paths` | Print the consolidated home, configuration, data, cache, and state paths |
| `orichum context list` | Show configured parent-directory contexts |
| `orichum context add ROOT [--model-stack STACK] [--pool POOL] [--github-account ACCOUNT]` | Add a parent-directory context; repeat `--pool` for ordered pools |
| `orichum context jira ROOT [--url URL] [--username USER]` | Prompt for and save Jira credentials directly on a project context |
| `orichum context jira ROOT --remove` | Remove Jira from a project context |
| `orichum context update ROOT ...` | Replace pools or GitHub identity; set or inherit a stack |
| `orichum context remove ROOT` | Remove a context mapping |
| `orichum context validate` | Validate all configured project contexts |
| `orichum models list` | List declared models |
| `orichum models stacks` | List configured stacks |
| `orichum models resolve [STACK]` | Resolve effective stack routes |
| `orichum models validate` | Validate model routing |
| `orichum stack available` | Show live provider/model choices |
| `orichum stack configure` | Create or edit a stack interactively |
| `orichum stack list` | List stacks |
| `orichum stack show STACK` | Inspect roles, providers, and account policy |
| `orichum provider configure` | Log in and register one provider account interactively |
| `orichum provider login TYPE` | Authenticate a provider through CLIProxyAPI |
| `orichum provider list` | List configured provider adapters and model families |
| `orichum provider accounts` | List named accounts |
| `orichum provider account add NAME PROVIDER CREDENTIAL_FILE POOL [--priority VALUE]` | Register a credential without using the wizard |
| `orichum provider account rename ACCOUNT NAME` | Change an account's display name |
| `orichum provider account priority ACCOUNT VALUE` | Set an alias or numeric priority |
| `orichum provider account enable ACCOUNT` / `orichum provider account disable ACCOUNT` | Change account availability |
| `orichum provider account remove ACCOUNT` | Remove an account's registry entry |
| `orichum provider account sync [ACCOUNT]` | Reconcile one or all registered credentials |
| `orichum plugin list` | List declared optional plugins |
| `orichum plugin add PLUGIN@MARKETPLACE [--source SOURCE]` | Declare and install a plugin |
| `orichum plugin sync` / `orichum plugin update` | Reconcile or refresh declared plugins |
| `orichum plugin remove PLUGIN@MARKETPLACE` | Uninstall and remove a declaration |
| `orichum leanctx list [--limit N \| --all]` | List attached LeanCTX runs; include incompatible historical runs with `--all` |
| `orichum leanctx stats [--run RUN]` | Show session MCP and shared wire-proxy savings |
| `orichum leanctx watch [--run RUN]` | Open LeanCTX's live terminal monitor |
| `orichum leanctx dashboard [--run RUN] [--port PORT] [--open MODE]` | Open the local authenticated LeanCTX Observatory |
| `orichum doctor` | Validate local component ownership, configuration, protocols, and service health |
| `orichum status [ID]` | Show the selected session's current model, named account, route state, and quota windows |
| `orichum sessions [--limit N \| --all]` | List recent logical sessions |
| `orichum sessions cleanup [--older-than DAYS] [--yes]` | Preview or remove inactive physical launch snapshots |
| `orichum sessions remove ID [--yes]` | Preview or remove one inactive leaf logical session |
| `orichum sessions clear [--yes]` | Preview or remove all inactive logical sessions |
| `orichum session routes ID` / `orichum sessions routes ID` | Inspect a session's frozen routes |
| `orichum resume ID` | Resume by Orichum logical ID or Claude session UUID |
| `orichum fork ID --stack STACK --handoff-file FILE` | Create a child session on another stack |

Forward ordinary Claude Code arguments after `--`, for example:

```bash
orichum run -- -p "Summarize this repository"
```

Orichum rejects model, session, workspace, MCP, plugin, effort, tool-approval,
and permission-mode options because those are bound by its validated control
plane.

## LeanCTX monitoring

```bash
orichum leanctx list
orichum leanctx list --all
orichum leanctx stats
orichum leanctx watch --run run.mrds3ghq
orichum leanctx dashboard --open browser
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

Inside a live session, monitoring uses that physical run. Otherwise it uses the
newest physical run for the current project, regardless of whether it has
recorded activity. `--run RUN` selects an ID explicitly. Implicit selection
never crosses project boundaries and never substitutes an older active run.
`list` shows up to 20 attached runs by default. Use `--limit N` to change that
bound or `--all` to include every attached and historical incompatible run.

`stats` has two sections. **Session MCP** compares source tokens processed by
LeanCTX with tokens returned to the model for the selected physical run.
**Shared wire proxy** reports cumulative request compression across all Orichum
sessions since the shared proxy started. These are optimizer counters, not
provider billing, prompt-cache, reasoning, or output-token totals. A dash means
that path has not observed measurable input yet.

`--port PORT` requests a specific loopback port. When omitted, Orichum selects
the first available port starting at `3333`. `--open` accepts `browser`,
`none`, or `vscode` and defaults to `browser`. The dashboard always binds to
`127.0.0.1`, keeps bearer-token authentication enabled, runs in the foreground,
and stops with Ctrl+C.

Physical `run.*` IDs refer to one isolated LeanCTX runtime. Logical `oc-s-*`
IDs refer to resumable Orichum sessions; use those with `resume`, `fork`, and
`status` or `session routes`. Inside a live session, `orichum status` uses
`ORICHUM_SESSION_ID`; from another shell, pass the logical ID explicitly.
