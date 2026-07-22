#!/usr/bin/env python3
"""Hermetic tests for workflow-owned session state."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import integrations.common.session_config as session_config
from integrations.common.session_config import (
    ContextBinding,
    SessionError,
    create_session,
    sha256_file,
    verify_context_binding,
    verify_session,
)


class SessionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve(strict=True)
        self.home = self.fixture / "home"
        self.workflow_root = self.fixture / "workflow"
        self.runtime = self.workflow_root / "runtime"
        self.xebia = self.home / "xebia"
        self.complion = self.home / "complion"
        self.launch_dir = self.xebia / "project"
        self.palace = self.home / ".mempalace" / "palaces" / "xebia"
        self.complion_palace = self.home / ".mempalace" / "palaces" / "complion"
        for directory in (
            self.runtime,
            self.launch_dir,
            self.complion,
            self.palace,
            self.complion_palace,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.palace.chmod(0o700)
        self.tools = {
            "docker": "/opt/tools/docker",
            "mempalace-mcp": "/opt/tools/mempalace-mcp",
            "graphify-mcp": "/opt/tools/graphify-mcp",
        }
        which = mock.patch(
            "integrations.common.session_config.shutil.which",
            side_effect=lambda name: self.tools.get(name),
        )
        which.start()
        self.addCleanup(which.stop)
        self.config_path = self.workflow_root / "project-context.json"
        self.write_config(self.palace)

    def write_config(self, palace: Path) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "contexts": [
                        {
                            "root": str(self.xebia),
                            "dockerProfile": "xebia",
                            "memoryPalace": str(palace),
                            "memoryWing": "xebia",
                        },
                        {
                            "root": str(self.complion),
                            "dockerProfile": "realtime",
                            "memoryPalace": str(self.complion_palace),
                            "memoryWing": "complion",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def create(self):
        return create_session(
            self.workflow_root, self.launch_dir, self.config_path
        )

    def assert_rejected(self, action) -> None:
        with self.assertRaises(SessionError):
            action()

    def test_create_session_writes_private_project_mcp_state(self) -> None:
        session = self.create()

        self.assertEqual(stat.S_IMODE(session.run_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(session.context_file.stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(session.mcp_file.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(session.mcp_file.read_text()),
            {
                "mcpServers": {
                    "docker": {
                        "command": "/opt/tools/docker",
                        "args": ["mcp", "gateway", "run", "--profile", "xebia"],
                    },
                    "mempalace": {
                        "command": "/opt/tools/mempalace-mcp",
                        "args": ["--palace", str(self.palace)],
                    },
                }
            },
        )
        self.assertTrue(session.run_id.startswith("run."))
        context = json.loads(session.context_file.read_text())
        self.assertEqual(context["route"]["id"], "xebia")
        self.assertEqual(context["route"]["dockerProfile"], "xebia")
        self.assertEqual(session.context_sha256, sha256_file(session.context_file))
        self.assertEqual(
            session.context_sha256,
            hashlib.sha256(session.context_file.read_bytes()).hexdigest(),
        )
        verified = verify_session(
            self.workflow_root, session.run_dir, session.context_sha256
        )
        self.assertEqual(verified, session)

    def test_explicit_private_data_root_keeps_state_outside_checkout(self) -> None:
        data_root = self.fixture / "data-root"
        data_root.mkdir(mode=0o700)
        session = create_session(
            self.workflow_root, self.launch_dir, self.config_path,
            data_root=data_root,
        )
        self.assertEqual(session.run_dir.parent, data_root / "state" / "sessions")
        self.assertFalse((self.workflow_root / "runtime" / "state").exists())
        self.assertEqual(
            verify_session(
                self.workflow_root, session.run_dir, session.context_sha256,
                data_root=data_root,
            ),
            session,
        )

    def test_context_binding_reuses_verified_bytes_without_requiring_empty_mcp(self) -> None:
        session = self.create()
        session.mcp_file.write_text(
            '{"mcpServers":{"future-strict-server":{}}}', encoding="utf-8"
        )
        session.mcp_file.chmod(0o600)

        binding = verify_context_binding(
            self.workflow_root,
            session.run_dir,
            session.context_file,
            session.context_sha256,
            session.run_id,
        )

        self.assertIsInstance(binding, ContextBinding)
        self.assertEqual(binding.workflow_root, self.workflow_root.resolve())
        self.assertEqual(binding.run_id, session.run_id)
        self.assertEqual(binding.run_dir, session.run_dir)
        self.assertEqual(binding.context_file, session.context_file)
        self.assertEqual(binding.context_sha256, session.context_sha256)
        self.assertEqual(binding.context, json.loads(session.context_file.read_bytes()))
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def test_context_binding_rejects_authority_field_mismatch(self) -> None:
        session = self.create()
        calls = (
            lambda: verify_context_binding(
                self.workflow_root,
                session.run_dir,
                session.context_file,
                session.context_sha256,
                session.run_id + ".other",
            ),
            lambda: verify_context_binding(
                self.workflow_root,
                session.run_dir,
                session.run_dir / "other.json",
                session.context_sha256,
                session.run_id,
            ),
            lambda: verify_context_binding(
                self.workflow_root,
                session.run_dir.with_name(session.run_id + ".other"),
                session.context_file,
                session.context_sha256,
                session.run_id,
            ),
        )
        for action in calls:
            with self.subTest(action=action):
                self.assert_rejected(action)

    def test_context_binding_rejects_tamper_and_symlink_substitution(self) -> None:
        session = self.create()
        original_bytes = session.context_file.read_bytes()
        session.context_file.write_bytes(original_bytes + b" ")
        session.context_file.chmod(0o600)
        self.assert_rejected(
            lambda: verify_context_binding(
                self.workflow_root,
                session.run_dir,
                session.context_file,
                session.context_sha256,
                session.run_id,
            )
        )

        session.context_file.unlink()
        replacement = session.run_dir / "replacement.json"
        replacement.write_bytes(original_bytes)
        replacement.chmod(0o600)
        session.context_file.symlink_to(replacement)
        self.assert_rejected(
            lambda: verify_context_binding(
                self.workflow_root,
                session.run_dir,
                session.context_file,
                session.context_sha256,
                session.run_id,
            )
        )

    def test_missing_palace_preserves_route_and_git_resolution(self) -> None:
        repository = self.launch_dir / "repository"
        repository.mkdir()
        self.write_config(self.home / "missing-palace")

        with mock.patch(
            "integrations.common.project_context._git_root",
            return_value=str(repository.resolve()),
        ):
            session = create_session(
                self.workflow_root, repository, self.config_path
            )
        context = json.loads(session.context_file.read_text())

        self.assertEqual(context["route"]["dockerProfile"], "xebia")
        self.assertEqual(context["route"]["memoryWing"], "xebia")
        self.assertFalse(context["route"]["memoryAvailable"])
        self.assertEqual(context["route"]["memoryFailureCode"], "palace_missing")
        self.assertIsNone(context["route"]["palacePathReal"])
        self.assertEqual(context["repoRootReal"], str(repository.resolve()))
        self.assertEqual(
            json.loads(session.mcp_file.read_text()),
            {
                "mcpServers": {
                    "docker": {
                        "command": "/opt/tools/docker",
                        "args": ["mcp", "gateway", "run", "--profile", "xebia"],
                    }
                }
            },
        )

    def test_graphify_is_added_only_for_a_repository_with_a_graph(self) -> None:
        repository = self.launch_dir / "repository"
        graph = repository / "graphify-out" / "graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_text("{}", encoding="utf-8")

        with mock.patch(
            "integrations.common.project_context._git_root",
            return_value=str(repository.resolve()),
        ):
            session = create_session(
                self.workflow_root, repository, self.config_path
            )

        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        self.assertEqual(
            servers["graphify"],
            {
                "command": "/opt/tools/graphify-mcp",
                "args": ["--graph", str(graph.resolve())],
            },
        )

    def test_unsafe_palace_preserves_mapped_route(self) -> None:
        self.palace.chmod(0o755)
        session = self.create()
        context = json.loads(session.context_file.read_text())

        self.assertEqual(context["route"]["dockerProfile"], "xebia")
        self.assertFalse(context["route"]["memoryAvailable"])
        self.assertEqual(
            context["route"]["memoryFailureCode"], "palace_permissions"
        )
        self.assertIsNone(context["route"]["palacePathReal"])

    def test_rejects_symlinked_workflow_root(self) -> None:
        link = self.fixture / "workflow-link"
        link.symlink_to(self.workflow_root, target_is_directory=True)
        self.assert_rejected(
            lambda: create_session(link, self.launch_dir, self.config_path)
        )

    def test_rejects_symlinked_runtime_component(self) -> None:
        shutil.rmtree(self.runtime)
        external = self.fixture / "external-runtime"
        external.mkdir()
        self.runtime.symlink_to(external, target_is_directory=True)
        self.assert_rejected(self.create)

    def test_rejects_symlinked_state_component(self) -> None:
        external = self.fixture / "external-state"
        external.mkdir(mode=0o700)
        (self.runtime / "state").symlink_to(external, target_is_directory=True)
        self.assert_rejected(self.create)

    def test_rejects_symlinked_sessions_component(self) -> None:
        state = self.runtime / "state"
        state.mkdir(mode=0o700)
        external = self.fixture / "external-sessions"
        external.mkdir(mode=0o700)
        (state / "sessions").symlink_to(external, target_is_directory=True)
        self.assert_rejected(self.create)

    def test_rejects_group_or_world_accessible_state(self) -> None:
        state = self.runtime / "state"
        state.mkdir(mode=0o750)
        self.assert_rejected(self.create)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o750)

    def test_rejects_state_owned_by_another_uid(self) -> None:
        state = self.runtime / "state"
        state.mkdir(mode=0o700)
        real_lstat = os.lstat

        def wrong_owner(path, *args, **kwargs):
            observed = real_lstat(path, *args, **kwargs)
            if Path(path) == state:
                values = list(observed)
                values[4] = observed.st_uid + 1
                return os.stat_result(values)
            return observed

        with mock.patch(
            "integrations.common.session_config.os.lstat", side_effect=wrong_owner
        ):
            self.assert_rejected(self.create)

    def test_verify_rejects_symlinked_run_directory(self) -> None:
        session = self.create()
        moved = session.run_dir.with_name(session.run_dir.name + ".moved")
        session.run_dir.rename(moved)
        session.run_dir.symlink_to(moved, target_is_directory=True)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def test_verify_rejects_non_direct_child_session(self) -> None:
        session = self.create()
        outsider = self.fixture / session.run_id
        shutil.copytree(session.run_dir, outsider)
        outsider.chmod(0o700)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, outsider, session.context_sha256
            )
        )

    def test_verify_rejects_symlinked_context_file(self) -> None:
        session = self.create()
        original = session.context_file.with_suffix(".original")
        session.context_file.rename(original)
        session.context_file.symlink_to(original)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def test_verify_rejects_symlinked_mcp_file(self) -> None:
        session = self.create()
        original = session.mcp_file.with_suffix(".original")
        session.mcp_file.rename(original)
        session.mcp_file.symlink_to(original)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def test_verify_rejects_valid_json_context_rewrite(self) -> None:
        session = self.create()
        context = json.loads(session.context_file.read_text())
        context["route"]["id"] = "complion"
        context["route"]["dockerProfile"] = "realtime"
        context["route"]["memoryWing"] = "complion"
        session.context_file.write_text(
            json.dumps(context, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        session.context_file.chmod(0o600)

        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def assert_read_swap_rejected(self, file_name: str) -> None:
        session = self.create()
        target = session.run_dir / file_name
        target_inode = os.lstat(target).st_ino
        replacement = session.run_dir / f"{file_name}.replacement"
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o600)
        real_read = os.read
        swapped = False

        def swap_path_during_read(file_descriptor: int, size: int) -> bytes:
            nonlocal swapped
            data = real_read(file_descriptor, size)
            if not swapped and os.fstat(file_descriptor).st_ino == target_inode:
                os.replace(replacement, target)
                swapped = True
            return data

        with mock.patch.object(
            session_config.os, "read", side_effect=swap_path_during_read
        ):
            self.assert_rejected(
                lambda: verify_session(
                    self.workflow_root, session.run_dir, session.context_sha256
                )
            )
        self.assertTrue(swapped)

    def test_verify_rejects_context_inode_swap_during_read(self) -> None:
        self.assert_read_swap_rejected("context.json")

    def test_verify_rejects_mcp_inode_swap_during_read(self) -> None:
        self.assert_read_swap_rejected("mcp.json")

    def test_verify_rejects_non_empty_foundation_mcp_config(self) -> None:
        session = self.create()
        session.mcp_file.write_text(
            '{"mcpServers":{"forbidden":{}}}', encoding="utf-8"
        )
        session.mcp_file.chmod(0o600)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root, session.run_dir, session.context_sha256
            )
        )

    def test_cli_create_has_bounded_schema_and_verify_is_silent(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
        create = subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.session_config",
                "create",
                "--workflow-root",
                str(self.workflow_root),
                "--launch-dir",
                str(self.launch_dir),
                "--config",
                str(self.config_path),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(create.stdout)
        self.assertEqual(
            set(payload),
            {"runId", "runDir", "contextFile", "contextSha256", "mcpFile"},
        )
        self.assertEqual(create.stderr, "")
        self.assertTrue(Path(payload["runDir"]).is_absolute())
        self.assertEqual(payload["contextSha256"], sha256_file(Path(payload["contextFile"])))

        verify = subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.session_config",
                "verify",
                "--workflow-root",
                str(self.workflow_root),
                "--run-dir",
                payload["runDir"],
                "--context-sha256",
                payload["contextSha256"],
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verify.stdout, "")
        self.assertEqual(verify.stderr, "")


if __name__ == "__main__":
    unittest.main()
