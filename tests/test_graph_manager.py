#!/usr/bin/env python3
from __future__ import annotations

import multiprocessing
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from integrations.common.graph_manager import (
    GraphError,
    GraphManagerError,
    discover_graph_targets,
    inspect_graph,
    migrate_legacy_graph,
    normalize_remote_url,
    prune_orphaned_working_graphs,
    resolve_graph_target,
    resolve_repository_identity,
    sync_graph,
    sync_graphs,
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


def _sync_in_process(repository: str, data_root: str, graphify: str, results) -> None:
    try:
        result = sync_graph(
            Path(repository), Path(data_root), graphify=graphify
        )
    except BaseException as error:
        results.put(("error", repr(error)))
    else:
        results.put(("ok", result.action))


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
        self._git(
            destination, "config", "user.email", "tests@example.invalid"
        )
        self._git(destination, "config", "user.name", "Graph tests")

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

    def test_interrupted_worktree_migration_reconciles_state_on_retry(self) -> None:
        import integrations.common.graph_manager as graph_manager

        linked = self.root / "interrupted-linked"
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

        for repository in (self.first_clone, linked):
            (repository / "dirty.txt").write_text("changed\n", encoding="utf-8")
            prior_id = uuid.uuid4()
            stale_id = uuid.uuid4()
            self._git(
                repository,
                "config",
                "--worktree",
                "orichum.checkoutIdentity",
                str(prior_id),
            )
            git_dir = Path(
                self._git(
                    repository, "rev-parse", "--absolute-git-dir"
                ).strip()
            )
            state_file = git_dir / "orichum.checkoutIdentity"
            state_file.write_text(str(stale_id), encoding="ascii")
            git_call = graph_manager._git

            def fail_config_removal(target, *arguments):
                if arguments == (
                    "config",
                    "--worktree",
                    "--unset-all",
                    "orichum.checkoutIdentity",
                ):
                    return subprocess.CompletedProcess(
                        ["git"], returncode=1, stdout="", stderr=""
                    )
                return git_call(target, *arguments)

            with mock.patch(
                "integrations.common.graph_manager._git",
                side_effect=fail_config_removal,
            ), self.assertRaises(GraphManagerError):
                resolve_graph_target(repository, self.data_root)

            self.assertEqual(uuid.UUID(state_file.read_text()), prior_id)
            self.assertEqual(
                self._git(
                    repository,
                    "config",
                    "--worktree",
                    "--get",
                    "orichum.checkoutIdentity",
                ).strip(),
                str(prior_id),
            )
            self.assertEqual(
                list(git_dir.glob(".orichum.checkoutIdentity.*.tmp")),
                [],
            )

            target = resolve_graph_target(repository, self.data_root)

            self.assertEqual(target.state_id[:32], prior_id.hex)
            self.assertEqual(uuid.UUID(state_file.read_text()), prior_id)
            migrated = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "config",
                    "--worktree",
                    "--get",
                    "orichum.checkoutIdentity",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(migrated.returncode, 0)
            self.assertEqual(
                list(git_dir.glob(".orichum.checkoutIdentity.*.tmp")),
                [],
            )

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

    def test_user_quarantine_prefix_content_uses_working_target(self) -> None:
        user_content = (
            self.first_clone / ".orichum-legacy-graphify-user-content"
        )
        user_content.mkdir()
        (user_content / "notes.txt").write_text(
            "user content\n", encoding="utf-8"
        )

        target = resolve_graph_target(self.first_clone, self.data_root)

        self.assertEqual(target.kind, "working")

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


class GraphLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data_root = self.root / "orichum-data"
        self.data_root.mkdir(mode=0o700)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "Graph tests")
        (self.repository / "source.py").write_text(
            "print('fixture')\n", encoding="utf-8"
        )
        self._git("add", "source.py")
        self._git("commit", "-qm", "Initial commit")
        self._git(
            "remote", "add", "origin",
            "https://github.com/example/repository.git",
        )
        self.calls_file = self.root / "calls.jsonl"
        self.failure_file = self.root / "fail-next"
        self.active_file = self.root / "active-count"
        self.max_active_file = self.root / "max-active-count"
        self.graphify = str(self.root / "graphify")
        Path(self.graphify).write_text(
            """#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

output = Path(os.environ["GRAPHIFY_OUT"])
repository = Path(sys.argv[2])
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
call = [*sys.argv[1:], os.environ["GRAPHIFY_OUT"], os.getcwd()]
with Path(os.environ["GRAPH_TEST_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(call) + "\\n")
failure = Path(os.environ["GRAPH_TEST_FAIL"])
if failure.exists():
    failure.unlink()
    print("simulated failure", file=sys.stderr)
    raise SystemExit(2)
if os.environ.get("GRAPH_TEST_UNSUPPORTED") == "1":
    print("found 0 code")
    print("graph is empty", file=sys.stderr)
    raise SystemExit(1)
counter = Path(os.environ["GRAPH_TEST_ACTIVE"])
maximum = Path(os.environ["GRAPH_TEST_MAX_ACTIVE"])
counter.touch(exist_ok=True)
with counter.open("r+", encoding="ascii") as state:
    fcntl.flock(state, fcntl.LOCK_EX)
    value = int(state.read() or "0") + 1
    state.seek(0)
    state.truncate()
    state.write(str(value))
    maximum.write_text(str(max(value, int(maximum.read_text() or "0") if maximum.exists() else 0)), encoding="ascii")
    fcntl.flock(state, fcntl.LOCK_UN)
time.sleep(float(os.environ.get("GRAPH_TEST_DELAY", "0")))
output.mkdir(parents=True, exist_ok=True)
(output / "graph.json").write_text(
    json.dumps({
        "built_at_commit": commit,
        "nodes": [{"id": "node", "source_file": "source.py"}],
        "links": [],
    }),
    encoding="utf-8",
)
mutation = os.environ.get("GRAPH_TEST_MUTATION")
if mutation == "dirty":
    (repository / "source.py").write_text(
        "print('changed during graphify')\\n", encoding="utf-8"
    )
elif mutation == "commit":
    (repository / "source.py").write_text(
        "print('committed during graphify')\\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "source.py"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(repository),
            "commit", "-qm", "Concurrent commit",
        ],
        check=True,
    )
elif mutation == "checkout":
    subprocess.run(
        ["git", "-C", str(repository), "checkout", "-q", "HEAD^"],
        check=True,
    )
with counter.open("r+", encoding="ascii") as state:
    fcntl.flock(state, fcntl.LOCK_EX)
    value = int(state.read() or "0") - 1
    state.seek(0)
    state.truncate()
    state.write(str(value))
    fcntl.flock(state, fcntl.LOCK_UN)
""",
            encoding="utf-8",
        )
        Path(self.graphify).chmod(0o755)
        self.environment = {
            "GRAPH_TEST_CALLS": str(self.calls_file),
            "GRAPH_TEST_FAIL": str(self.failure_file),
            "GRAPH_TEST_ACTIVE": str(self.active_file),
            "GRAPH_TEST_MAX_ACTIVE": str(self.max_active_file),
        }
        self.environment_patch = mock.patch.dict(
            os.environ, self.environment, clear=False
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def calls(self) -> list[tuple[str, ...]]:
        if not self.calls_file.exists():
            return []
        return [
            tuple(json.loads(line))
            for line in self.calls_file.read_text(encoding="utf-8").splitlines()
        ]

    def target(self):
        return resolve_graph_target(self.repository, self.data_root)

    def test_missing_graph_runs_code_only_extract_in_central_directory(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )

        self.assertEqual(result.action, "created")
        self.assertEqual(
            self.calls()[0][:3],
            ("extract", str(self.repository), "--code-only"),
        )
        self.assertTrue(Path(self.calls()[0][3]).is_absolute())
        self.assertNotEqual(Path(self.calls()[0][3]), result.output_dir)
        self.assertEqual(Path(self.calls()[0][4]), self.repository)
        self.assertTrue(result.output_dir.is_absolute())
        self.assertFalse((self.repository / "graphify-out").exists())

    def test_existing_graph_runs_incremental_update(self):
        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        second = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )

        self.assertEqual(first.action, "created")
        self.assertEqual(second.action, "updated")
        self.assertEqual(self.calls()[-1][0], "update")

    def test_commit_during_graphify_does_not_activate_old_target(self):
        original = self.target()

        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_MUTATION": "commit"}, clear=False
        ), self.assertRaisesRegex(GraphError, "retry"):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        current = self.target()
        self.assertNotEqual(current.revision, original.revision)
        self.assertFalse(original.output_dir.exists())
        self.assertFalse(current.output_dir.exists())

    def test_checkout_during_graphify_does_not_activate_old_target(self):
        (self.repository / "source.py").write_text(
            "print('second commit')\n", encoding="utf-8"
        )
        self._git("add", "source.py")
        self._git("commit", "-qm", "Second commit")
        original = self.target()

        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_MUTATION": "checkout"}, clear=False
        ), self.assertRaisesRegex(GraphError, "retry"):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        current = self.target()
        self.assertNotEqual(current.revision, original.revision)
        self.assertFalse(original.output_dir.exists())
        self.assertFalse(current.output_dir.exists())

    def test_dirty_edit_during_graphify_does_not_activate_revision_target(self):
        original = self.target()

        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_MUTATION": "dirty"}, clear=False
        ), self.assertRaisesRegex(GraphError, "retry"):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        current = self.target()
        self.assertEqual(original.kind, "revision")
        self.assertEqual(current.kind, "working")
        self.assertFalse(original.output_dir.exists())
        self.assertFalse(current.output_dir.exists())

    def test_clean_revision_graph_is_shared_across_clones(self):
        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        second_clone = self.root / "second-clone"
        subprocess.run(
            ["git", "clone", "-q", str(self.repository), str(second_clone)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(second_clone),
                "config", "user.email", "tests@example.invalid",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(second_clone),
                "config", "user.name", "Graph tests",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(second_clone), "remote", "set-url", "origin",
                "https://github.com/example/repository.git",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        second_target = resolve_graph_target(second_clone, self.data_root)

        self.assertEqual(first.output_dir, second_target.output_dir)
        self.assertEqual(inspect_graph(second_target).status, "current")
        second = sync_graph(
            second_clone, self.data_root, graphify=self.graphify
        )
        self.assertEqual(second.action, "updated")
        metadata = json.loads(
            second_target.metadata_file.read_text(encoding="utf-8")
        )
        self.assertNotIn("checkout_path", metadata)

    def test_failed_update_preserves_last_valid_graph(self):
        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        original = first.graph_file.read_bytes()
        self.failure_file.touch()

        with self.assertRaises(GraphError):
            sync_graph(self.repository, self.data_root, graphify=self.graphify)

        self.assertEqual(first.graph_file.read_bytes(), original)

    def test_failed_atomic_exchange_preserves_valid_graph(self):
        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        original = first.graph_file.read_bytes()

        with mock.patch(
            "integrations.common.graph_manager._atomic_exchange_directories",
            side_effect=GraphError("simulated exchange failure"),
        ), self.assertRaises(GraphError):
            sync_graph(self.repository, self.data_root, graphify=self.graphify)

        self.assertEqual(first.graph_file.read_bytes(), original)

    def test_update_activation_never_removes_the_active_directory(self):
        import integrations.common.graph_manager as graph_manager

        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        real_replace = os.replace

        def require_active_during_publish(source, destination):
            if Path(destination) == first.output_dir:
                self.assertTrue(
                    first.output_dir.exists(),
                    "active graph disappeared during update activation",
                )
            return real_replace(source, destination)

        with mock.patch(
            "integrations.common.graph_manager.os.replace",
            side_effect=require_active_during_publish,
        ):
            second = sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        self.assertEqual(second.action, "updated")
        self.assertEqual(inspect_graph(self.target()).status, "current")

    def test_atomic_exchange_fails_closed_on_unsupported_platform(self):
        import integrations.common.graph_manager as graph_manager

        active = self.root / "active"
        staged = self.root / "staged"
        active.mkdir()
        staged.mkdir()
        (active / "marker").write_text("active", encoding="utf-8")
        (staged / "marker").write_text("staged", encoding="utf-8")

        with mock.patch.object(
            graph_manager.sys, "platform", "win32"
        ), self.assertRaises(GraphError):
            graph_manager._atomic_exchange_directories(staged, active)

        self.assertEqual(
            (active / "marker").read_text(encoding="utf-8"), "active"
        )
        self.assertEqual(
            (staged / "marker").read_text(encoding="utf-8"), "staged"
        )

    def test_exchange_cleanup_failure_leaves_active_and_allows_retry(self):
        import integrations.common.graph_manager as graph_manager

        active = self.root / "active"
        staged = self.root / "staged"
        active.mkdir()
        staged.mkdir()
        (active / "marker").write_text("old", encoding="utf-8")
        (staged / "marker").write_text("new", encoding="utf-8")

        with mock.patch.object(
            graph_manager.shutil,
            "rmtree",
            side_effect=OSError("simulated interrupted cleanup"),
        ):
            graph_manager._activate_staged_output(staged, active)

        self.assertEqual(
            (active / "marker").read_text(encoding="utf-8"), "new"
        )
        self.assertEqual(
            (staged / "marker").read_text(encoding="utf-8"), "old"
        )

        newer = self.root / "newer"
        newer.mkdir()
        (newer / "marker").write_text("newer", encoding="utf-8")
        graph_manager._activate_staged_output(newer, active)
        self.assertEqual(
            (active / "marker").read_text(encoding="utf-8"), "newer"
        )

    def test_project_root_and_submodule_discovery(self):
        nested = self.repository / "vendor" / "module"
        nested.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", str(nested)],
            check=True,
            capture_output=True,
            text=True,
        )
        (nested / "nested.py").write_text("pass\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(nested), "add", "nested.py"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(nested),
                "-c", "user.name=Graph tests",
                "-c", "user.email=tests@example.invalid",
                "commit", "-qm", "Nested commit",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            discover_graph_targets(self.root),
            (self.repository, nested),
        )
        results = sync_graphs(
            self.root, self.data_root, graphify=self.graphify
        )
        self.assertEqual(
            tuple(result.repository for result in results),
            (self.repository, nested),
        )

    def test_unsupported_code_returns_not_applicable_without_activation(self):
        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_UNSUPPORTED": "1"}, clear=False
        ):
            result = sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        self.assertEqual(result.action, "not-applicable")
        self.assertEqual(result.node_count, 0)
        self.assertFalse(result.output_dir.exists())

    def test_not_applicable_sync_still_prunes_orphaned_working_graphs(self):
        target = self.target()
        stale = (
            self.data_root
            / "graphs"
            / target.identity.key
            / "working"
            / "stale"
            / "graphify-out"
        )
        stale.mkdir(parents=True)
        relative = stale.relative_to(self.data_root)
        directory = self.data_root
        for component in relative.parts:
            directory /= component
            directory.chmod(0o700)
        (stale / "metadata.json").write_text(
            json.dumps({
                "schema_version": 1,
                "repository_identity": target.identity.key,
                "revision": target.revision,
                "state_id": "stale",
                "kind": "working",
                "checkout_path": str(self.root / "deleted-checkout"),
                "built_at_commit": target.revision,
            }),
            encoding="utf-8",
        )

        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_UNSUPPORTED": "1"}, clear=False
        ):
            result = sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        self.assertEqual(result.action, "not-applicable")
        self.assertFalse(stale.parent.exists())

    def test_graph_validation_rejects_absolute_and_escaping_sources(self):
        for source_file in ("/tmp/source.py", "../source.py"):
            with self.subTest(source_file=source_file):
                target = self.target()
                target.output_dir.mkdir(parents=True, exist_ok=True)
                target.output_dir.chmod(0o700)
                target.graph_file.write_text(
                    json.dumps({
                        "built_at_commit": target.revision,
                        "nodes": [{
                            "id": "node",
                            "source_file": source_file,
                        }],
                        "links": [],
                    }),
                    encoding="utf-8",
                )
                target.metadata_file.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "repository_identity": target.identity.key,
                        "revision": target.revision,
                        "state_id": target.state_id,
                        "kind": target.kind,
                        "checkout_path": str(target.repository),
                        "built_at_commit": target.revision,
                    }),
                    encoding="utf-8",
                )
                self.assertEqual(inspect_graph(target).status, "invalid")
                shutil.rmtree(target.output_dir)

    def test_graph_validation_rejects_escaping_link_source_paths(self):
        for source_file in ("/tmp/link.py", "../link.py"):
            with self.subTest(source_file=source_file):
                target = self.target()
                target.output_dir.mkdir(parents=True, exist_ok=True)
                target.output_dir.chmod(0o700)
                target.graph_file.write_text(
                    json.dumps({
                        "built_at_commit": target.revision,
                        "nodes": [{
                            "id": "node",
                            "source_file": "source.py",
                        }],
                        "links": [{
                            "source": "node",
                            "target": "node",
                            "source_file": source_file,
                        }],
                    }),
                    encoding="utf-8",
                )
                target.metadata_file.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "repository_identity": target.identity.key,
                        "revision": target.revision,
                        "state_id": target.state_id,
                        "kind": target.kind,
                        "built_at_commit": target.revision,
                    }),
                    encoding="utf-8",
                )

                self.assertEqual(inspect_graph(target).status, "invalid")
                shutil.rmtree(target.output_dir)

    def test_graph_validation_allows_absent_and_null_link_source_paths(self):
        for link in (
            {"source": "node", "target": "node"},
            {"source": "node", "target": "node", "source_file": None},
        ):
            with self.subTest(link=link):
                target = self.target()
                target.output_dir.mkdir(parents=True, exist_ok=True)
                target.output_dir.chmod(0o700)
                target.graph_file.write_text(
                    json.dumps({
                        "built_at_commit": target.revision,
                        "nodes": [{
                            "id": "node",
                            "source_file": "source.py",
                        }],
                        "links": [link],
                    }),
                    encoding="utf-8",
                )
                target.metadata_file.write_text(
                    json.dumps({
                        "schema_version": 1,
                        "repository_identity": target.identity.key,
                        "revision": target.revision,
                        "state_id": target.state_id,
                        "kind": target.kind,
                        "built_at_commit": target.revision,
                    }),
                    encoding="utf-8",
                )

                self.assertEqual(inspect_graph(target).status, "current")
                shutil.rmtree(target.output_dir)

    def test_graph_validation_allows_empty_source_paths(self):
        target = self.target()
        target.output_dir.mkdir(parents=True, exist_ok=True)
        target.output_dir.chmod(0o700)
        target.graph_file.write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{
                    "id": "imported-symbol",
                    "source_file": "",
                }],
                "links": [{
                    "source": "imported-symbol",
                    "target": "imported-symbol",
                    "source_file": "",
                }],
            }),
            encoding="utf-8",
        )
        target.metadata_file.write_text(
            json.dumps({
                "schema_version": 1,
                "repository_identity": target.identity.key,
                "revision": target.revision,
                "state_id": target.state_id,
                "kind": target.kind,
                "built_at_commit": target.revision,
            }),
            encoding="utf-8",
        )

        self.assertEqual(inspect_graph(target).status, "current")

    def test_graph_provenance_requires_matching_40_hex_commit(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        original = json.loads(result.graph_file.read_text(encoding="utf-8"))
        cases = (None, "abc", "f" * 40)

        for built_at_commit in cases:
            with self.subTest(built_at_commit=built_at_commit):
                graph = dict(original)
                if built_at_commit is None:
                    graph.pop("built_at_commit", None)
                else:
                    graph["built_at_commit"] = built_at_commit
                result.graph_file.write_text(
                    json.dumps(graph), encoding="utf-8"
                )

                self.assertEqual(
                    inspect_graph(self.target()).status, "invalid"
                )

    def test_built_at_commit_mismatch_is_stale(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        metadata = json.loads(
            (result.output_dir / "metadata.json").read_text(encoding="utf-8")
        )
        metadata["built_at_commit"] = "0" * 40
        (result.output_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )

        self.assertEqual(inspect_graph(self.target()).status, "stale")

    def test_stale_graph_is_repaired_with_fresh_code_only_extract(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        metadata_file = result.output_dir / "metadata.json"
        metadata = json.loads(
            metadata_file.read_text(encoding="utf-8")
        )
        metadata["built_at_commit"] = "0" * 40
        metadata_file.write_text(
            json.dumps(metadata), encoding="utf-8"
        )

        repaired = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )

        self.assertEqual(repaired.action, "updated")
        self.assertEqual(
            self.calls()[-1][:3],
            ("extract", str(self.repository), "--code-only"),
        )
        self.assertEqual(inspect_graph(self.target()).status, "current")

    def test_invalid_graph_is_repaired_with_fresh_code_only_extract(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        result.graph_file.write_text("{}", encoding="utf-8")

        repaired = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )

        self.assertEqual(repaired.action, "updated")
        self.assertEqual(
            self.calls()[-1][:3],
            ("extract", str(self.repository), "--code-only"),
        )
        self.assertEqual(inspect_graph(self.target()).status, "current")

    def test_failed_repair_preserves_stale_target_for_diagnosis(self):
        result = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        metadata_file = result.output_dir / "metadata.json"
        metadata = json.loads(
            metadata_file.read_text(encoding="utf-8")
        )
        metadata["built_at_commit"] = "0" * 40
        metadata_file.write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        before = {
            path.relative_to(result.output_dir): path.read_bytes()
            for path in result.output_dir.rglob("*")
            if path.is_file()
        }

        with mock.patch(
            "integrations.common.graph_manager._atomic_exchange_directories",
            side_effect=GraphError("simulated repair activation failure"),
        ), self.assertRaises(GraphError):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        after = {
            path.relative_to(result.output_dir): path.read_bytes()
            for path in result.output_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)
        self.assertEqual(inspect_graph(self.target()).status, "stale")
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(
            self.calls()[-1][:3],
            ("extract", str(self.repository), "--code-only"),
        )

    def test_concurrent_syncs_are_excluded_by_repository_lock(self):
        context = multiprocessing.get_context("fork")
        results = context.Queue()
        with mock.patch.dict(
            os.environ, {"GRAPH_TEST_DELAY": "0.25"}, clear=False
        ):
            processes = [
                context.Process(
                    target=_sync_in_process,
                    args=(
                        str(self.repository),
                        str(self.data_root),
                        self.graphify,
                        results,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

        self.assertEqual(
            [results.get(timeout=2)[0] for _ in processes], ["ok", "ok"]
        )
        self.assertEqual(self.max_active_file.read_text(encoding="ascii"), "1")

    def test_non_private_central_graph_root_is_rejected(self):
        graphs = self.data_root / "graphs"
        graphs.mkdir(mode=0o755)

        with self.assertRaises(GraphError):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        self.assertEqual(self.calls(), [])

    def test_symlinked_graph_lock_is_rejected_without_changing_target(self):
        target = self.target()
        identity_root = (
            self.data_root / "graphs" / target.identity.key
        )
        identity_root.mkdir(parents=True)
        relative = identity_root.relative_to(self.data_root)
        directory = self.data_root
        for component in relative.parts:
            directory /= component
            directory.chmod(0o700)
        outside = self.root / "outside-lock"
        outside.write_text("outside\n", encoding="utf-8")
        outside.chmod(0o644)
        (identity_root / ".orichum.lock").symlink_to(outside)

        with self.assertRaises(GraphError):
            sync_graph(
                self.repository, self.data_root, graphify=self.graphify
            )

        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)

    def test_safe_legacy_migration_copies_then_removes_source(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )

        self.assertTrue(migrate_legacy_graph(target))
        self.assertTrue(target.graph_file.is_file())
        self.assertFalse(legacy.exists())
        self.assertEqual(inspect_graph(target).status, "current")

    def test_migration_rejects_wrong_graph_provenance_and_restores_source(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        original = json.dumps({
            "built_at_commit": "f" * 40,
            "nodes": [{"id": "node", "source_file": "source.py"}],
            "links": [],
        })
        (legacy / "graph.json").write_text(original, encoding="utf-8")

        with self.assertRaises(GraphError):
            migrate_legacy_graph(target)

        self.assertEqual(
            (legacy / "graph.json").read_text(encoding="utf-8"), original
        )
        self.assertFalse(target.output_dir.exists())

    def test_migration_accepts_all_documented_graphify_outputs(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        for name in (
            "cost.json",
            "manifest.json",
            ".graphify_build.json",
            ".graphify_labels.json",
            ".graphify_analysis.json",
            ".graphify_semantic_marker",
            ".needs_update",
            "needs_update",
            "example-project-callflow.html",
        ):
            (legacy / name).write_text("{}\n", encoding="utf-8")
        cache = legacy / "cache"
        cache.mkdir()
        (cache / "stat-index.json").write_text("{}\n", encoding="utf-8")
        obsidian = legacy / "obsidian"
        obsidian.mkdir()
        (obsidian / "index.md").write_text("# Graph\n", encoding="utf-8")

        self.assertTrue(migrate_legacy_graph(target))

    def test_unignored_legacy_output_migrates_once_to_revision_target(self):
        self._git("config", "core.excludesFile", os.devnull)
        (self.repository / ".git" / "info" / "exclude").write_text(
            "", encoding="utf-8"
        )
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )

        before = target
        first = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        second = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )

        self.assertEqual(before.kind, "revision")
        self.assertEqual(first.action, "migrated")
        self.assertEqual(first.output_dir, before.output_dir)
        self.assertEqual(second.action, "updated")
        identity_root = self.data_root / "graphs" / before.identity.key
        self.assertFalse((identity_root / "working").exists())
        self.assertEqual(
            tuple((identity_root / "revisions").rglob("graph.json")),
            (first.graph_file,),
        )

    def test_hook_cleanup_finishes_on_the_post_cleanup_graph_state(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        self._git(
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self._git(
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        attributes.write_text(
            "graphify-out/graph.json merge=graphify\n",
            encoding="utf-8",
        )

        result = sync_graphs(
            self.repository, self.data_root, graphify=self.graphify
        )[0]
        current = self.target()

        self.assertFalse(attributes.exists())
        self.assertEqual(result.output_dir, current.output_dir)
        self.assertEqual(inspect_graph(current).status, "current")

    def test_migration_ignores_owned_macos_metadata(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        (legacy / ".DS_Store").write_bytes(b"finder")
        dated = legacy / "2026-07-27"
        dated.mkdir()
        (dated / ".DS_Store").write_bytes(b"finder")

        self.assertTrue(migrate_legacy_graph(target))
        self.assertFalse((target.output_dir / ".DS_Store").exists())
        self.assertFalse(
            (target.output_dir / "2026-07-27" / ".DS_Store").exists()
        )

    def test_unknown_legacy_entry_refuses_migration_and_preserves_source(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        (legacy / "graph.json").write_text(
            json.dumps({
                "nodes": [{"id": "node", "source_file": "source.py"}]
            }),
            encoding="utf-8",
        )
        (legacy / "unexpected.bin").write_bytes(b"unknown")

        with self.assertRaisesRegex(GraphError, r"unexpected\.bin"):
            migrate_legacy_graph(self.target())

        self.assertTrue(legacy.exists())
        self.assertFalse(self.target().output_dir.exists())

    def test_nested_or_arbitrary_callflow_html_refuses_migration(self):
        for relative in (
            Path("architecture.html"),
            Path("docs") / "example-project-callflow.html",
        ):
            with self.subTest(relative=relative):
                legacy = self.repository / "graphify-out"
                legacy.mkdir()
                (legacy / "graph.json").write_text(
                    json.dumps({
                        "nodes": [{
                            "id": "node",
                            "source_file": "source.py",
                        }]
                    }),
                    encoding="utf-8",
                )
                artifact = legacy / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text("<html></html>\n", encoding="utf-8")

                with self.assertRaises(GraphError):
                    migrate_legacy_graph(self.target())

                self.assertTrue(legacy.exists())
                self.assertFalse(self.target().output_dir.exists())
                shutil.rmtree(legacy)

    def test_legacy_migration_refuses_foreign_owned_source(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        (legacy / "graph.json").write_text(
            json.dumps({
                "nodes": [{"id": "node", "source_file": "source.py"}]
            }),
            encoding="utf-8",
        )
        target = self.target()

        with mock.patch(
            "integrations.common.graph_manager.os.getuid",
            return_value=os.getuid() + 1,
        ), self.assertRaises(GraphError):
            migrate_legacy_graph(target)

        self.assertTrue(legacy.exists())
        self.assertFalse(target.output_dir.exists())

    def test_legacy_migration_refuses_to_replace_an_active_graph(self):
        active = sync_graph(
            self.repository, self.data_root, graphify=self.graphify
        )
        original = active.graph_file.read_bytes()
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        (legacy / "graph.json").write_text(
            json.dumps({
                "nodes": [{"id": "legacy", "source_file": "source.py"}]
            }),
            encoding="utf-8",
        )

        with self.assertRaises(GraphError):
            migrate_legacy_graph(self.target())

        self.assertEqual(active.graph_file.read_bytes(), original)
        self.assertTrue(legacy.exists())

    def test_failed_migration_activation_rolls_back_and_preserves_source(self):
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        original_replace = os.replace

        def fail_activation(source, destination):
            if Path(destination) == target.output_dir:
                raise OSError("simulated activation failure")
            return original_replace(source, destination)

        with mock.patch(
            "integrations.common.graph_manager.os.replace",
            side_effect=fail_activation,
        ), self.assertRaises(GraphError):
            migrate_legacy_graph(target)

        self.assertTrue(legacy.exists())
        self.assertFalse(target.output_dir.exists())

    def test_migration_does_not_delete_replacement_legacy_directory(self):
        import integrations.common.graph_manager as graph_manager

        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        real_copy = graph_manager._copy_legacy_tree

        def replace_source_after_open(source, destination):
            source = Path(source)
            if not legacy.exists():
                legacy.mkdir()
                (legacy / "replacement.txt").write_text(
                    "replacement\n", encoding="utf-8"
                )
            return real_copy(source, destination)

        with mock.patch(
            "integrations.common.graph_manager._copy_legacy_tree",
            side_effect=replace_source_after_open,
        ):
            self.assertTrue(migrate_legacy_graph(target))

        self.assertEqual(
            (legacy / "replacement.txt").read_text(encoding="utf-8"),
            "replacement\n",
        )

    def test_migration_restores_source_when_binding_changes_after_copy(self):
        import integrations.common.graph_manager as graph_manager

        target = self.target()
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        real_copy = graph_manager._copy_legacy_tree

        def copy_then_dirty(source, destination):
            hashes = real_copy(source, destination)
            (self.repository / "source.py").write_text(
                "print('dirty during migration')\n", encoding="utf-8"
            )
            return hashes

        with mock.patch(
            "integrations.common.graph_manager._copy_legacy_tree",
            side_effect=copy_then_dirty,
        ), self.assertRaisesRegex(GraphError, "retry"):
            migrate_legacy_graph(target)

        self.assertTrue(legacy.exists())
        self.assertFalse(target.output_dir.exists())
        self.assertEqual(self.target().kind, "working")

    def test_active_quarantine_is_ignored_only_by_migration_recheck(self):
        import integrations.common.graph_manager as graph_manager

        target = self.target()
        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        real_copy = graph_manager._copy_legacy_tree
        observed = []

        def observe_quarantine(source, destination):
            hashes = real_copy(source, destination)
            observed.append(self.target().kind)
            return hashes

        with mock.patch(
            "integrations.common.graph_manager._copy_legacy_tree",
            side_effect=observe_quarantine,
        ):
            self.assertTrue(migrate_legacy_graph(target))

        self.assertEqual(observed, ["working"])
        self.assertEqual(inspect_graph(target).status, "current")

    def test_quarantine_cleanup_failure_rolls_back_and_retry_migrates(self):
        import integrations.common.graph_manager as graph_manager

        legacy = self.repository / "graphify-out"
        legacy.mkdir()
        target = self.target()
        (legacy / "graph.json").write_text(
            json.dumps({
                "built_at_commit": target.revision,
                "nodes": [{"id": "node", "source_file": "source.py"}],
                "links": [],
            }),
            encoding="utf-8",
        )
        real_rmtree = shutil.rmtree
        failed = False

        def fail_quarantine_cleanup(path, *arguments, **keywords):
            nonlocal failed
            if (
                not failed
                and Path(path).name.startswith(
                    ".orichum-legacy-graphify-"
                )
            ):
                failed = True
                raise OSError("simulated quarantine cleanup failure")
            return real_rmtree(path, *arguments, **keywords)

        with mock.patch.object(
            graph_manager.shutil,
            "rmtree",
            side_effect=fail_quarantine_cleanup,
        ), self.assertRaises(GraphError):
            migrate_legacy_graph(target)

        self.assertTrue(legacy.exists())
        self.assertFalse(target.output_dir.exists())
        self.assertTrue(migrate_legacy_graph(target))
        self.assertFalse(legacy.exists())
        self.assertEqual(inspect_graph(target).status, "current")

    def test_prunes_only_orphaned_working_graphs(self):
        identity = self.target().identity
        working = self.data_root / "graphs" / identity.key / "working"
        revisions = self.data_root / "graphs" / identity.key / "revisions"
        existing_checkout = self.root / "existing-checkout"
        existing_checkout.mkdir()
        stale = working / "stale" / "graphify-out"
        current = working / "current" / "graphify-out"
        revision = revisions / ("a" * 40) / "graphify-out"
        for output, checkout in (
            (stale, self.root / "deleted-checkout"),
            (current, existing_checkout),
            (revision, self.root / "deleted-revision-checkout"),
        ):
            output.mkdir(parents=True)
            relative = output.relative_to(self.data_root)
            directory = self.data_root
            for component in relative.parts:
                directory /= component
                directory.chmod(0o700)
            (output / "metadata.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "repository_identity": identity.key,
                    "revision": "a" * 40,
                    "state_id": output.parent.name,
                    "kind": "working",
                    "checkout_path": str(checkout),
                    "built_at_commit": "a" * 40,
                }),
                encoding="utf-8",
            )

        removed = prune_orphaned_working_graphs(identity, self.data_root)

        self.assertEqual(removed, (stale.parent,))
        self.assertFalse(stale.parent.exists())
        self.assertTrue(current.parent.exists())
        self.assertTrue(revision.parent.exists())

    def test_pruning_preserves_orphan_with_inconsistent_metadata(self):
        target = self.target()
        state = (
            self.data_root
            / "graphs"
            / target.identity.key
            / "working"
            / "invalid"
            / "graphify-out"
        )
        state.mkdir(parents=True)
        relative = state.relative_to(self.data_root)
        directory = self.data_root
        for component in relative.parts:
            directory /= component
            directory.chmod(0o700)
        (state / "metadata.json").write_text(
            json.dumps({
                "schema_version": 1,
                "repository_identity": target.identity.key,
                "revision": target.revision,
                "state_id": "invalid",
                "kind": "working",
                "checkout_path": str(self.root / "deleted-checkout"),
                "built_at_commit": "different-revision",
            }),
            encoding="utf-8",
        )

        self.assertEqual(
            prune_orphaned_working_graphs(target.identity, self.data_root),
            (),
        )
        self.assertTrue(state.parent.exists())


if __name__ == "__main__":
    unittest.main()
