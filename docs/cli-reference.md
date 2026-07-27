# CLI reference

Run `orichum COMMAND --help` for the authoritative options installed on the
current machine.

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
| `orichum config paths` | Print installed configuration and data paths |
| `orichum context list` | Show configured parent-directory contexts |
| `orichum context add ROOT ...` | Populate and add a context |
| `orichum context update ROOT ...` | Change context routing |
| `orichum context populate ROOT` | Explicitly refresh memory and graphs |
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
| `orichum provider account ...` | Add, rename, reprioritize, enable, disable, sync, or remove |
| `orichum plugin ...` | List, add, sync, update, or remove optional plugins |
| `orichum graph [PATH]` | Build or refresh central Graphify data for repositories below `PATH` (default: `.`) |
| `orichum graph status [PATH]` | Read graph, working-tree, hook, output, and Graphify version status without changing state |
| `orichum graph identity PATH --set ID` | Set an explicit repository identity such as `github.com/xebia/X-ACE-UI` |
| `orichum graph identity PATH --clear` | Clear the explicit identity and return to remote-derived identity |
| `orichum leanctx list` | List LeanCTX-enabled physical runs and mark the current project's newest run |
| `orichum leanctx stats [--run RUN]` | Show an exact savings snapshot |
| `orichum leanctx watch [--run RUN]` | Open LeanCTX's live terminal monitor |
| `orichum leanctx dashboard [--run RUN] [--port PORT] [--open MODE]` | Open the local authenticated LeanCTX Observatory |
| `orichum doctor` | Validate the complete local installation |
| `orichum sessions` | List logical sessions |
| `orichum sessions cleanup [--older-than DAYS] [--yes]` | Preview or remove inactive physical launch snapshots |
| `orichum session routes ID` / `orichum sessions routes ID` | Inspect a session's frozen routes |
| `orichum resume ID` | Resume the same logical session |
| `orichum fork ID --stack STACK --handoff-file FILE` | Create a child session on another stack |

Forward ordinary Claude Code arguments after `--`, for example:

```bash
orichum run -- -p "Summarize this repository"
```

Orichum rejects model, session, workspace, MCP, plugin, effort, tool-approval,
and permission-mode options because those are bound by its validated control
plane.

## Graph examples

```bash
orichum graph .
orichum graph ~/xebia
orichum graph status .
orichum graph identity . --set github.com/xebia/X-ACE-UI
```

`orichum graph status` is read-only. An explicit identity is useful for a
repository without a remote or when clones that should share revision graphs
cannot derive the same unambiguous fetch identity. Graph commands manage
Graphify only; they do not mine or query Mempalace.

## LeanCTX monitoring

```bash
orichum leanctx list
orichum leanctx stats
orichum leanctx watch --run run.mrds3ghq
orichum leanctx dashboard --open browser
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

The monitoring commands use the newest LeanCTX-enabled physical run for the
current project unless `--run RUN` selects an ID from `orichum leanctx list`.
Implicit selection never crosses project boundaries.

`--port PORT` requests a specific loopback port. When omitted, Orichum selects
the first available port starting at `3333`. `--open` accepts `browser`,
`none`, or `vscode` and defaults to `browser`. The dashboard always binds to
`127.0.0.1`, keeps bearer-token authentication enabled, runs in the foreground,
and stops with Ctrl+C.

Physical `run.*` IDs refer to one isolated LeanCTX runtime. Logical `oc-s-*`
IDs refer to resumable Orichum sessions; use those with `resume`, `fork`, and
`session routes`.
