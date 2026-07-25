# Task 4 report: Worktree-aware Orichum Git hooks

Status: DONE

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_hooks -v`
  - Failed with `ModuleNotFoundError` because
    `integrations.common.graph_hooks` did not exist.
- Focused lifecycle tests then failed because successful graph activation did
  not install Orichum hooks and `graph hook-update PATH` was not implemented.
- Smoke failed while doctor still depended on
  `ensure-graphify-hook.py` and that session-start mutation script still
  existed.
- Self-review regressions failed before their fixes for marker-like text in
  unrelated user commands and cleanup attempted against an unmanaged
  repository.

### GREEN

- `python3 -m unittest tests.test_graph_hooks tests.test_graph_manager`
  - Passed: 75 tests in 82.929 seconds.
- `bash tests/test_smoke.sh`
  - Passed: Orichum command and control-plane smoke.
- `git diff --check`, Python bytecode compilation for the changed Python
  surfaces, and `bash -n doctor.sh tests/test_smoke.sh`
  - Passed.

## Implementation

- Added exact marked-block management for shared `post-commit` and
  `post-checkout` hook files. Existing user content is retained and installs
  are idempotent.
- Hook commands quote the absolute Orichum launcher and pass `"$PWD"`, so a
  shared hook resolves the worktree that actually triggered it.
- Hook-directory and file ownership, type, symlink, size, and writable-mode
  checks fail closed. Git path probes are bounded.
- The hidden hook command validates the runtime worktree, launches graph sync
  through a detached child, and returns without waiting for Graphify. Output is
  appended to a private repository-hashed log; logs over 1 MiB rotate to one
  `.previous` file.
- Removed only complete upstream Graphify marker blocks. The Graphify merge
  driver and its `.gitattributes` entry are removed only when the repository is
  already Orichum-managed and the known name, driver, and attribute all match.
- Successful created, updated, and migrated graph activations install the
  Orichum hook contract. Graph status now reports that contract instead of
  probing Graphify's upstream hook installer.
- Deleted the session-start hook mutation script and its obsolete tests.
  Doctor now imports and validates the repository graph manager and hook
  interfaces.

## Source verification

- Inspected the locally installed Graphify 0.9.25 hook implementation to match
  its exact post-commit and post-checkout markers, merge-driver name and
  command, and default `.gitattributes` registration.

## Self-review

- Shell quoting was exercised with an absolute launcher containing spaces.
- Linked worktrees were verified to share one idempotent default hook
  installation while each invocation passes its runtime checkout.
- Detached execution was verified with a deliberately slow child and bounded
  log rotation.
- Unrelated marker-like text, malformed sections, symlink hooks,
  world-writable hook directories, mismatched merge registrations, and
  unmanaged repositories all preserve user-owned state or fail closed.

## Concerns

- None.
