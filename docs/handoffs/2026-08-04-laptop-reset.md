# Orichum workstation-reset handoff

Last updated: 2026-08-04

This is the durable continuation point for a new Codex or Claude session after
the current laptop is reset. It contains no credentials, OAuth callbacks,
tokens, or private provider data.

## Immediate state

- Repository: <https://github.com/orichum/orichum>
- Default branch: `main`
- Current merged `main`: `01223c0154c2192ac00ae0c876071e83d3a759cf`
- Latest published Orichum release: `v0.1.0-rc.9`
- PR #92 is merged after `v0.1.0-rc.9` and is therefore not included in that
  published release.
- Current local installation reports `Orichum 0.1.0-rc.9`.
- The next operational task is to publish the next release candidate containing
  PR #92, install it, and perform live acceptance.

## Completed milestones

### Installer and setup UX

The installer and setup flow were simplified so a normal user can:

1. run `./install.sh`;
2. run `orichum setup`;
3. choose a provider and authenticate through a displayed URL that works over
   SSH;
4. enter an account name;
5. select a projects folder;
6. receive a recommended model stack without typing model IDs.

Setup output was reduced to useful progress and actionable failures. Credential
modes, service ownership, missing model stacks, route/controller readiness, and
CLI-compatible OAuth handling received focused fixes.

### Claudex fork and resume output

- Orichum installs the latest release from `alupao/claudex` rather than the
  upstream repository while the upstream PR is pending.
- The latest fork release is `v0.2.5`.
- The local installed component records
  `github:alupao/claudex@v0.2.5`.
- The fork and installer changes were released in Orichum `v0.1.0-rc.9`.
- Resume output is designed to present the concise Orichum-owned command instead
  of raw Claude and Claudex launch commands.

Historical design and implementation records:

- `docs/superpowers/specs/2026-08-04-claudex-fork-source-design.md`
- `docs/superpowers/plans/2026-08-04-claudex-fork-source-plan.md`

### LeanCtx efficiency and economics

Merged PR: <https://github.com/orichum/orichum/pull/92>

The merged change:

- sets `LEAN_CTX_RULES_INJECTION=off` on every managed LeanCtx path that can
  supply or validate runtime context;
- retains the `lean` profile with four resident tools and seven provider-
  deferred tools;
- retains `max_index_threads = 2`, `max_ram_percent = 12`, semantic model
  auto-download, shared persistent indexes, and isolated per-session state;
- accepts only exact official negative bounce corrections in rolling economics;
- preserves safe upgrades from the previous launchd/systemd service definition;
- keeps post-install ownership checks strict and rejects unrelated drift.

Live evidence collected before the reset:

- active-session MCP reduction: 95.6%;
- 1,000,499 source tokens reduced to 43,741 returned tokens;
- live ledger: 1,576 rows, including six negative bounce corrections;
- seven-day reader result: 278 compression events and 1,247 caching records;
- estimated seven-day values at measurement time: $11.220410 compression saving
  and $669.702737 cache discount.

Do not interpret `tools health.rules_tokens` as proof that rules were injected.
LeanCtx reports rule inventory there even when injection is disabled. The local
LeanCtx 3.9.12 configuration schema defines `off` as writing no rules file for
hosts that supply their own steering.

Historical design and implementation records:

- `docs/superpowers/specs/2026-08-04-leanctx-efficiency-design.md`
- `docs/superpowers/plans/2026-08-04-leanctx-efficiency.md`

## Current installed component versions

Recorded in `~/.orichum/state/install-state.json` on the old laptop:

| Component | Version/source |
|---|---|
| Orichum | `0.1.0-rc.9` |
| Claudex | `0.2.5`, `github:alupao/claudex@v0.2.5` |
| CLIProxyAPI | `7.2.117`, `github:router-for-me/CLIProxyAPI@v7.2.117` |
| LeanCtx | `3.9.12`, `github:yvgude/lean-ctx@v3.9.12` |
| Python | `3.14.6` |

## New-laptop bootstrap

### Git identity

The current global identity is:

```text
user.name = athevar
user.email = arvind.thevar@dynamisch.co
```

Repositories under `github.com/orichum/**` and `github.com/alupao/**` use:

```text
user.name = alupao
user.email = arvind9981@gmail.com
```

Recreate the conditional identity file and includes before committing. The old
laptop used `~/.gitconfig-alupao` plus HTTPS, SCP-style SSH, and `ssh://`
`includeIf.hasconfig:remote.*.url` entries for both organizations.

Authenticate the GitHub CLI as `alupao`:

```bash
gh auth login
gh auth status
```

### Clone and install

```bash
git clone https://github.com/orichum/orichum.git
cd orichum
git fetch --all --tags
git switch main
git pull --ff-only origin main
./install.sh
orichum setup
orichum doctor
orichum --version
```

Installing from `main` includes PR #92 even before the next release is cut. A
fresh release install of `v0.1.0-rc.9` does not include it.

### Secrets and local state

Do not commit the following directories. If old sessions, credentials, or
indexes must survive, copy them separately using an encrypted backup:

- `~/.orichum/auth` — provider credentials;
- `~/.orichum/state` — Orichum sessions and runtime state;
- `~/.orichum/leanctx` — shared LeanCtx indexes, ledger, and semantic assets;
- `~/.claude` — Claude Code configuration and session data;
- `~/.codex` — Codex configuration, skills, plugins, and local task data.

Provider authentication can instead be recreated with `orichum setup`.

## Next release and live acceptance

1. Confirm `main` contains merge commit `01223c0` or later.
2. Prepare the next release candidate after `v0.1.0-rc.9`; do not retag or
   overwrite RC9.
3. Install the published release on macOS and Linux.
4. Run:

   ```bash
   orichum doctor
   orichum --version
   orichum leanctx economics --hours 168
   orichum leanctx stats
   ```

5. Start and resume a real session. Verify the generated MCP environment and
   the persistent LeanCtx proxy service contain
   `LEAN_CTX_RULES_INJECTION=off`.
6. Confirm rolling economics accepts the existing ledger instead of reporting
   `LeanCTX savings ledger is invalid`.
7. Confirm existing service definitions upgrade without being classified as
   foreign, then rerun the installer once to prove idempotency.
8. Compare a new session's fixed prompt footprint with the pre-change baseline.
   Report observed savings separately from inferred savings.

## Working conventions to retain

- Keep changes surgical and within the requested scope.
- Do not modify unrelated README, workflow, security, or infrastructure files
  as preparation for another task.
- Present declared state, observed state, and inference separately.
- Never force-push.
- Run focused tests first, then the nearest integration boundary.
- Verify live after installation before claiming deployment success.
- When the user says `LGTM`, proceed with the agreed work without repeated
  routine approval prompts.
