# Release readiness

This report records the release-candidate acceptance pass run on 2026-07-27.
It separates live evidence from deterministic and isolated acceptance coverage.

## Verdict

Orichum is release-candidate ready for its supported macOS ARM64, Linux AMD64,
and WSL2-with-systemd targets. The final release gate is the two native GitHub
Actions jobs on the release branch.

Two intentionally excluded cases are not release blockers:

- real quota exhaustion across multiple accounts;
- Kimi inference with real credentials.

Both were excluded at the user's request. Account selection, priority,
validation, and rollover behavior remain covered deterministically.

## Live acceptance

| Capability | Evidence | Result |
|---|---|---|
| Install and upgrade | Ran `./install.sh` against the existing managed installation; services were reused and the automatic doctor completed | Pass |
| Runtime health | Private Python 3.14.6, CLIProxyAPI, route proxy, Claudex, Claude Code, LeanCTX, Mempalace, and Graphify passed `orichum doctor` | Pass |
| OpenAI controller | GPT 5.6 Sol completed new and resumed logical sessions through Orichum | Pass |
| Anthropic agents | Sonnet 5 critic and Opus 4.8 architect completed bounded delegated work | Pass |
| Antigravity transport | Claude Opus 4.6 Thinking and Gemini 3 Flash returned live acceptance markers through the named Antigravity route | Pass |
| Sessions | New, resume, same-family fork, parent linkage, immutable route display, and concurrent physical state were exercised | Pass |
| Subagents | Explorer, verifier, critic, architect, and implementation-worker roles completed bounded tasks | Pass |
| Project routing | Xebia and Complion resolved different Docker profiles, GitHub identities, account pools, palaces, and wings | Pass |
| GitHub identity | Isolated `GH_CONFIG_DIR` instances resolved `athevar-xebia` and `arvind9981` without changing the user's active account | Pass |
| MCP_DOCKER | Both `xebia` and `realtime` profiles completed MCP initialize and tools/list handshakes | Pass |
| LeanCTX | Exact six-tool jailed MCP exposed read, search, tree, expand, patch, and shell only; compact read, shell, and dry-run patch calls succeeded | Pass |
| Mempalace | Population, store verification, immutable context binding, wing injection, and a live wing-scoped read succeeded | Pass |
| Graphify | Central graph creation, immutable session snapshot, MCP query, Git hook refresh after commit, and repository-aware identity succeeded | Pass |
| Status line | Displayed Orichum, project, stack, active GPT account, route state, context, and quota values | Pass |
| Service lifecycle | Shared resident services remained healthy; no per-session Claudex translators remained after sessions exited | Pass |

The live provider tests were bounded and did not write to external Jira,
Atlassian, GitHub, or other project services.

## Deterministic and isolated acceptance

| Boundary | Coverage |
|---|---|
| Python behavior | 593 `unittest` cases, including routing, accounts, sessions, hooks, tool deferral, graph safety, and status rendering |
| Shell behavior | All seven suites: smoke, plugin, installer, transaction, route, launcher, and uninstall |
| Installer safety | Fresh install, idempotent upgrade, occupied-port selection, owned-service reuse, foreign-service preservation, and rollback |
| Uninstall | Default and purge behavior in isolated homes; external tools and unrelated services are preserved |
| Linux AMD64 | Native GitHub Actions acceptance plus a privileged Ubuntu systemd-user container |
| WSL2 contract | The same systemd-user service path plus WSL1 rejection and WSL2 detection fixtures |
| macOS ARM64 | Native macOS 15 acceptance with launchd service lifecycle |
| Security | Private ownership/modes, no-follow reads, immutable session digests, project jails, strict MCP config, and exact tool allowlists |

## Non-blocking notices

`orichum doctor` currently reports:

- Graphify package/skill drift (`0.9.27` package, `0.9.23` separately
  installed global skill);
- legacy repository-local Graphify outputs;
- repositories whose older hooks can be reconciled.

These do not affect Orichum's bound Graphify MCP or central graphs. Orichum
does not silently rewrite unrelated global skills or bulk-change existing
repositories. Reconcile a repository when you next work in it with:

```bash
orichum graph .
```
