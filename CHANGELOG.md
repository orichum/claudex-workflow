# Changelog

All notable Orichum changes are recorded here.

## Unreleased

## 0.1.0-rc.2 - 2026-07-28

### Added

- Orichum now installs an allowlisted, content-addressed runtime under
  `~/.orichum/runtime/releases` and activates it through an atomic current
  pointer.
- Existing XDG-based Orichum state is migrated transactionally into the
  consolidated home and restored if installation fails.

### Changed

- Configuration, credentials, sessions, logs, caches, and LeanCTX knowledge
  now live under one configurable `ORICHUM_HOME`, which defaults to
  `~/.orichum`.
- Launchers and owned services bind to a verified physical runtime release;
  the Git checkout is used only as an installation and upgrade source.
- Architecture, installation, configuration, troubleshooting, and CLI
  documentation now describe the consolidated runtime and state layout.
- LeanCTX now owns live code context, repository graphs, task orientation, and
  durable project knowledge through one repo-aware store.
- Every built-in specialist reuses the session's jailed LeanCTX MCP under an
  exact role-specific tool contract.
- LeanCTX monitoring reports only the selected physical run and distinguishes
  MCP registration from real tool activity.
- Project contexts no longer require memory population, palace paths, or wing
  names.
- Deterministic shell routing uses `ctx_shell` for compressed observation and
  native `Bash` for mutations, authentication, and interactive processes.
- User documentation now reflects the consolidated LeanCTX architecture and
  complete Orichum command surface.

### Fixed

- Unknown bare launcher commands now fail closed instead of being forwarded to
  Claude Code as prompts.
- Route-proxy services now retain the selected Orichum data root, including
  relocated macOS, Linux, and CI installations.
- Native acceptance validates the private managed Python and the current
  route-proxy runtime rather than stale system or wrapper paths.
- Fast repeat acceptance now requires routing reuse instead of unnecessary
  repair.
- Provider-free installs no longer emit a routing-fingerprint traceback or
  report intentionally inactive route telemetry as a second failure while
  waiting for the first account login.
- Native acceptance isolates the consolidated home and validates only models
  provided by its disposable OpenAI and Anthropic accounts.
- Nested context and plugin help is delegated to the helper that owns the
  command, so it displays the real options instead of generic passthrough help.

### Removed

- The Mempalace runtime, MCP server, hooks, installer dependency, and project
  configuration fields.

## 0.1.0-rc.1 - 2026-07-27

First release candidate of the unified Orichum harness.

### Added

- Project-aware model stacks spanning GPT, Claude, Google, Kimi, and other
  configured CLIProxyAPI routes.
- Named provider accounts, priorities, account pools, and bounded same-model
  recovery.
- Immutable logical sessions with resume and explicit cross-stack forks.
- Deterministic LeanCTX source, graph, callgraph, and impact routing;
  MCP_DOCKER profiles; and isolated GitHub identities.
- Interactive provider and model-stack configuration.
- macOS ARM64, Linux AMD64, and WSL2-with-systemd acceptance contracts.

### Known release-candidate exclusions

- Real multi-account quota exhaustion and rollover.
- Kimi inference with real credentials.
