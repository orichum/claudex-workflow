# Task 1 report: Repository identity and central graph paths

Status: DONE

## Commit

- `4113b97fdfc229432de77dafa1e65149043191a7 Add repository-aware graph identity`

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_manager -v`
  - Failed as expected with `ModuleNotFoundError: No module named 'integrations.common.graph_manager'` before implementation.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_repository_without_remote_persists_identity_after_movement -v`
  - Failed as expected because a local-only repository exposed its persisted local key as `remote` rather than `None`.

### GREEN

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_repository_without_remote_persists_identity_after_movement -v`
  - Passed after the minimal identity-source correction.
- `python3 -m unittest tests.test_graph_manager -v`
  - Passed: 11 tests.
- `git diff --check && python3 -m py_compile integrations/common/graph_manager.py tests/test_graph_manager.py`
  - Passed.

## Files changed

- `integrations/common/graph_manager.py`
- `tests/test_graph_manager.py`

## Self-review findings

- No blocking findings. Identity keys remove credentials and query/fragment data, unsafe path components are percent encoded, local identity is Git-config persisted, and dirty targets include both checkout-specific and content-sensitive state.

## Concerns

- None.

---

# Review fix round 4

Status: DONE

## Commit

- This round's Task 1 fix commit.

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_prior_worktree_config_ids_are_migrated_for_main_and_linked tests.test_graph_manager.GraphManagerTests.test_checkout_identity_publish_is_synced_before_atomic_replace tests.test_graph_manager.GraphManagerTests.test_failed_checkout_identity_replace_cleans_temporary_state tests.test_graph_manager.GraphManagerTests.test_concurrent_checkout_initialization_returns_one_persisted_id -v`
  - Failed as expected for all three new implementation gaps: existing main-worktree `--worktree` identity was replaced, atomic replacement was not invoked, and simulated replacement failure did not stop the direct final-file write.
  - The rewritten process-concurrency regression passed against the existing advisory-lock implementation; unlike the superseded thread test, it synchronizes both processes at the production-invoked `fcntl.flock` boundary and validates the persisted UUID.

### GREEN

- The four focused regressions above passed after the minimal migration and atomic-publication changes.
- `git diff --check && python3 -m py_compile integrations/common/graph_manager.py tests/test_graph_manager.py && python3 -m unittest tests.test_graph_manager -v`
  - Passed: syntax/whitespace checks clean and 28 tests passed.

## Files changed

- `integrations/common/graph_manager.py`
- `tests/test_graph_manager.py`
- `.superpowers/sdd/2026-07-25-orichum-repository-aware-graph-management/task-1-report.md`

## Self-review findings

- No blocking findings. Upgrade migration preserves both main and linked worktree-scoped UUIDs; checkout identity publication writes and syncs a unique same-directory temporary file, atomically replaces the final file, syncs the directory, and cleans an unpublished temporary file while holding the checkout lock.
- The concurrency regression uses separate processes and a barrier at the production `flock` invocation, then proves both callers return the same valid persisted UUID.

## Concerns

- None.

---

# Review fix round 3

Status: DONE

## Commit

