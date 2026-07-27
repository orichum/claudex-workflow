# Release readiness

This report records the Orichum `0.1.0-rc.1` acceptance pass run on 2026-07-27.
It separates live evidence from deterministic and isolated acceptance coverage.

## Verdict

Orichum is release-candidate ready for macOS ARM64 and Linux AMD64. WSL2 with
systemd shares the Linux service implementation and has deterministic contract
coverage; it has not been presented here as a native WSL acceptance run.

Every pull request and `main` push runs one fast Linux contract check. The
costlier native macOS ARM64 and Linux AMD64 acceptance workflows remain manual
release gates.

Two intentionally excluded cases are not release blockers:

- real quota exhaustion across multiple accounts;
- Kimi inference with real credentials.

Both were excluded at the user's request. Account selection, priority,
validation, and rollover behavior remain covered deterministically.

Orichum is licensed under Apache-2.0. Its root `LICENSE` and `NOTICE` files
declare the project terms, while `THIRD_PARTY_NOTICES.md` records the
independent licenses retained by integrated upstream tools.

## Live acceptance

| Capability | Evidence | Result |
|---|---|---|
| Install and upgrade | Ran `./install.sh` against the existing managed installation; services were reused and the automatic doctor completed | Pass |
| Runtime health | Private Python 3.14.x, CLIProxyAPI, route proxy, Claudex, Claude Code, and LeanCTX passed local component readiness checks | Pass |
| OpenAI controller | GPT 5.6 Sol completed new and resumed logical sessions through Orichum | Pass |
| Anthropic agents | Sonnet 5 critic and Opus 4.8 architect completed bounded delegated work | Pass |
| Antigravity transport | Claude Opus 4.6 Thinking and Gemini 3 Flash returned live acceptance markers through the named Antigravity route | Pass |
| Sessions | New, resume, same-family fork, parent linkage, immutable route display, and concurrent physical state were exercised | Pass |
| Subagents | Explorer, verifier, critic, architect, and implementation-worker roles completed bounded tasks | Pass |
| Project routing | Xebia and Complion resolved different Docker profiles, GitHub identities, and account pools | Pass |
| GitHub identity | Isolated `GH_CONFIG_DIR` instances resolved `athevar-xebia` and `arvind9981` without changing the user's active account | Pass |
| MCP_DOCKER | Both `xebia` and `realtime` profiles completed MCP initialize and tools/list handshakes | Pass |
| LeanCTX | Exact eleven-tool jailed MCP exposed only the bounded source, graph, overview, and knowledge surface | Pass |
| LeanCTX specialists | Explorer, verifier, critic, architect, and implementation worker each completed a live bounded read through the shared session MCP | Pass |
| LeanCTX memory route | The controller dynamically loaded deferred overview and knowledge tools, completed task orientation, and performed read-only project recall | Pass |
| Status line | Displayed Orichum, project, stack, active GPT account, route state, context, and quota values | Pass |
| Service lifecycle | Shared resident services remained healthy; no per-session Claudex translators remained after sessions exited | Pass |

The live provider tests were bounded and did not write to external Jira,
Atlassian, GitHub, or other project services.

The post-migration LeanCTX acceptance on 2026-07-28 measured 94.0% reduction
for a bounded explorer read and 99.1% across the remaining specialist roles.
The controller also completed one overview and one read-only knowledge call;
those tools do not emit source-compression counters. These are LeanCTX
tool-payload measurements, not whole-session provider-token savings.

## Deterministic and isolated acceptance

| Boundary | Coverage |
|---|---|
| Python behavior | Complete local `unittest` discovery, including routing, accounts, sessions, hooks, tool deferral, LeanCTX isolation, and status rendering |
| Shell behavior | All seven suites: smoke, plugin, installer, transaction, route, launcher, and uninstall |
| Installer safety | Fresh install, idempotent upgrade, occupied-port selection, owned-service reuse, foreign-service preservation, and rollback |
| Uninstall | Default and purge behavior in isolated homes; external tools and unrelated services are preserved |
| Linux AMD64 | Native GitHub Actions acceptance plus a privileged Ubuntu systemd-user container |
| WSL2 contract | The Linux systemd-user path plus WSL1 rejection and WSL2 detection fixtures; native WSL execution is a separate release-environment check |
| macOS ARM64 | Native macOS 15 acceptance with launchd service lifecycle |
| Security | Private ownership/modes, no-follow reads, immutable session digests, project jails, strict MCP config, and exact tool allowlists |
