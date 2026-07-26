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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import integrations.common.session_config as session_config
from integrations.common.session_config import (
    ContextBinding,
    SessionError,
    create_resolved_session,
    create_session,
    sha256_file,
    verify_context_binding,
    verify_session,
)
from integrations.common.model_routing import EffectiveStack
from integrations.common.graph_manager import resolve_graph_target


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
        self.routing_path = self.workflow_root / "model-routing.json"
        self.models_path = self.fixture / "models.json"
        self.plugin_source = REPOSITORY_ROOT / "controller" / "plugin"
        self.first_catalog = ["controller/main"] + [
            f"balanced/{role}" for role in session_config.ROLES
        ]
        self.second_catalog = ["controller/alternate"] + [
            f"alternate/{role}" for role in session_config.ROLES
        ]
        self.routing_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "defaultStack": "balanced",
                    "stacks": {
                        "balanced": {
                            "controller": "controller/main",
                            "agents": {
                                role: [f"balanced/{role}"]
                                for role in session_config.ROLES
                            },
                        },
                        "alternate": {
                            "controller": "controller/alternate",
                            "agents": {
                                role: [f"alternate/{role}"]
                                for role in session_config.ROLES
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.write_catalog(self.first_catalog)
        self.write_config(self.palace)

    def write_config(self, palace: Path) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "contexts": [
                        {
                            "root": str(self.xebia),
                            "dockerProfile": "xebia",
                            "modelStack": None,
                            "memoryPalace": str(palace),
                            "memoryWing": "xebia",
                        },
                        {
                            "root": str(self.complion),
                            "dockerProfile": "realtime",
                            "modelStack": None,
                            "memoryPalace": str(self.complion_palace),
                            "memoryWing": "complion",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def write_catalog(self, models: list[str]) -> None:
        self.models_path.write_text(
            json.dumps(
                {
                    "object": "list",
                    "data": [{"id": model} for model in models],
                }
            ),
            encoding="utf-8",
        )

    def routing_options(self) -> dict[str, Path]:
        return {
            "routing_path": self.routing_path,
            "models_path": self.models_path,
            "plugin_source": self.plugin_source,
        }

    def create(
        self,
        *,
        stack: str | None = None,
        models: list[str] | None = None,
    ):
        if stack is not None:
            document = json.loads(self.config_path.read_text(encoding="utf-8"))
            document["contexts"][0]["modelStack"] = stack
            self.config_path.write_text(json.dumps(document), encoding="utf-8")
        if models is not None:
            self.write_catalog(models)
        return create_session(
            self.workflow_root,
            self.launch_dir,
            self.config_path,
            **self.routing_options(),
        )

    def init_repository(
        self,
        repository: Path | None = None,
        *,
        content: str = "initial\n",
    ) -> Path:
        repository = self.launch_dir if repository is None else repository
        repository.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "-C", str(repository), "init", "-q"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "config",
                "user.email",
                "tests@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Session tests"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "remote",
                "add",
                "origin",
                "https://github.com/example/session-graph.git",
            ],
            check=True,
        )
        (repository / "tracked.txt").write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "fixture"],
            check=True,
        )
        return repository.resolve(strict=True)

    def install_leanctx(self, data_root: Path | None = None) -> Path:
        data_root = self.runtime if data_root is None else data_root
        binary_dir = data_root / "bin"
        binary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary_dir.chmod(0o700)
        binary = binary_dir / "lean-ctx"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary.resolve(strict=True)

    def create_central_graph(
        self,
        repository: Path,
        *,
        revision: str | None = None,
        identity: str | None = None,
        state_id: str | None = None,
        graph_document: dict | None = None,
    ):
        self.runtime.chmod(0o700)
        target = resolve_graph_target(repository, self.runtime)
        target.output_dir.mkdir(parents=True, mode=0o700)
        target.output_dir.chmod(0o700)
        document = graph_document or {
            "nodes": [{"id": "tracked", "source_file": "tracked.txt"}]
        }
        target.graph_file.write_text(
            json.dumps(
                {**document, "built_at_commit": target.revision}
            ),
            encoding="utf-8",
        )
        target.graph_file.chmod(0o600)
        target.metadata_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "repository_identity": identity or target.identity.key,
                    "revision": revision or target.revision,
                    "state_id": state_id or target.state_id,
                    "kind": target.kind,
                    "built_at_commit": revision or target.revision,
                    **(
                        {"checkout_path": str(target.repository)}
                        if target.kind == "working"
                        else {}
                    ),
                }
            ),
            encoding="utf-8",
        )
        target.metadata_file.chmod(0o600)
        return target

    def assert_rejected(self, action) -> None:
        with self.assertRaises(SessionError):
            action()

    def test_component_creation_accepts_valid_concurrent_mkdir_winner(
        self,
    ) -> None:
        parent = self.fixture / "race-parent"
        parent.mkdir(mode=0o700)
        child = parent / "state"
        real_lstat = session_config.os.lstat
        real_mkdir = session_config.os.mkdir
        first_lookup = True

        def miss_once(path):
            nonlocal first_lookup
            if Path(path) == child and first_lookup:
                first_lookup = False
                raise FileNotFoundError
            return real_lstat(path)

        def concurrent_winner(path, mode):
            real_mkdir(path, mode)
            raise FileExistsError

        with (
            mock.patch.object(
                session_config.os, "lstat", side_effect=miss_once
            ),
            mock.patch.object(
                session_config.os,
                "mkdir",
                side_effect=concurrent_winner,
            ),
        ):
            resolved = session_config.require_owned_component(
                parent, "state", private=True, create=True
            )

        self.assertEqual(resolved, child)
        self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o700)

    def test_create_session_writes_private_project_mcp_state(self) -> None:
        session = self.create()

        self.assertEqual(stat.S_IMODE(session.run_dir.stat().st_mode), 0o700)
        completion = session.run_dir / ".complete"
        self.assertTrue(completion.is_file())
        self.assertFalse(completion.is_symlink())
        self.assertEqual(stat.S_IMODE(completion.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(session.context_file.stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(session.mcp_file.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(session.effective_models_file.stat().st_mode), 0o600
        )
        self.assertEqual(stat.S_IMODE(session.plugin_dir.stat().st_mode), 0o700)
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
            self.workflow_root,
            session.run_dir,
            session.context_sha256,
            session.effective_models_sha256,
        )
        self.assertEqual(verified, session)

    def test_create_session_removes_incomplete_run_after_failure(self) -> None:
        real_atomic_json = session_config.atomic_json

        def fail_mcp(path, payload, mode=0o600):
            if Path(path).name == "mcp.json":
                raise SessionError("injected MCP publication failure")
            return real_atomic_json(path, payload, mode)

        with mock.patch.object(
            session_config, "atomic_json", side_effect=fail_mcp
        ):
            with self.assertRaisesRegex(SessionError, "injected MCP"):
                self.create()

        sessions = self.runtime / "state" / "sessions"
        self.assertEqual(list(sessions.glob("run.*")), [])

    def test_create_session_removes_run_when_initial_fsync_fails(self) -> None:
        real_fsync_directory = session_config._fsync_directory
        failed = False

        def fail_session_publication(directory):
            nonlocal failed
            if Path(directory).name == "sessions" and not failed:
                failed = True
                raise OSError("injected session directory fsync failure")
            return real_fsync_directory(directory)

        with mock.patch.object(
            session_config,
            "_fsync_directory",
            side_effect=fail_session_publication,
        ):
            with self.assertRaisesRegex(
                SessionError, "session directory could not be created"
            ):
                self.create()

        sessions = self.runtime / "state" / "sessions"
        self.assertEqual(list(sessions.glob("run.*")), [])

    def test_two_sessions_keep_independent_plugins(self) -> None:
        first = self.create(stack="balanced", models=self.first_catalog)
        second = self.create(stack="alternate", models=self.second_catalog)

        self.assertNotEqual(first.plugin_dir, second.plugin_dir)
        self.assertNotEqual(first.controller_model, second.controller_model)
        self.assertTrue(first.plugin_dir.is_relative_to(first.run_dir))
        self.assertTrue(second.plugin_dir.is_relative_to(second.run_dir))

    def test_resolved_session_uses_routed_models_and_existing_hardening(self) -> None:
        data_root = self.fixture / "resolved-data"
        data_root.mkdir(mode=0o700)
        context = session_config.resolve_context(
            session_config.load_config(self.config_path), self.launch_dir
        )
        effective = EffectiveStack(
            "balanced",
            "oc-r-0000000000000001/gpt-5.6-sol",
            {
                role: (f"oc-r-{index + 2:016x}/gpt-5.6-terra",)
                for index, role in enumerate(session_config.ROLES)
            },
            {
                role: f"oc-r-{index + 2:016x}/gpt-5.6-terra"
                for index, role in enumerate(session_config.ROLES)
            },
        )

        session = create_resolved_session(
            self.workflow_root,
            data_root=data_root,
            context=context,
            effective=effective,
            plugin_source=self.plugin_source,
        )

        self.assertEqual(session.controller_model, effective.controller)
        self.assertEqual(session.run_dir.parent, data_root / "state" / "sessions")
        self.assertEqual(
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
                data_root=data_root,
            ),
            session,
        )
        for role in session_config.ROLES:
            agent = session.plugin_dir / "agents" / f"{role}.md"
            self.assertIn(
                f"model: {effective.agents[role]}",
                agent.read_text(encoding="utf-8"),
            )

    def test_session_rejects_modified_effective_mapping(self) -> None:
        session = self.create()
        session.effective_models_file.write_text("{}", encoding="utf-8")
        session.effective_models_file.chmod(0o600)

        with self.assertRaises(SessionError):
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

    def test_session_verification_uses_immutable_effective_mapping(self) -> None:
        session = self.create()
        self.write_catalog(self.second_catalog)

        verified = verify_session(
            self.workflow_root,
            session.run_dir,
            session.context_sha256,
            session.effective_models_sha256,
        )

        self.assertEqual(verified.controller_model, "controller/main")

    def test_session_rejects_modified_or_unsafe_runtime_plugin(self) -> None:
        session = self.create()
        agent = (
            session.plugin_dir
            / "agents"
            / f"{session_config.ROLES[0]}.md"
        )
        original = agent.read_text(encoding="utf-8")
        agent.write_text(original.replace("balanced/", "alternate/"), encoding="utf-8")
        agent.chmod(0o600)
        with self.assertRaises(SessionError):
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

        agent.write_text(original, encoding="utf-8")
        agent.chmod(0o640)
        with self.assertRaises(SessionError):
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

    def test_session_rejects_runtime_plugin_symlink(self) -> None:
        session = self.create()
        workflow = session.plugin_dir / "workflows" / "review.js"
        original = workflow.with_suffix(".original")
        workflow.rename(original)
        workflow.symlink_to(original)

        with self.assertRaises(SessionError):
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

    def test_session_rejects_empty_plugin_directory_symlink_swap(self) -> None:
        session = self.create()
        empty = session.plugin_dir / "empty"
        displaced = session.plugin_dir / "empty.original"
        empty.mkdir(mode=0o700)
        empty_inode = os.lstat(empty).st_ino
        real_scandir = os.scandir
        swapped = False

        def swap_before_enumeration(target):
            nonlocal swapped
            targets_empty = (
                isinstance(target, int)
                and os.fstat(target).st_ino == empty_inode
            ) or (not isinstance(target, int) and Path(target) == empty)
            if not swapped and targets_empty:
                empty.rename(displaced)
                empty.symlink_to(displaced, target_is_directory=True)
                swapped = True
            return real_scandir(target)

        with mock.patch.object(
            session_config.os,
            "scandir",
            side_effect=swap_before_enumeration,
        ):
            with self.assertRaises(SessionError):
                verify_session(
                    self.workflow_root,
                    session.run_dir,
                    session.context_sha256,
                    session.effective_models_sha256,
                )
        self.assertTrue(swapped)

    def test_session_accepts_executable_agent_normalized_to_private_mode(
        self,
    ) -> None:
        plugin_source = self.fixture / "executable-agent-plugin"
        shutil.copytree(self.plugin_source, plugin_source)
        role = session_config.ROLES[0]
        (plugin_source / "agents" / f"{role}.md").chmod(0o755)

        session = create_session(
            self.workflow_root,
            self.launch_dir,
            self.config_path,
            routing_path=self.routing_path,
            models_path=self.models_path,
            plugin_source=plugin_source,
        )

        self.assertEqual(
            stat.S_IMODE(
                (session.plugin_dir / "agents" / f"{role}.md").stat().st_mode
            ),
            0o700,
        )

    def test_context_without_docker_profile_omits_docker_mcp(self) -> None:
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        document["contexts"][0]["dockerProfile"] = None
        self.config_path.write_text(json.dumps(document), encoding="utf-8")

        session = self.create()
        context = json.loads(session.context_file.read_text(encoding="utf-8"))
        servers = json.loads(session.mcp_file.read_text(encoding="utf-8"))["mcpServers"]
        self.assertIsNone(context["route"]["dockerProfile"])
        self.assertNotIn("docker", servers)
        self.assertIn("mempalace", servers)

    def test_explicit_private_data_root_keeps_state_outside_checkout(self) -> None:
        data_root = self.fixture / "data-root"
        data_root.mkdir(mode=0o700)
        session = create_session(
            self.workflow_root, self.launch_dir, self.config_path,
            data_root=data_root,
            **self.routing_options(),
        )
        self.assertEqual(session.run_dir.parent, data_root / "state" / "sessions")
        self.assertFalse((self.workflow_root / "runtime" / "state").exists())
        self.assertEqual(
            verify_session(
                self.workflow_root, session.run_dir, session.context_sha256,
                session.effective_models_sha256,
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
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
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
                self.workflow_root,
                repository,
                self.config_path,
                **self.routing_options(),
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

    def test_session_uses_matching_central_graph(self) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)

        session = self.create()
        context = json.loads(session.context_file.read_text())
        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        snapshot = Path(context["graph"]["graphFile"])

        self.assertEqual(context["graph"]["identity"], graph.identity.key)
        self.assertEqual(
            context["graph"]["centralGraphFile"], str(graph.graph_file)
        )
        self.assertEqual(snapshot.parent, session.run_dir)
        self.assertNotEqual(snapshot, graph.graph_file)
        self.assertEqual(snapshot.read_bytes(), graph.graph_file.read_bytes())
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(
            servers["graphify"],
            {
                "command": "/opt/tools/graphify-mcp",
                "args": ["--graph", str(snapshot)],
            },
        )

    def test_git_session_gets_private_bounded_leanctx_mcp(self) -> None:
        repository = self.init_repository()
        binary = self.install_leanctx()

        session = self.create()
        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        leanctx_dir = session.run_dir / "leanctx"
        config = leanctx_dir / "config.toml"

        self.assertIn("leanctx", servers)
        self.assertEqual(
            servers["leanctx"],
            {
                "command": str(binary),
                "args": [],
                "env": {
                    "LEAN_CTX_ALLOW_REROOT": "false",
                    "LEAN_CTX_AUTONOMY": "false",
                    "LEAN_CTX_BYPASS_HINTS": "off",
                    "LEAN_CTX_CACHE_DIR": str(leanctx_dir),
                    "LEAN_CTX_CONFIG_DIR": str(leanctx_dir),
                    "LEAN_CTX_DATA_DIR": str(leanctx_dir),
                    "LEAN_CTX_FULL_TOOLS": "0",
                    "LEAN_CTX_HEADLESS": "1",
                    "LEAN_CTX_MINIMAL": "1",
                    "LEAN_CTX_PROJECT_ROOT": str(repository),
                    "LEAN_CTX_STATE_DIR": str(leanctx_dir),
                },
            },
        )
        self.assertEqual(stat.S_IMODE(leanctx_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(
            config.read_text(encoding="utf-8"),
            """compression_level = "lite"
minimal_overhead = true
tools_enabled = ["ctx_read", "ctx_delta", "ctx_search", "ctx_glob", "ctx_tree", "ctx_outline", "ctx_explore", "ctx_expand", "ctx_patch", "ctx_shell"]
disabled_tools = ["ctx_call"]
auto_capture = false
buddy_enabled = false
enable_wakeup_ctx = false
journal_enabled = false
max_index_threads = 2
no_degrade = true
prefer_native_editor = false
proxy_enabled = false
rules_injection = "off"
shadow_mode = false
shell_activation = "off"
shell_hook_disabled = true
update_check_disabled = true
""",
        )

    def test_leanctx_config_tampering_invalidates_the_session(self) -> None:
        self.init_repository()
        self.install_leanctx()
        session = self.create()
        config = session.run_dir / "leanctx" / "config.toml"
        if not config.is_file():
            self.fail("LeanCTX config was not materialized")
        config.write_text("tools_enabled = []\n", encoding="utf-8")
        config.chmod(0o600)

        with self.assertRaisesRegex(
            SessionError, "LeanCTX configuration does not match"
        ):
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

    def test_two_sessions_do_not_share_leanctx_state(self) -> None:
        self.init_repository()
        self.install_leanctx()

        first = self.create()
        second = self.create()
        first_servers = json.loads(first.mcp_file.read_text())["mcpServers"]
        second_servers = json.loads(second.mcp_file.read_text())["mcpServers"]
        self.assertIn("leanctx", first_servers)
        self.assertIn("leanctx", second_servers)
        first_server = first_servers["leanctx"]
        second_server = second_servers["leanctx"]

        self.assertNotEqual(
            first_server["env"]["LEAN_CTX_DATA_DIR"],
            second_server["env"]["LEAN_CTX_DATA_DIR"],
        )
        self.assertEqual(
            Path(first_server["env"]["LEAN_CTX_DATA_DIR"]).parent,
            first.run_dir,
        )
        self.assertEqual(
            Path(second_server["env"]["LEAN_CTX_DATA_DIR"]).parent,
            second.run_dir,
        )

    def test_existing_session_snapshot_survives_central_replacement(self) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        session = self.create()
        context = json.loads(session.context_file.read_text())
        snapshot = Path(context["graph"]["graphFile"])
        original = snapshot.read_bytes()

        graph.graph_file.write_text(
            json.dumps(
                {
                    "built_at_commit": graph.revision,
                    "nodes": [
                        {
                            "id": "replacement",
                            "source_file": "tracked.txt",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        graph.graph_file.chmod(0o600)

        self.assertEqual(snapshot.read_bytes(), original)
        self.assertNotEqual(snapshot.read_bytes(), graph.graph_file.read_bytes())
        self.assertEqual(
            json.loads(session.mcp_file.read_text())["mcpServers"]["graphify"][
                "args"
            ],
            ["--graph", str(snapshot)],
        )
        self.assertEqual(
            verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            ),
            session,
        )

    def test_verify_rejects_tampered_graph_snapshot(self) -> None:
        repository = self.init_repository()
        self.create_central_graph(repository)
        session = self.create()
        context = json.loads(session.context_file.read_text())
        snapshot = Path(context["graph"]["graphFile"])
        snapshot.write_text('{"nodes":[]}', encoding="utf-8")
        snapshot.chmod(0o600)

        with mock.patch.object(
            session_config.shutil, "which", return_value=None
        ):
            self.assert_rejected(
                lambda: verify_session(
                    self.workflow_root,
                    session.run_dir,
                    session.context_sha256,
                    session.effective_models_sha256,
                )
            )

    def test_verify_streams_large_graph_snapshot_in_bounded_chunks(self) -> None:
        repository = self.init_repository()
        self.create_central_graph(
            repository,
            graph_document={
                "nodes": [
                    {
                        "id": "large",
                        "source_file": "tracked.txt",
                        "payload": "x" * (4 * 1024 * 1024),
                    }
                ]
            },
        )
        session = self.create()
        real_read_owned_file = session_config._read_owned_file
        real_os_read = session_config.os.read
        read_sizes: list[int] = []

        def reject_graph_accumulation(parent, file_name, expected_mode=0o600):
            if file_name == "graph.json":
                raise AssertionError("graph snapshot must be streamed")
            return real_read_owned_file(parent, file_name, expected_mode)

        def guard_read_size(descriptor, size):
            read_sizes.append(size)
            if size > 64 * 1024:
                raise AssertionError("graph snapshot read is unbounded")
            return real_os_read(descriptor, size)

        with (
            mock.patch.object(
                session_config,
                "_read_owned_file",
                side_effect=reject_graph_accumulation,
            ),
            mock.patch.object(
                session_config.os,
                "read",
                side_effect=guard_read_size,
            ),
        ):
            verified = verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )

        self.assertEqual(verified, session)
        self.assertTrue(read_sizes)
        self.assertLessEqual(max(read_sizes), 64 * 1024)

    def test_verify_rejects_symlinked_graph_snapshot(self) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        session = self.create()
        context = json.loads(session.context_file.read_text())
        snapshot = Path(context["graph"]["graphFile"])
        snapshot.unlink()
        snapshot.symlink_to(graph.graph_file)

        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )
        )

    def test_verify_rejects_public_graph_snapshot(self) -> None:
        repository = self.init_repository()
        self.create_central_graph(repository)
        session = self.create()
        context = json.loads(session.context_file.read_text())
        snapshot = Path(context["graph"]["graphFile"])
        snapshot.chmod(0o644)

        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )
        )

    def test_stale_graph_is_omitted_without_blocking_session(self) -> None:
        repository = self.init_repository()
        self.create_central_graph(repository, revision="0" * 40)

        session = self.create()
        servers = json.loads(session.mcp_file.read_text())["mcpServers"]

        self.assertNotIn("graphify", servers)

    def test_graph_digest_mismatch_rematerializes_or_omits(self) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        real_resolver = session_config.resolve_available_graph
        calls = 0

        def replace_after_binding(repository_path, data_root):
            nonlocal calls
            binding = real_resolver(repository_path, data_root)
            calls += 1
            if calls == 1:
                graph.graph_file.write_text(
                    json.dumps(
                        {
                            "built_at_commit": graph.revision,
                            "nodes": [
                                {
                                    "id": "replacement",
                                    "source_file": "tracked.txt",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                graph.graph_file.chmod(0o600)
            return binding

        with mock.patch.object(
            session_config,
            "resolve_available_graph",
            side_effect=replace_after_binding,
        ):
            session = self.create()

        context = json.loads(session.context_file.read_text())
        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        graph_server = servers.get("graphify")
        if graph_server is None:
            self.assertNotIn("graph", context)
        else:
            snapshot = Path(context["graph"]["graphFile"])
            self.assertEqual(graph_server["args"], ["--graph", str(snapshot)])
            self.assertEqual(
                context["graph"]["sha256"],
                hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                snapshot.read_bytes(), graph.graph_file.read_bytes()
            )

    def test_unsafe_central_graph_path_is_omitted(self) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        outside = self.fixture / "outside-graph.json"
        outside.write_text(
            graph.graph_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        graph.graph_file.unlink()
        graph.graph_file.symlink_to(outside)

        session = self.create()

        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        self.assertNotIn("graphify", servers)

    def test_wrong_repository_identity_is_omitted(self) -> None:
        repository = self.init_repository()
        self.create_central_graph(repository, identity="github.com/other/repository")

        session = self.create()

        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        self.assertNotIn("graphify", servers)

    def test_dirty_state_mismatch_is_omitted(self) -> None:
        repository = self.init_repository()
        (repository / "tracked.txt").write_text("dirty-a\n", encoding="utf-8")
        self.create_central_graph(repository)
        first_state = resolve_graph_target(repository, self.runtime).state_id
        (repository / "tracked.txt").write_text("dirty-b\n", encoding="utf-8")
        second_state = resolve_graph_target(repository, self.runtime).state_id

        session = self.create()

        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        self.assertNotEqual(first_state, second_state)
        self.assertNotIn("graphify", servers)

    def test_repeated_graph_replacement_falls_back_to_consistent_omission(
        self,
    ) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        real_resolver = session_config.resolve_available_graph
        calls = 0
        replacements = 0

        def replace_during_copy(repository_path, data_root):
            nonlocal calls, replacements
            binding = real_resolver(repository_path, data_root)
            calls += 1
            if calls in {2, 4}:
                replacements += 1
                graph.graph_file.write_text(
                    json.dumps(
                        {
                            "built_at_commit": graph.revision,
                            "nodes": [
                                {
                                    "id": f"replacement-{replacements}",
                                    "source_file": "tracked.txt",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                graph.graph_file.chmod(0o600)
            return binding

        with mock.patch.object(
            session_config,
            "resolve_available_graph",
            side_effect=replace_during_copy,
        ):
            session = self.create()

        context = json.loads(session.context_file.read_text())
        servers = json.loads(session.mcp_file.read_text())["mcpServers"]
        self.assertNotIn("graph", context)
        self.assertNotIn("graphify", servers)
        self.assertEqual(replacements, 2)
        self.assertEqual(
            [path for path in session.run_dir.parent.glob("run.*")],
            [session.run_dir],
        )

    def test_new_physical_session_binds_replacement_without_mutating_existing_mcp(
        self,
    ) -> None:
        repository = self.init_repository()
        graph = self.create_central_graph(repository)
        first = self.create()
        first_mcp = first.mcp_file.read_bytes()
        first_context = json.loads(first.context_file.read_text())
        first_snapshot = Path(first_context["graph"]["graphFile"])
        first_snapshot_bytes = first_snapshot.read_bytes()
        graph.graph_file.write_text(
            json.dumps(
                {
                    "built_at_commit": graph.revision,
                    "nodes": [
                        {
                            "id": "replacement",
                            "source_file": "tracked.txt",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        graph.graph_file.chmod(0o600)

        resumed = self.create()
        resumed_context = json.loads(resumed.context_file.read_text())
        resumed_snapshot = Path(resumed_context["graph"]["graphFile"])

        self.assertEqual(first.mcp_file.read_bytes(), first_mcp)
        self.assertEqual(first_snapshot.read_bytes(), first_snapshot_bytes)
        self.assertNotEqual(first_snapshot, resumed_snapshot)
        self.assertNotEqual(first_snapshot.read_bytes(), resumed_snapshot.read_bytes())
        self.assertEqual(resumed_snapshot.read_bytes(), graph.graph_file.read_bytes())
        self.assertNotEqual(
            first_context["graph"]["sha256"],
            resumed_context["graph"]["sha256"],
        )
        self.assertIn(
            "graphify",
            json.loads(resumed.mcp_file.read_text())["mcpServers"],
        )

    def test_concurrent_sessions_keep_repository_graph_bindings_isolated(
        self,
    ) -> None:
        first_repository = self.init_repository(
            self.launch_dir / "first", content="first\n"
        )
        second_repository = self.init_repository(
            self.launch_dir / "second", content="second\n"
        )
        first_graph = self.create_central_graph(first_repository)
        second_graph = self.create_central_graph(second_repository)

        def create_for(repository: Path):
            return create_session(
                self.workflow_root,
                repository,
                self.config_path,
                **self.routing_options(),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_session, second_session = executor.map(
                create_for, (first_repository, second_repository)
            )

        first_context = json.loads(first_session.context_file.read_text())
        second_context = json.loads(second_session.context_file.read_text())
        self.assertEqual(first_context["graph"]["stateId"], first_graph.state_id)
        self.assertEqual(second_context["graph"]["stateId"], second_graph.state_id)
        self.assertNotEqual(
            first_context["graph"]["revision"],
            second_context["graph"]["revision"],
        )
        first_snapshot = Path(first_context["graph"]["graphFile"])
        second_snapshot = Path(second_context["graph"]["graphFile"])
        self.assertEqual(first_snapshot.parent, first_session.run_dir)
        self.assertEqual(second_snapshot.parent, second_session.run_dir)
        self.assertNotEqual(first_snapshot, second_snapshot)
        self.assertEqual(first_snapshot.read_bytes(), first_graph.graph_file.read_bytes())
        self.assertEqual(
            second_snapshot.read_bytes(), second_graph.graph_file.read_bytes()
        )
        self.assertEqual(
            json.loads(first_session.mcp_file.read_text())["mcpServers"]["graphify"][
                "args"
            ],
            ["--graph", str(first_snapshot)],
        )
        self.assertEqual(
            json.loads(second_session.mcp_file.read_text())["mcpServers"][
                "graphify"
            ]["args"],
            ["--graph", str(second_snapshot)],
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
            lambda: create_session(
                link,
                self.launch_dir,
                self.config_path,
                **self.routing_options(),
            )
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
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )
        )

    def test_verify_rejects_non_direct_child_session(self) -> None:
        session = self.create()
        outsider = self.fixture / session.run_id
        shutil.copytree(session.run_dir, outsider)
        outsider.chmod(0o700)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                outsider,
                session.context_sha256,
                session.effective_models_sha256,
            )
        )

    def test_verify_rejects_symlinked_context_file(self) -> None:
        session = self.create()
        original = session.context_file.with_suffix(".original")
        session.context_file.rename(original)
        session.context_file.symlink_to(original)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
            )
        )

    def test_verify_rejects_symlinked_mcp_file(self) -> None:
        session = self.create()
        original = session.mcp_file.with_suffix(".original")
        session.mcp_file.rename(original)
        session.mcp_file.symlink_to(original)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
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
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
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
                    self.workflow_root,
                    session.run_dir,
                    session.context_sha256,
                    session.effective_models_sha256,
                )
            )
        self.assertTrue(swapped)

    def test_verify_rejects_context_inode_swap_during_read(self) -> None:
        self.assert_read_swap_rejected("context.json")

    def test_verify_rejects_mcp_inode_swap_during_read(self) -> None:
        self.assert_read_swap_rejected("mcp.json")

    def test_verify_rejects_effective_mapping_in_place_mutation_during_read(
        self,
    ) -> None:
        session = self.create()
        target = session.effective_models_file
        target_inode = os.lstat(target).st_ino
        real_read = os.read
        mutated = False

        def mutate_file_during_read(
            file_descriptor: int, size: int
        ) -> bytes:
            nonlocal mutated
            data = real_read(file_descriptor, size)
            if (
                not mutated
                and os.fstat(file_descriptor).st_ino == target_inode
            ):
                target.write_text("{}", encoding="utf-8")
                target.chmod(0o600)
                mutated = True
            return data

        with mock.patch.object(
            session_config.os,
            "read",
            side_effect=mutate_file_during_read,
        ):
            with self.assertRaises(SessionError):
                verify_session(
                    self.workflow_root,
                    session.run_dir,
                    session.context_sha256,
                    session.effective_models_sha256,
                )
        self.assertTrue(mutated)

    def test_verify_rejects_non_empty_foundation_mcp_config(self) -> None:
        session = self.create()
        session.mcp_file.write_text(
            '{"mcpServers":{"forbidden":{}}}', encoding="utf-8"
        )
        session.mcp_file.chmod(0o600)
        self.assert_rejected(
            lambda: verify_session(
                self.workflow_root,
                session.run_dir,
                session.context_sha256,
                session.effective_models_sha256,
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
                "--routing-config",
                str(self.routing_path),
                "--models-file",
                str(self.models_path),
                "--plugin-source",
                str(self.plugin_source),
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
            {
                "runId",
                "runDir",
                "contextFile",
                "contextSha256",
                "mcpFile",
                "effectiveModelsFile",
                "effectiveModelsSha256",
                "pluginDir",
                "controllerModel",
            },
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
                "--effective-models-sha256",
                payload["effectiveModelsSha256"],
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
