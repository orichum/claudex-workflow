# Configuration

Orichum exposes several focused files as one validated control plane. Edit the
installed copies shown by:

```bash
orichum config paths
```

| File | Responsibility |
|---|---|
| `model-stacks.json` | Models, families, controller candidates, and specialist role candidates |
| `providers.json` | Provider adapters, auth types, pools, and family route order |
| `projects.json` | Parent paths, stack overrides, pools, GitHub identities, Docker profiles, and memory |
| `plugins.json` | Optional Claude Code marketplaces and plugins |
| `runtime.json` | Controller effort, tool concurrency, and session subagent limit |
| `controller-policy.md` | Sole-writer, delegation, graph, memory, and attribution rules |
| `accounts.json` | Private named-account registry managed by provider commands |
| `stack-bindings.json` | Private machine-local named-account locks |

Portable declarations contain credential references, not secrets.
`accounts.json`, `stack-bindings.json`, authentication data, and session state
are private machine-local files and must not be committed.

Validate after an edit:

```bash
orichum config show
orichum config validate
orichum models validate
orichum models resolve
orichum context validate
```

Use `ORICHUM_CONFIG_HOME`, `ORICHUM_DATA_HOME`, and `ORICHUM_CACHE_HOME` to
relocate the three roots. Each value must be an absolute path. Logical sessions
always remain below the selected data root so the CLI and resident route
service resolve the same state.

Prefer `orichum stack configure`, `orichum provider account`, `orichum context`,
and `orichum plugin` commands over direct JSON editing. They validate and save
changes transactionally.