- `468b6919abcbee2fa34b3937744d28736bfeae65 Stabilize graph checkout identity`

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_symlink_before_parent_traversal_is_rejected -v`
  - Failed as expected: `abspath` collapsed the symlink-prefixed `..` component before validation.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_legacy_main_worktree_id_is_migrated_without_linked_collision -v`
  - Failed as expected: the main worktree received a new ID instead of its legacy common-config value; the initial config.worktree approach also copied that value into a later linked worktree.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_concurrent_checkout_initialization_returns_one_persisted_id -v`
  - Failed as expected: synchronized concurrent initialization returned two different IDs.

### GREEN

- Each focused regression passed after its minimal implementation change.
- `git diff --check && python3 -m py_compile integrations/common/graph_manager.py tests/test_graph_manager.py && python3 -m unittest tests.test_graph_manager -v`
  - Passed: syntax/whitespace checks clean and 25 tests passed.

## Files changed

- `integrations/common/graph_manager.py`
- `tests/test_graph_manager.py`

## Self-review findings

- No blocking findings. Textual parent traversal is rejected before normalization; legacy main-worktree IDs migrate into worktree-local Git administrative state that linked worktrees do not copy; a per-worktree advisory lock serializes initialization.

## Concerns

- None.

---

# Review fix round 2

Status: DONE

## Commit

- `4eeaf1b06b47761da69a0e740d10b2b8434362d2 Isolate graph worktree targets`

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_dirty_linked_worktrees_have_distinct_stable_targets -v`
  - Failed as expected: two linked worktrees with the same dirty content used the shared local checkout ID and produced equal state IDs.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_intermediate_repository_symlink_is_rejected tests.test_graph_manager.GraphManagerTests.test_intermediate_data_root_symlink_is_rejected tests.test_graph_manager.GraphManagerTests.test_graphs_output_ancestor_symlink_is_rejected tests.test_graph_manager.GraphManagerTests.test_nested_output_ancestor_symlink_is_rejected -v`
  - Failed as expected: intermediate repository/data-root symlinks and existing `graphs`/identity output ancestors were accepted.

### GREEN

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_dirty_linked_worktrees_have_distinct_stable_targets -v`
  - Passed after storing the checkout UUID with `git config --worktree`; the moved linked worktree retained its target.
- The four focused symlink regressions above passed after component-wise `lstat` validation was added.
- `git diff --check && python3 -m py_compile integrations/common/graph_manager.py tests/test_graph_manager.py && python3 -m unittest tests.test_graph_manager -v`
  - Passed: syntax/whitespace checks clean and 22 tests passed.

## Files changed

- `integrations/common/graph_manager.py`
- `tests/test_graph_manager.py`

## Self-review findings

- No blocking findings. `extensions.worktreeConfig` enables worktree-specific state while linked-worktree move retains its UUID; every existing path component through the selected output directory is now checked with `lstat`, so central storage cannot leave the validated data root through a symlink.

## Concerns

- None.

---

# Review fix round 1

Status: DONE

## Commit

- `bd4c2f54a47edbafa0202f27d1f0b9c5f1b836f4 Harden repository graph targets`

## TDD evidence

### RED

- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_moving_an_unchanged_dirty_checkout_preserves_its_target -v`
  - Failed as expected: moving the same dirty clone changed the path-derived checkout portion of `state_id`.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_ambiguous_origin_fetch_urls_are_rejected -v`
  - Failed as expected: the resolver accepted origin after reading only its first fetch URL.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_repository_and_data_root_symlinks_are_rejected -v`
  - Failed as expected: a symlinked repository was followed.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_repository_and_data_root_require_current_user_ownership -v`
  - Failed as expected: foreign ownership was accepted.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_data_root_requires_private_permissions -v`
  - Failed as expected: mode `0750` was accepted for the graph data root.
- `python3 -m unittest tests.test_graph_manager.GraphManagerTests.test_repository_symlink_is_rejected_by_identity_and_fingerprint -v`
  - Failed as expected: direct identity/fingerprint calls followed a repository symlink.

### GREEN

- Each focused command above passed after its minimal implementation change.
- `git diff --check && python3 -m py_compile integrations/common/graph_manager.py tests/test_graph_manager.py && python3 -m unittest tests.test_graph_manager -v`
  - Passed: syntax/whitespace checks clean and 17 tests passed.

## Files changed

- `integrations/common/graph_manager.py`
- `tests/test_graph_manager.py`

## Self-review findings

- No blocking findings. Dirty state now uses a UUID persisted in repository-local Git config; origin fetch URLs are considered exhaustively; storage boundaries require an existing, owned directory, reject terminal symlinks, and require data-root mode `0700`.

## Concerns

- None.
