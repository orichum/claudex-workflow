#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import integrations.common.install_control_plane as control_plane
from integrations.common.account_registry import Account, update_accounts
from integrations.common.install_control_plane import (
    InstallControlPlaneError,
    activate,
    finalize,
    recover,
    rollback,
    stage,
)
from integrations.common.plugin_registry import update_plugins


class InstallControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = Path(__file__).resolve().parents[1]
        self.installed = self.root / "installed"
        self.candidate = self.root / "candidate"
        self.installed.mkdir(mode=0o700)
        for name in (
            "model-stacks.json",
            "projects.json",
            "providers.json",
            "plugins.json",
            "runtime.json",
            "controller-policy.md",
        ):
            destination = self.installed / name
            destination.write_bytes(
                (self.repository / "config" / name).read_bytes()
            )
            destination.chmod(0o600)
        self.original_model = (
            self.installed / "model-stacks.json"
        ).read_bytes()
        stage(self.repository, self.installed, self.candidate)

    def _lock_path(self, journal: Path) -> Path:
        lock = Path(journal).parent / "install.lock"
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError:
            pass
        return lock

    def _lock_fd(self, journal: Path) -> int:
        lock = self._lock_path(journal)
        descriptor = os.open(
            lock,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        self.addCleanup(os.close, descriptor)
        return descriptor

    def _activate(self, journal: Path) -> None:
        activate(
            self.candidate,
            self.installed,
            journal,
            self._lock_path(journal),
            self._lock_fd(journal),
        )

    def _rollback(self, journal: Path) -> None:
        rollback(
            self.installed,
            journal,
            self._lock_path(journal),
            self._lock_fd(journal),
        )

    def _recover(self, journal: Path) -> None:
        recover(
            self.installed,
            journal,
            self._lock_path(journal),
            self._lock_fd(journal),
        )

    def _finalize(self, journal: Path) -> None:
        finalize(
            journal,
            self._lock_path(journal),
            self._lock_fd(journal),
        )

    def _assert_original_state(self) -> None:
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            self.original_model,
        )
        self.assertFalse((self.installed / "stack-bindings.json").exists())
        self.assertFalse((self.installed / "accounts.json").exists())
        self.assertFalse(
            any(
                self.installed.glob(".model-stacks.transaction*")
            )
        )

    def test_activation_replaces_and_rollback_restores_controller_policy(
        self,
    ) -> None:
        installed_policy = self.installed / "controller-policy.md"
        stale = b"# stale controller policy\n"
        installed_policy.write_text(
            stale.decode(),
            encoding="utf-8",
        )
        installed_policy.chmod(0o600)
        shutil.rmtree(self.candidate)

        stage(self.repository, self.installed, self.candidate)

        expected = (
            self.repository / "config" / "controller-policy.md"
        ).read_bytes()
        self.assertEqual(
            (self.candidate / "controller-policy.md").read_bytes(),
            expected,
        )
        journal = self.root / "policy-journal"
        self._activate(journal)
        self.assertEqual(installed_policy.read_bytes(), expected)
        self._rollback(journal)
        self.assertEqual(installed_policy.read_bytes(), stale)

    def test_activation_normalizes_and_rollback_restores_projects(self) -> None:
        projects_path = self.installed / "projects.json"
        project_root = self.root / "project"
        project_root.mkdir()
        original = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": str(project_root),
                    "dockerProfile": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                    "githubAccount": None,
                    "memoryPalace": "/private/old",
                    "memoryWing": "old",
                }
            ],
        }
        projects_path.write_text(json.dumps(original), encoding="utf-8")
        projects_path.chmod(0o600)
        shutil.rmtree(self.candidate)
        stage(self.repository, self.installed, self.candidate)

        journal = self.root / "projects-journal"
        self._activate(journal)
        activated = json.loads(projects_path.read_text(encoding="utf-8"))
        self.assertEqual(
            activated["contexts"][0],
            {
                "root": str(project_root),
                "dockerProfile": None,
                "modelStack": None,
                "accountPools": ["shared"],
                "githubAccount": None,
            },
        )

        self._rollback(journal)
        self.assertEqual(
            json.loads(projects_path.read_text(encoding="utf-8")),
            original,
        )

    def test_rollback_accepts_previous_schema_two_journal(self) -> None:
        journal = self.root / "legacy-schema-two-journal"
        self._activate(journal)
        manifest_path = journal / "installed-control-plane.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("priorPolicyPresent")
        manifest.pop("activatedPolicyDigest")
        manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        for name in (
            "installed-controller-policy.data",
            "installed-controller-policy.present",
            "installed-controller-policy.absent",
        ):
            path = journal / name
            if path.exists():
                path.unlink()

        self._rollback(journal)

        self._assert_original_state()
        self.assertFalse(journal.exists())

    def test_rollback_is_idempotent_across_activation_interruptions(
        self,
    ) -> None:
        for checkpoint in (
            "prepared",
            "bootstrap:accounts.json",
            "controller-policy-installed",
            "stack-saved",
            "committed",
        ):
            with self.subTest(checkpoint=checkpoint):
                snapshot = self.root / f"snapshot-{checkpoint.replace(':', '-')}"

                def interrupt(observed: str) -> None:
                    if observed == checkpoint:
                        raise KeyboardInterrupt(checkpoint)

                with mock.patch(
                    "integrations.common.install_control_plane."
                    "_activation_checkpoint",
                    side_effect=interrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        activate(
                            self.candidate,
                            self.installed,
                            snapshot,
                            self._lock_path(snapshot),
                            self._lock_fd(snapshot),
                        )
                self._rollback(snapshot)
                self._rollback(snapshot)
                self._assert_original_state()

    def test_rollback_before_manifest_is_a_safe_noop(self) -> None:
        snapshot = self.root / "pre-manifest-snapshot"
        snapshot.mkdir(mode=0o700)
        self._rollback(snapshot)
        self._assert_original_state()

    def test_activation_rejects_symlinked_journal_parent(self) -> None:
        state = self.root / "real-state"
        state.mkdir(mode=0o700)
        linked_state = self.root / "linked-state"
        linked_state.symlink_to(state, target_is_directory=True)

        with self.assertRaisesRegex(
            InstallControlPlaneError,
            "journal parent is unsafe",
        ):
            activate(
                self.candidate,
                self.installed,
                linked_state / "install-control-plane",
                self._lock_path(
                    linked_state / "install-control-plane"
                ),
                self._lock_fd(
                    linked_state / "install-control-plane"
                ),
            )

        self.assertFalse((state / "install-control-plane").exists())
        self._assert_original_state()

    def test_journal_parent_identity_swap_fails_closed(self) -> None:
        state = self.root / "swap-state"
        state.mkdir(mode=0o700)
        observed = os.lstat(state)
        changed_fields = list(observed)
        changed_fields[1] += 1
        changed = os.stat_result(changed_fields)

        with (
            mock.patch.object(Path, "resolve", return_value=state),
            mock.patch.object(
                control_plane,
                "_require_private_root",
                return_value=None,
            ),
            mock.patch.object(
                control_plane.os,
                "lstat",
                side_effect=(observed, changed, observed),
            ),
        ):
            with self.assertRaisesRegex(
                InstallControlPlaneError,
                "journal parent changed",
            ):
                control_plane._private_child(
                    state / "install-control-plane"
                )

    def test_journal_mkdir_is_synced_before_first_snapshot(self) -> None:
        state = self.root / "durable-state"
        state.mkdir(mode=0o700)
        journal = state / "install-control-plane"
        events: list[tuple[str, object]] = []
        real_snapshot = control_plane._snapshot
        state_details = os.stat(state)
        state_identity = (
            state_details.st_dev,
            state_details.st_ino,
        )

        def record_fsync(descriptor: int) -> None:
            details = os.fstat(descriptor)
            events.append(
                ("fsync", (details.st_dev, details.st_ino))
            )

        def record_snapshot(*args, **kwargs):
            events.append(("snapshot", Path(args[0])))
            return real_snapshot(*args, **kwargs)

        with (
            mock.patch.object(
                control_plane.os,
                "fsync",
                side_effect=record_fsync,
            ),
            mock.patch.object(
                control_plane,
                "_snapshot",
                side_effect=record_snapshot,
            ),
        ):
            self._activate(journal)

        self.assertEqual(events[0], ("fsync", state_identity))
        first_snapshot = next(
            index
            for index, event in enumerate(events)
            if event[0] == "snapshot"
        )
        self.assertLess(
            events.index(("fsync", state_identity)),
            first_snapshot,
        )

    def test_rollback_preserves_concurrently_updated_bootstrap_account(
        self,
    ) -> None:
        snapshot = self.root / "account-snapshot"
        self._activate(snapshot)
        account = Account(
            id="oc-a-1111111111111111",
            name="concurrent",
            provider="openai",
            credential_ref="concurrent.json",
            pool="shared",
            routing_prefix="oc-r-1111111111111111",
            priority=100,
            state="active",
            original_prefix=None,
            original_priority=None,
        )
        update_accounts(
            self.installed / "accounts.json",
            lambda current: (*current, account),
        )

        with self.assertRaisesRegex(
            InstallControlPlaneError,
            "accounts.json changed after installer activation",
        ):
            self._rollback(snapshot)

        accounts = json.loads(
            (self.installed / "accounts.json").read_text()
        )["accounts"]
        self.assertEqual([entry["id"] for entry in accounts], [account.id])
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            self.original_model,
        )
        manifest = json.loads(
            (snapshot / "installed-control-plane.json").read_text()
        )
        self.assertEqual(manifest["phase"], "rollbackConflict")
        self.assertEqual(manifest["conflicts"], ["accounts.json"])

    def test_rollback_serializes_concurrent_bootstrap_plugin_update(
        self,
    ) -> None:
        (self.installed / "plugins.json").unlink()
        shutil.rmtree(self.candidate)
        stage(self.repository, self.installed, self.candidate)
        journal = self.root / "plugin-journal"
        self._activate(journal)
        writer_locked = threading.Event()
        release_writer = threading.Event()
        rollback_waiting = threading.Event()
        writer_errors: list[BaseException] = []
        rollback_errors: list[BaseException] = []

        def add_plugin(document: dict[str, object]) -> dict[str, object]:
            writer_locked.set()
            if not release_writer.wait(5):
                raise RuntimeError("plugin writer release timed out")
            return {
                **document,
                "marketplaces": [
                    {"name": "acme", "source": "example/acme"}
                ],
                "plugins": ["sample@acme"],
            }

        def write_plugin() -> None:
            try:
                update_plugins(
                    self.installed / "plugins.json", add_plugin
                )
            except BaseException as error:
                writer_errors.append(error)

        def rollback_install() -> None:
            try:
                self._rollback(journal)
            except BaseException as error:
                rollback_errors.append(error)

        def checkpoint(name: str) -> None:
            if name == "before-lock:plugins.json":
                rollback_waiting.set()

        writer = threading.Thread(target=write_plugin)
        writer.start()
        self.assertTrue(writer_locked.wait(5))
        with mock.patch(
            "integrations.common.install_control_plane."
            "_rollback_checkpoint",
            side_effect=checkpoint,
        ):
            restorer = threading.Thread(target=rollback_install)
            restorer.start()
            self.assertTrue(rollback_waiting.wait(5))
            release_writer.set()
            writer.join(5)
            restorer.join(5)

        self.assertFalse(writer.is_alive())
        self.assertFalse(restorer.is_alive())
        self.assertEqual(writer_errors, [])
        self.assertEqual(len(rollback_errors), 1)
        self.assertIsInstance(
            rollback_errors[0], InstallControlPlaneError
        )
        self.assertIn("plugins.json", str(rollback_errors[0]))
        document = json.loads(
            (self.installed / "plugins.json").read_text()
        )
        self.assertEqual(document["plugins"], ["sample@acme"])

    def test_rollback_fsyncs_bootstrap_unlinks_before_terminal_state(
        self,
    ) -> None:
        journal = self.root / "fsync-journal"
        self._activate(journal)
        events: list[tuple[str, object]] = []
        real_write_manifest = (
            __import__(
                "integrations.common.install_control_plane",
                fromlist=["_write_manifest"],
            )._write_manifest
        )

        def record_fsync(path: Path) -> None:
            events.append(("fsync", Path(path)))

        def record_manifest(
            root: Path, manifest: dict[str, object]
        ) -> None:
            events.append(("manifest", manifest["phase"]))
            real_write_manifest(root, manifest)

        with (
            mock.patch(
                "integrations.common.install_control_plane."
                "_fsync_directory",
                side_effect=record_fsync,
            ),
            mock.patch(
                "integrations.common.install_control_plane."
                "_write_manifest",
                side_effect=record_manifest,
            ),
        ):
            self._rollback(journal)

        config_fsync = events.index(("fsync", self.installed))
        terminal = events.index(("manifest", "rolledBack"))
        self.assertLess(config_fsync, terminal)
        self.assertFalse(journal.exists())

    def test_next_install_recovers_journal_left_by_killed_process(
        self,
    ) -> None:
        journal = self.root / "data/state/install-control-plane"
        journal.parent.mkdir(mode=0o700, parents=True)
        script = """
import os
from pathlib import Path
import sys
import integrations.common.install_control_plane as control

def checkpoint(name):
    if name == "stack-saved":
        os._exit(91)

control._activation_checkpoint = checkpoint
control.activate(
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    int(sys.argv[5]),
)
"""
        lock_path = self._lock_path(journal)
        lock_fd = self._lock_fd(journal)
        killed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(self.candidate),
                str(self.installed),
                str(journal),
                str(lock_path),
                str(lock_fd),
            ],
            cwd=self.repository,
            check=False,
            pass_fds=(lock_fd,),
        )
        self.assertEqual(killed.returncode, 91)
        self.assertTrue(journal.exists())
        self.assertNotEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            self.original_model,
        )

        self._recover(journal)

        self._assert_original_state()
        self.assertFalse(journal.exists())

    def test_recovery_refuses_a_different_installed_root(self) -> None:
        journal = self.root / "target-bound-journal"
        self._activate(journal)
        other = self.root / "other-installed"
        other.mkdir(mode=0o700)

        with self.assertRaisesRegex(
            InstallControlPlaneError,
            "different installed configuration root",
        ):
            recover(
                other,
                journal,
                self._lock_path(journal),
                self._lock_fd(journal),
            )

        self.assertTrue(journal.exists())
        self.assertTrue((self.installed / "accounts.json").exists())

    def test_recovery_rejects_replaced_state_after_lock_acquisition(
        self,
    ) -> None:
        state = self.root / "fenced-state"
        state.mkdir(mode=0o700)
        held_lock = state / "install.lock"
        held_lock.mkdir(mode=0o700)
        descriptor = os.open(
            held_lock,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        self.addCleanup(os.close, descriptor)
        displaced = self.root / "displaced-state"
        state.rename(displaced)
        state.mkdir(mode=0o700)
        (state / "install.lock").mkdir(mode=0o700)
        replacement_journal = state / "install-control-plane"
        replacement_journal.mkdir(mode=0o700)

        with self.assertRaisesRegex(
            InstallControlPlaneError,
            "held installer lock",
        ):
            recover(
                self.installed,
                replacement_journal,
                held_lock,
                descriptor,
            )

        self.assertTrue(replacement_journal.exists())
        self.assertFalse(
            (replacement_journal / "installed-control-plane.json").exists()
        )

    def test_activation_never_returns_to_state_path_after_fd_check(
        self,
    ) -> None:
        state = self.root / "race-state"
        state.mkdir(mode=0o700)
        held_lock = state / "install.lock"
        held_lock.mkdir(mode=0o700)
        descriptor = os.open(
            held_lock,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        self.addCleanup(os.close, descriptor)
        displaced = self.root / "race-state-held"
        replacement_created = False

        def replace_state(phase: str) -> None:
            nonlocal replacement_created
            if phase != "verified" or replacement_created:
                return
            state.rename(displaced)
            state.mkdir(mode=0o700)
            (state / "install.lock").mkdir(mode=0o700)
            replacement_created = True

        with mock.patch.object(
            control_plane,
            "_journal_checkpoint",
            side_effect=replace_state,
            create=True,
        ):
            activate(
                self.candidate,
                self.installed,
                state / "install-control-plane",
                held_lock,
                descriptor,
            )

        self.assertTrue(replacement_created)
        self.assertTrue(
            (displaced / "install-control-plane").exists()
        )
        self.assertFalse((state / "install-control-plane").exists())

    def test_finalize_removes_committed_journal_without_rollback(
        self,
    ) -> None:
        journal = self.root / "finalize-journal"
        self._activate(journal)
        committed_model = (
            self.installed / "model-stacks.json"
        ).read_bytes()

        self._finalize(journal)

        self.assertFalse(journal.exists())
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            committed_model,
        )

    def test_recovery_does_not_rollback_durably_finalized_journal(
        self,
    ) -> None:
        journal = self.root / "finalized-journal"
        self._activate(journal)
        committed_model = (
            self.installed / "model-stacks.json"
        ).read_bytes()
        with mock.patch(
            "integrations.common.install_control_plane._remove_journal",
            side_effect=OSError("injected journal cleanup failure"),
        ):
            with self.assertRaises(OSError):
                self._finalize(journal)
        manifest = json.loads(
            (journal / "installed-control-plane.json").read_text()
        )
        self.assertEqual(manifest["phase"], "finalized")

        self._recover(journal)

        self.assertFalse(journal.exists())
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            committed_model,
        )


if __name__ == "__main__":
    unittest.main()
