# CLI reference

Run `orichum COMMAND --help` for the authoritative options installed on the
current machine.

| Command | Purpose |
|---|---|
| `orichum` / `orichum run` | Start a project-aware session |
| `orichum config show` | Show the merged, redacted control plane |
| `orichum config validate` | Validate focused configuration |
| `orichum config paths` | Print installed configuration and data paths |
| `orichum context list` | Show configured parent-directory contexts |
| `orichum context add ROOT ...` | Populate and add a context |
| `orichum context update ROOT ...` | Change context routing |
| `orichum context populate ROOT` | Explicitly refresh memory and graphs |
| `orichum context remove ROOT` | Remove a context mapping |
| `orichum models list` | List declared models |
| `orichum models stacks` | List configured stacks |
| `orichum models resolve [STACK]` | Resolve effective stack routes |
| `orichum models validate` | Validate model routing |
| `orichum stack available` | Show live provider/model choices |
| `orichum stack configure` | Create or edit a stack interactively |
| `orichum stack list` | List stacks |
| `orichum stack show STACK` | Inspect roles, providers, and account policy |
| `orichum provider login TYPE` | Authenticate a provider through CLIProxyAPI |
| `orichum provider accounts` | List named accounts |
| `orichum provider account ...` | Add, rename, reprioritize, enable, disable, sync, or remove |
| `orichum plugin ...` | List, add, sync, update, or remove optional plugins |
| `orichum headroom status` | Inspect Headroom |
| `orichum doctor` | Validate the complete local installation |
| `orichum sessions` | List logical sessions |
| `orichum session routes ID` | Inspect a session's frozen routes |
| `orichum resume ID` | Resume the same logical session |
| `orichum fork ID --stack STACK --handoff-file FILE` | Create a child session on another stack |
