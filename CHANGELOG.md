# Changelog

All notable Orichum changes are recorded here.

## Unreleased

## 0.1.0-rc.1 - 2026-07-27

First release candidate of the unified Orichum harness.

### Added

- Project-aware model stacks spanning GPT, Claude, Google, Kimi, and other
  configured CLIProxyAPI routes.
- Named provider accounts, priorities, account pools, and bounded same-model
  recovery.
- Immutable logical sessions with resume and explicit cross-stack forks.
- Deterministic LeanCTX source, graph, callgraph, and impact routing; Mempalace
  project memory; MCP_DOCKER profiles; and isolated GitHub identities.
- Interactive provider and model-stack configuration.
- macOS ARM64, Linux AMD64, and WSL2-with-systemd acceptance contracts.

### Known release-candidate exclusions

- Real multi-account quota exhaustion and rollover.
- Kimi inference with real credentials.
