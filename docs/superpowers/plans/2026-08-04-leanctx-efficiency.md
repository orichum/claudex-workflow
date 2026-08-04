# LeanCtx Integration Efficiency Implementation Plan

> Historical implementation record. Completed and merged in Orichum PR #92 on
> 2026-08-04. Preserved at the user's request before a workstation reset.

**Goal:** Remove duplicate LeanCtx rule injection and make Orichum rolling
economics compatible with official bounce correction records.

**Architecture:** Keep the existing static tool-residency profiles and request
transformer unchanged. Add the official rules-injection opt-out to every
Orichum-owned LeanCtx environment, and extend the strict ledger reader with one
narrowly validated negative bounce-record path.

**Tech Stack:** Python 3.14-compatible standard library, Bash, `unittest`,
launchd, and systemd service rendering.

## Constraints

- Sol remained the only writer; reviewers were read-only.
- Logical-session profile semantics and the eleven-tool MCP contract were not
  changed.
- The upstream savings ledger remains read-only.
- No-follow, ownership, mode, size, and concurrent-change validation remain
  fail-closed.

## Completed work

### Bounce-aware economics

- Added valid in-window and out-of-window bounce coverage.
- Added rejection coverage for negative ordinary compression and caching rows.
- Added rejection coverage for malformed bounce records.
- Accepts a negative `saved_usd` only when:
  - `mechanism == "compression"`;
  - `tool == "bounce"`;
  - `actual_tokens == baseline_tokens`;
  - `saved_tokens == 0`;
  - `0 < bounce_adjustment <= baseline_tokens`;
  - `saved_usd` is finite and negative.

### Duplicate rule injection

- Added `LEAN_CTX_RULES_INJECTION=off` to:
  - private session MCP environments;
  - monitor, watch, and dashboard environments;
  - the shared LeanCtx proxy environment;
  - embedding provision and status probes;
  - installer capability and proxy probes;
  - launchd and systemd service definitions.
- Ownership verification requires the new setting after activation.
- Installer preflight accepts only the exact immediately previous service shape
  so existing installations can migrate; unrelated drift remains rejected.

### Documentation and validation

- Updated `docs/leanctx.md` and `docs/efficiency-and-performance.md`.
- Ran 222 focused Python tests successfully.
- Ran `bash tests/test_installer.sh` successfully.
- Ran `git diff --check` successfully.
- Validated the reader against the live ledger: 1,576 rows, including six
  official negative bounce rows.
- An independent GPT review found the upgrade-compatibility issue above; the
  repair was re-reviewed cleanly. The intended Fable review was unavailable due
  to a provider authentication failure and was not retried unchanged.

## Publication

- Commit: `3f888d5 Improve LeanCtx integration efficiency`
- PR: <https://github.com/orichum/orichum/pull/92>
- Merge commit: `01223c0154c2192ac00ae0c876071e83d3a759cf`
