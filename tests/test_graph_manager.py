#!/usr/bin/env python3
from __future__ import annotations

import multiprocessing
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from integrations.common.graph_manager import (
    GraphManagerError,
    normalize_remote_url,
    resolve_graph_target,
    resolve_repository_identity,
    working_tree_fingerprint,
)


def _resolve_after_flock_barrier(
    repository: str,
    data_root: str,
    barrier,
    results,
) -> None:
    import integrations.common.graph_manager as graph_manager

    flock = graph_manager.fcntl.flock

    def synchronized_flock(descriptor, operation):
        barrier.wait(timeout=10)
        return flock(descriptor, operation)

    graph_manager.fcntl.flock = synchronized_flock
    try:
        target = graph_manager.resolve_graph_target(Path(repository), Path(data_root))
    except BaseException as error:
        results.put(("error", repr(error)))
    else:
        results.put(("ok", target.state_id))


class GraphManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data_root = self.root / "orichum-data"
        self.data_root.mkdir(mode=0o700)
        self.source = self.root / "source"
        self._init_repository(self.source)
        (self.source / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git(self.source, "add", "tracked.txt")
        self._git(self.source, "commit", "-qm", "Initial commit")
        self.first_clone = self.root / "first-clone"
        self.second_clone = self.root / "second-clone"
        self._clone(self.source, self.first_clone)
        self._clone(self.source, self.second_clone)
        for repository in (self.first_clone, self.second_clone):
            self._git(
                repository,
                "remote",
                "set-url",
                "origin",
                "https://github.com/xebia/X-ACE-UI.git",
            )

    def _git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _init_repository(self, repository: Path) -> None:
        repository.mkdir()
        self._git(repository, "init", "-q")
        self._git(repository, "config", "user.email", "tests@example.invalid")
        self._git(repository, "config", "user.name", "Graph tests")

    def _clone(self, source: Path, destination: Path) -> None:
        subprocess.run(
            ["git", "clone", "-q", str(source), str(destination)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_ssh_and_https_remotes_share_identity(self) -> None:
        self.assertEqual(
            normalize_remote_url("git@github.com:xebia/X-ACE-UI.git"),
            "github.com/xebia/X-ACE-UI",
        )
        self.assertEqual(
            normalize_remote_url("https://github.com/xebia/X-ACE-UI.git"),
            "github.com/xebia/X-ACE-UI",
        )

    def test_credential_bearing_url_does_not_affect_identity(self) -> None:
        self.assertEqual(
            normalize_remote_url(
                "https://username:secret@example.github.com/team/project.git?ref=main#readme"
            ),
            "example.github.com/team/project",
        )

    def test_unsafe_path_segments_are_percent_encoded(self) -> None:
        self.assertEqual(
            normalize_remote_url("https://github.com/team/../project.git"),
            "github.com/team/%2E%2E/project",
        )

    def test_two_clones_at_same_commit_share_revision_target(self) -> None:
        first = resolve_graph_target(self.first_clone, self.data_root)
        second = resolve_graph_target(self.second_clone, self.data_root)
        self.assertEqual(first.identity.key, second.identity.key)
        self.assertEqual(first.output_dir, second.output_dir)

    def test_dirty_checkout_uses_private_working_target(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        target = resolve_graph_target(self.first_clone, self.data_root)
        self.assertEqual(target.kind, "working")
        self.assertIn("/working/", target.output_dir.as_posix())

    def test_moving_an_unchanged_dirty_checkout_preserves_its_target(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        original = resolve_graph_target(self.first_clone, self.data_root)
        moved = self.root / "moved-dirty-clone"
        shutil.move(str(self.first_clone), moved)

        after_movement = resolve_graph_target(moved, self.data_root)

        self.assertEqual(original.state_id, after_movement.state_id)
        self.assertEqual(original.output_dir, after_movement.output_dir)

    def test_dirty_linked_worktrees_have_distinct_stable_targets(self) -> None:
        first_worktree = self.root / "linked-first"
        second_worktree = self.root / "linked-second"
        worktrees = [first_worktree, second_worktree]

        def remove_worktrees() -> None:
            for repository in worktrees:
                if repository.exists():
                    self._git(
                        self.first_clone,
                        "worktree",
                        "remove",
                        "--force",
                        str(repository),
                    )

        self.addCleanup(remove_worktrees)
        self._git(
            self.first_clone, "worktree", "add", "--detach", str(first_worktree)
        )
        self._git(
            self.first_clone, "worktree", "add", "--detach", str(second_worktree)
        )
        for repository in (first_worktree, second_worktree):
            (repository / "dirty.txt").write_text("changed\n", encoding="utf-8")

        first = resolve_graph_target(first_worktree, self.data_root)
        second = resolve_graph_target(second_worktree, self.data_root)
        moved = self.root / "linked-first-moved"
        self._git(
            self.first_clone, "worktree", "move", str(first_worktree), str(moved)
        )
        worktrees[0] = moved
        after_movement = resolve_graph_target(moved, self.data_root)

        self.assertNotEqual(first.state_id, second.state_id)
        self.assertNotEqual(first.output_dir, second.output_dir)
        self.assertEqual(first.state_id, after_movement.state_id)
        self.assertEqual(first.output_dir, after_movement.output_dir)

    def test_legacy_main_worktree_id_is_migrated_without_linked_collision(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        legacy_id = uuid.uuid4()
        self._git(
            self.first_clone,
            "config",
            "--local",
            "orichum.checkoutIdentity",
            str(legacy_id),
        )
        fingerprint = working_tree_fingerprint(self.first_clone)

        main_target = resolve_graph_target(self.first_clone, self.data_root)

        self.assertEqual(main_target.state_id, f"{legacy_id.hex}-{fingerprint}")
        legacy = subprocess.run(
            [
                "git",
                "-C",
                str(self.first_clone),
                "config",
                "--local",
                "--get",
                "orichum.checkoutIdentity",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(legacy.returncode, 0)

        linked = self.root / "legacy-linked"
        self.addCleanup(
            lambda: self._git(
                self.first_clone, "worktree", "remove", "--force", str(linked)
            )
            if linked.exists()
            else None
        )
        self._git(self.first_clone, "worktree", "add", "--detach", str(linked))
        (linked / "dirty.txt").write_text("changed\n", encoding="utf-8")
        linked_target = resolve_graph_target(linked, self.data_root)

        self.assertNotEqual(main_target.state_id, linked_target.state_id)

    def test_prior_worktree_config_ids_are_migrated_for_main_and_linked(self) -> None:
        linked = self.root / "prior-linked"
        self.addCleanup(
            lambda: self._git(
                self.first_clone, "worktree", "remove", "--force", str(linked)
            )
            if linked.exists()
            else None
        )
        self._git(
            self.first_clone,
            "config",
            "--local",
            "extensions.worktreeConfig",
            "true",
        )
        self._git(self.first_clone, "worktree", "add", "--detach", str(linked))
        identities = {
            self.first_clone: uuid.uuid4(),
            linked: uuid.uuid4(),
        }
        for repository, identity in identities.items():
            self._git(
                repository,
                "config",
                "--worktree",
                "orichum.checkoutIdentity",
                str(identity),
            )
            (repository / "dirty.txt").write_text("changed\n", encoding="utf-8")
            fingerprint = working_tree_fingerprint(repository)

            target = resolve_graph_target(repository, self.data_root)

            self.assertEqual(target.state_id, f"{identity.hex}-{fingerprint}")

    def test_checkout_identity_publish_is_synced_before_atomic_replace(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        synced = False
        replaced = False
        fsync = os.fsync
        replace = os.replace

        def record_sync(descriptor):
            nonlocal synced
            synced = True
            return fsync(descriptor)

        def checked_replace(source, destination):
            nonlocal replaced
            self.assertTrue(synced)
            replaced = True
            return replace(source, destination)

        with mock.patch(
            "integrations.common.graph_manager.os.fsync", record_sync
        ), mock.patch(
            "integrations.common.graph_manager.os.replace", checked_replace
        ):
            target = resolve_graph_target(self.first_clone, self.data_root)

        self.assertTrue(replaced)
        persisted = Path(
            self._git(
                self.first_clone, "rev-parse", "--absolute-git-dir"
            ).strip()
        ) / "orichum.checkoutIdentity"
        self.assertEqual(uuid.UUID(persisted.read_text()).hex, target.state_id[:32])

    def test_failed_checkout_identity_replace_cleans_temporary_state(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        git_dir = Path(
            self._git(
                self.first_clone, "rev-parse", "--absolute-git-dir"
            ).strip()
        )

        with mock.patch(
            "integrations.common.graph_manager.os.replace",
            side_effect=OSError("simulated publish failure"),
        ), self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

        self.assertFalse((git_dir / "orichum.checkoutIdentity").exists())
        self.assertEqual(
            list(git_dir.glob(".orichum.checkoutIdentity.*.tmp")),
            [],
        )

    def test_concurrent_checkout_initialization_returns_one_persisted_id(self) -> None:
        (self.first_clone / "dirty.txt").write_text("changed\n", encoding="utf-8")
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_resolve_after_flock_barrier,
                args=(
                    str(self.first_clone),
                    str(self.data_root),
                    barrier,
                    results,
                ),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)

        observed = [results.get(timeout=5) for _ in processes]
        self.assertEqual([status for status, _value in observed], ["ok", "ok"])
        state_ids = [value for _status, value in observed]
        self.assertEqual(state_ids[0], state_ids[1])
        persisted = Path(
            self._git(
                self.first_clone, "rev-parse", "--absolute-git-dir"
            ).strip()
        ) / "orichum.checkoutIdentity"
        persisted_id = uuid.UUID(persisted.read_text()).hex
        self.assertEqual(state_ids[0][:32], persisted_id)
        self.assertEqual(
            state_ids[0],
            resolve_graph_target(self.first_clone, self.data_root).state_id,
        )

    def test_changed_content_changes_dirty_fingerprint(self) -> None:
        tracked = self.first_clone / "tracked.txt"
        tracked.write_text("first change\n", encoding="utf-8")
        first = working_tree_fingerprint(self.first_clone)
        tracked.write_text("second change\n", encoding="utf-8")
        second = working_tree_fingerprint(self.first_clone)
        self.assertNotEqual(first, second)

    def test_untracked_content_changes_dirty_fingerprint(self) -> None:
        untracked = self.first_clone / "new.txt"
        untracked.write_text("first version\n", encoding="utf-8")
        first = working_tree_fingerprint(self.first_clone)
        untracked.write_text("second version\n", encoding="utf-8")
        second = working_tree_fingerprint(self.first_clone)
        self.assertNotEqual(first, second)

    def test_ambiguous_fetch_remotes_are_rejected(self) -> None:
        repository = self.root / "ambiguous"
        self._init_repository(repository)
        (repository / "file.txt").write_text("content\n", encoding="utf-8")
        self._git(repository, "add", "file.txt")
        self._git(repository, "commit", "-qm", "Initial commit")
        self._git(repository, "remote", "add", "one", "https://example.com/a/one.git")
        self._git(repository, "remote", "add", "two", "https://example.com/b/two.git")

        with self.assertRaises(GraphManagerError):
            resolve_repository_identity(repository)

    def test_ambiguous_origin_fetch_urls_are_rejected(self) -> None:
        self._git(
            self.first_clone,
            "remote",
            "set-url",
            "--add",
            "origin",
            "https://example.com/other/project.git",
        )

        with self.assertRaises(GraphManagerError):
            resolve_repository_identity(self.first_clone)

    def test_explicit_repository_identity_overrides_remote(self) -> None:
        self._git(
            self.first_clone,
            "config",
            "orichum.repositoryIdentity",
            "git.example.test/platform/service",
        )

        identity = resolve_repository_identity(self.first_clone)

        self.assertEqual(identity.key, "git.example.test/platform/service")

    def test_repository_and_data_root_symlinks_are_rejected(self) -> None:
        repository_link = self.root / "repository-link"
        repository_link.symlink_to(self.first_clone, target_is_directory=True)
        with self.assertRaises(GraphManagerError):
            resolve_graph_target(repository_link, self.data_root)

        data_link = self.root / "data-link"
        data_link.symlink_to(self.data_root, target_is_directory=True)
        with self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, data_link)

    def test_intermediate_repository_symlink_is_rejected(self) -> None:
        linked_parent = self.root / "linked-repository-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(
                linked_parent / self.first_clone.name, self.data_root
            )

    def test_symlink_before_parent_traversal_is_rejected(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(nested, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(
                linked_parent / ".." / self.first_clone.name, self.data_root
            )

    def test_intermediate_data_root_symlink_is_rejected(self) -> None:
        linked_parent = self.root / "linked-data-parent"
        linked_parent.symlink_to(self.root, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(
                self.first_clone, linked_parent / self.data_root.name
            )

    def test_graphs_output_ancestor_symlink_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.data_root / "graphs").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

    def test_nested_output_ancestor_symlink_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        graphs = self.data_root / "graphs"
        graphs.mkdir()
        (graphs / "github.com").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

    def test_repository_symlink_is_rejected_by_identity_and_fingerprint(self) -> None:
        repository_link = self.root / "repository-link"
        repository_link.symlink_to(self.first_clone, target_is_directory=True)

        with self.assertRaises(GraphManagerError):
            resolve_repository_identity(repository_link)
        with self.assertRaises(GraphManagerError):
            working_tree_fingerprint(repository_link)

    def test_repository_and_data_root_require_current_user_ownership(self) -> None:
        current_uid = os.getuid()
        with mock.patch(
            "integrations.common.graph_manager.os.getuid",
            return_value=current_uid + 1,
        ), self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

        with mock.patch(
            "integrations.common.graph_manager.os.getuid",
            side_effect=[current_uid, current_uid + 1],
        ), self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

    def test_data_root_requires_private_permissions(self) -> None:
        self.data_root.chmod(0o750)

        with self.assertRaises(GraphManagerError):
            resolve_graph_target(self.first_clone, self.data_root)

    def test_repository_without_remote_persists_identity_after_movement(self) -> None:
        repository = self.root / "without-remote"
        self._init_repository(repository)
        (repository / "file.txt").write_text("content\n", encoding="utf-8")
        self._git(repository, "add", "file.txt")
        self._git(repository, "commit", "-qm", "Initial commit")

        original = resolve_repository_identity(repository)
        moved = self.root / "moved-repository"
        shutil.move(str(repository), moved)
        after_movement = resolve_repository_identity(moved)

        self.assertEqual(original.key, after_movement.key)
        self.assertIsNone(original.remote)
        self.assertIsNone(after_movement.remote)

    def test_different_commits_use_different_revision_targets(self) -> None:
        (self.first_clone / "tracked.txt").write_text("next\n", encoding="utf-8")
        self._git(self.first_clone, "add", "tracked.txt")
        self._git(self.first_clone, "commit", "-qm", "Next commit")

        first = resolve_graph_target(self.first_clone, self.data_root)
        second = resolve_graph_target(self.second_clone, self.data_root)

        self.assertEqual(first.kind, "revision")
        self.assertEqual(second.kind, "revision")
        self.assertNotEqual(first.revision, second.revision)
        self.assertNotEqual(first.output_dir, second.output_dir)


if __name__ == "__main__":
    unittest.main()
