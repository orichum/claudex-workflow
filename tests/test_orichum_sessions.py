#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import integrations.common.orichum_cli as orichum_cli
import integrations.common.session_config as session_config
from integrations.common.model_routing import ROLES
from integrations.common.account_registry import Account
from integrations.common.graph_manager import resolve_graph_target
from integrations.common.orichum_config import ResolvedConfig
from integrations.common.orichum_sessions import (
    LogicalSessionError,
    RouteBinding,
    create_logical_session,
    list_logical_sessions,
    load_logical_session,
    resolve_session_plan,
)
from integrations.common.route_selection import Route
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_definition import normalize_model_stacks
from integrations.common.project_context import (
    ContextError,
    resolve_control_plane_context,
)


class OrichumSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name).resolve() / "state"
        self.state.mkdir(mode=0o700)

    def binding(
        self,
        logical_model: str,
        family: str,
        ordinal: int,
    ) -> RouteBinding:
        def route(index: int) -> Route:
            suffix = f"{ordinal:08x}{index:08x}"
            return Route(
                account_id=f"oc-a-{suffix}",
                provider="anthropic" if family == "claude" else "openai",
                family=family,
                logical_model=logical_model,
                upstream_model=f"oc-r-{suffix}/{logical_model}",
                claudex_profile=f"ocp-{suffix}",
                priority=100 - index,
                pool="work",
            )

        return RouteBinding(primary=route(0), fallbacks=(route(1),))

    def create(self, ordinal: int = 1):
        controller = self.binding("gpt-5.6-sol", "gpt", ordinal)
        agents = {
            role: self.binding(
                "claude-sonnet-5" if role == "correctness-critic"
                else "gpt-5.6-terra",
                "claude" if role == "correctness-critic" else "gpt",
                ordinal + index + 1,
            )
            for index, role in enumerate(ROLES)
        }
        return create_logical_session(
            self.state,
            project_root=Path("/work/project"),
            stack="balanced",
            controller=controller,
            agents=agents,
        )

    def test_create_load_and_list_preserve_private_immutable_binding(self) -> None:
        session = self.create()

        self.assertTrue(session.id.startswith("oc-s-"))
        self.assertRegex(
            session.claude_session_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        directory = self.state / "logical-sessions" / session.id
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((directory / "binding.json").stat().st_mode), 0o600
        )
        self.assertEqual(load_logical_session(self.state, session.id), session)
        self.assertEqual(list_logical_sessions(self.state), (session,))
        self.assertIsNone(session.parent_id)
        self.assertEqual(session.controller.primary.family, "gpt")
        self.assertEqual(
            set(session.agents),
            set(ROLES),
        )
        with self.assertRaises(TypeError):
            session.agents[ROLES[0]] = session.controller

    def test_parent_is_recorded_without_reusing_claude_session_uuid(self) -> None:
        parent = self.create(1)
        child = create_logical_session(
            self.state,
            project_root=parent.project_root,
            stack=parent.stack,
            controller=parent.controller,
            agents=parent.agents,
            parent_id=parent.id,
        )

        self.assertEqual(child.parent_id, parent.id)
        self.assertNotEqual(child.id, parent.id)
        self.assertNotEqual(child.claude_session_id, parent.claude_session_id)

    def test_concurrent_creates_are_distinct_and_complete(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            sessions = tuple(executor.map(self.create, range(1, 17)))

        self.assertEqual(len({session.id for session in sessions}), 16)
        self.assertEqual(
            len({session.claude_session_id for session in sessions}), 16
        )
        self.assertEqual(len(list_logical_sessions(self.state)), 16)

    def test_rejects_tamper_symlink_permissions_and_unknown_session(self) -> None:
        session = self.create()
        directory = self.state / "logical-sessions" / session.id
        binding = directory / "binding.json"

        binding.chmod(0o644)
        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, session.id)
        binding.chmod(0o600)

        original = binding.read_bytes()
        binding.unlink()
        outside = self.state / "outside.json"
        outside.write_bytes(original)
        outside.chmod(0o600)
        binding.symlink_to(outside)
        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, session.id)

        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, "../escape")

    def test_rejects_mixed_family_fallback_and_secret_shaped_route_fields(self) -> None:
        primary = self.binding("gpt-5.6-sol", "gpt", 1).primary
        wrong_family = self.binding("claude-sonnet-5", "claude", 2).primary
        with self.assertRaises(LogicalSessionError):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(primary, (wrong_family,)),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 3)
                    for index, role in enumerate(ROLES)
                },
            )

        extra_fallback = self.binding("gpt-5.6-sol", "gpt", 3).primary
        with self.assertRaisesRegex(LogicalSessionError, "at most one"):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(
                    primary,
                    (
                        self.binding("gpt-5.6-sol", "gpt", 2).primary,
                        extra_fallback,
                    ),
                ),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 20)
                    for index, role in enumerate(ROLES)
                },
            )

        unsafe = Route(
            **{
                **primary.__dict__,
                "upstream_model": "https://token@example.com/model",
            }
        )
        with self.assertRaises(LogicalSessionError):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(unsafe, ()),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 10)
                    for index, role in enumerate(ROLES)
                },
            )

    def test_rejects_non_v4_uuid_parent_project_change_and_unknown_entries(self) -> None:
        session = self.create()
        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        document["claudeSessionId"] = "00000000-0000-1000-8000-000000000000"
        binding.write_text(json.dumps(document), encoding="utf-8")
        binding.chmod(0o600)
        with self.assertRaisesRegex(LogicalSessionError, "UUID v4"):
            load_logical_session(self.state, session.id)

        parent = self.create(50)
        with self.assertRaisesRegex(LogicalSessionError, "parent project"):
            create_logical_session(
                self.state,
                project_root=Path("/work/other"),
                stack=parent.stack,
                controller=parent.controller,
                agents=parent.agents,
                parent_id=parent.id,
            )

        unexpected = self.state / "logical-sessions" / ".temporary"
        unexpected.mkdir(mode=0o700)
        with self.assertRaisesRegex(LogicalSessionError, "unexpected entry"):
            list_logical_sessions(self.state)

    def test_control_plane_context_preserves_account_pools(self) -> None:
        home = Path(self.temporary.name).resolve() / "home"
        project = home / "work" / "repo"
        palace = home / ".mempalace" / "work"
        project.mkdir(parents=True)
        palace.mkdir(parents=True, mode=0o700)
        palace.chmod(0o700)
        document = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": "~/work",
                    "dockerProfile": "work",
                    "modelStack": None,
                    "accountPools": ["work", "shared"],
                    "memoryPalace": "~/.mempalace/work",
                    "memoryWing": "work",
                }
            ],
        }

        resolved = resolve_control_plane_context(
            document, project, home=home
        )

        self.assertEqual(
            resolved["route"]["accountPools"], ["work", "shared"]
        )
        self.assertEqual(
            resolved["route"]["contextRootReal"], str(home / "work")
        )

    def test_control_plane_context_rejects_unsafe_and_canonical_alias_roots(
        self,
    ) -> None:
        home = Path(self.temporary.name).resolve() / "home"
        project = home / "work"
        project.mkdir(parents=True)
        alias = home / "alias"
        alias.symlink_to(project, target_is_directory=True)

        def context(root: str, wing: str) -> dict[str, object]:
            return {
                "root": root,
                "dockerProfile": None,
                "modelStack": None,
                "accountPools": ["shared"],
                "memoryPalace": f"~/.mempalace/{wing}",
                "memoryWing": wing,
            }

        with self.assertRaisesRegex(ContextError, "unsafe"):
            resolve_control_plane_context(
                {
                    "schemaVersion": 1,
                    "contexts": [context("~", "home")],
                },
                home,
                home=home,
            )
        with self.assertRaisesRegex(ContextError, "overlap"):
            resolve_control_plane_context(
                {
                    "schemaVersion": 1,
                    "contexts": [
                        context("~/work", "work"),
                        context("~/alias", "alias"),
                    ],
                },
                project,
                home=home,
            )

    def test_incomplete_staging_entries_are_never_visible_to_readers(self) -> None:
        staging = self.state / "logical-session-staging"
        staging.mkdir(mode=0o700)
        pending = staging / "oc-s-0000000000000001"
        pending.mkdir(mode=0o700)

        self.assertEqual(list_logical_sessions(self.state), ())

    def test_resume_materializes_current_graph_without_changing_logical_routes(
        self,
    ) -> None:
        data_root = self.state.parent
        repository = data_root / "project"
        repository.mkdir()
        for arguments in (
            ("init", "-q"),
            ("config", "user.email", "tests@example.invalid"),
            ("config", "user.name", "Resume tests"),
        ):
            subprocess.run(
                ["git", "-C", str(repository), *arguments],
                check=True,
                capture_output=True,
            )
        (repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", "tracked.txt"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "fixture"],
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
                "https://github.com/example/resume.git",
            ],
            check=True,
        )
        graph = resolve_graph_target(repository, data_root)
        graph.output_dir.mkdir(parents=True, mode=0o700)
        graph.output_dir.chmod(0o700)

        def write_graph(node_id: str) -> None:
            graph.graph_file.write_text(
                json.dumps(
                    {
                        "built_at_commit": graph.revision,
                        "nodes": [
                            {
                                "id": node_id,
                                "source_file": "tracked.txt",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            graph.graph_file.chmod(0o600)
            graph.metadata_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repository_identity": graph.identity.key,
                        "revision": graph.revision,
                        "state_id": graph.state_id,
                        "kind": graph.kind,
                        "built_at_commit": graph.revision,
                    }
                ),
                encoding="utf-8",
            )
            graph.metadata_file.chmod(0o600)

        write_graph("initial")
        controller = self.binding("gpt-5.6-sol", "gpt", 100)
        agents = {
            role: self.binding("gpt-5.6-terra", "gpt", 101 + index)
            for index, role in enumerate(ROLES)
        }
        logical = create_logical_session(
            self.state,
            project_root=repository,
            stack="balanced",
            controller=controller,
            agents=agents,
        )
        palace = data_root / "palace"
        palace.mkdir(mode=0o700)
        projects = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": str(repository),
                    "dockerProfile": None,
                    "modelStack": None,
                    "accountPools": ["work"],
                    "memoryPalace": str(palace),
                    "memoryWing": "work",
                }
            ],
        }
        config = ResolvedConfig(
            documents={"projects": projects, "providers": {}},
            sources={},
        )
        config_root = data_root / "config"
        config_root.mkdir(mode=0o700)
        paths = {
            "data": data_root,
            "state": self.state,
            "config": config_root,
        }
        context = orichum_cli.resolve_control_plane_context(
            projects, repository
        )
        with mock.patch.object(
            session_config.shutil,
            "which",
            side_effect=lambda name: (
                "/opt/tools/graphify-mcp"
                if name == "graphify-mcp"
                else None
            ),
        ):
            initial = session_config.create_resolved_session(
                orichum_cli.WORKFLOW_ROOT,
                data_root=data_root,
                context=context,
                effective=orichum_cli._effective_for(logical),
                plugin_source=orichum_cli.WORKFLOW_ROOT
                / "controller"
                / "plugin",
            )
            write_graph("replacement")
            with (
                mock.patch.object(orichum_cli, "_verify_runtime"),
                mock.patch.object(
                    orichum_cli, "load_accounts", return_value=()
                ),
                mock.patch.object(
                    orichum_cli, "validate_account_bindings"
                ),
                mock.patch.object(
                    orichum_cli, "_validate_session_routes"
                ),
                mock.patch.object(orichum_cli, "_validate_live_models"),
            ):
                resumed = orichum_cli._prepare_resume(
                    paths,
                    config,
                    identifier=logical.id,
                    launch_dir=repository,
                )

        resumed_context = json.loads(
            resumed.physical.context_file.read_text()
        )
        resumed_mcp = json.loads(resumed.physical.mcp_file.read_text())
        initial_context = json.loads(initial.context_file.read_text())
        initial_snapshot = Path(initial_context["graph"]["graphFile"])
        resumed_snapshot = Path(resumed_context["graph"]["graphFile"])
        self.assertNotEqual(initial.run_id, resumed.physical.run_id)
        self.assertEqual(resumed.logical.id, logical.id)
        self.assertEqual(resumed.logical.controller, logical.controller)
        self.assertEqual(resumed.logical.agents, logical.agents)
        self.assertEqual(initial_snapshot.parent, initial.run_dir)
        self.assertEqual(resumed_snapshot.parent, resumed.physical.run_dir)
        self.assertNotEqual(initial_snapshot, resumed_snapshot)
        self.assertNotEqual(
            initial_snapshot.read_bytes(), resumed_snapshot.read_bytes()
        )
        self.assertEqual(
            resumed_context["graph"]["sha256"],
            hashlib.sha256(graph.graph_file.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            resumed_snapshot.read_bytes(), graph.graph_file.read_bytes()
        )
        self.assertEqual(
            resumed_mcp["mcpServers"]["graphify"]["args"],
            ["--graph", str(resumed_snapshot)],
        )

    def test_session_plan_pins_primary_fallback_and_routed_effective_models(self) -> None:
        models = {
            "gpt-controller": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-controller",
            },
            "gpt-worker": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-worker",
            },
        }
        config = {
            "model-stacks": {
                "schemaVersion": 1,
                "defaultStack": "balanced",
                "models": models,
                "stacks": {
                    "balanced": {
                        "controller": "gpt-controller",
                        "agents": {
                            role: ["gpt-worker"] for role in ROLES
                        },
                    }
                },
            },
            "providers": {
                "providers": {"openai": {"authType": "codex"}},
                "accountPools": {"work": {"providers": ["openai"]}},
                "fallbackRoutes": {"gpt": ["openai"]},
            },
        }
        config["model-stacks"] = normalize_model_stacks(
            config["model-stacks"]
        )

        def account(suffix: str, priority: int) -> Account:
            return Account(
                id=f"oc-a-{suffix}",
                name=f"Account {suffix}",
                provider="openai",
                credential_ref=f"codex-{suffix}.json",
                pool="work",
                routing_prefix=f"oc-r-{suffix}",
                priority=priority,
                state="active",
                original_prefix=None,
                original_priority=None,
            )

        plan = resolve_session_plan(
            config,
            (
                account("0000000000000001", 100),
                account("0000000000000002", 50),
            ),
            pools=("work",),
            requested_stack=None,
            health={},
            selection_ordinal=0,
        )

        self.assertEqual(plan.stack, "balanced")
        self.assertEqual(
            plan.controller.primary.upstream_model,
            "oc-r-0000000000000001/gpt-controller",
        )
        self.assertEqual(len(plan.controller.fallbacks), 1)
        self.assertEqual(
            plan.controller.fallbacks[0].account_id,
            "oc-a-0000000000000002",
        )
        self.assertEqual(
            plan.effective.controller,
            plan.controller.primary.upstream_model,
        )
        for role in ROLES:
            self.assertEqual(
                plan.effective.agents[role],
                plan.agents[role].primary.upstream_model,
            )

    def test_session_plan_tries_candidates_in_order_and_honors_account_binding(
        self,
    ) -> None:
        controller_candidates = [
            {
                "id": "oc-c-1111111111111111",
                "model": "gpt-unavailable",
                "providers": ["openai"],
            },
            {
                "id": "oc-c-2222222222222222",
                "model": "gpt-controller",
                "providers": ["openai"],
            },
        ]
        agent_candidate = {
            "id": "oc-c-3333333333333333",
            "model": "gpt-worker",
            "providers": ["openai"],
        }
        config = {
            "model-stacks": {
                "schemaVersion": 2,
                "defaultStack": "balanced",
                "models": {
                    "gpt-unavailable": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-unavailable"},
                    },
                    "gpt-controller": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-controller-live"},
                    },
                    "gpt-worker": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-worker-live"},
                    },
                },
                "stacks": {
                    "balanced": {
                        "controller": controller_candidates,
                        "agents": {
                            role: [dict(agent_candidate, id=f"oc-c-{index:016x}")]
                            for index, role in enumerate(ROLES, start=3)
                        },
                    }
                },
            },
            "providers": {
                "providers": {"openai": {"authType": "codex"}},
                "accountPools": {"work": {"providers": ["openai"]}},
                "fallbackRoutes": {"gpt": ["openai"]},
            },
        }

        def account(suffix: str, priority: int) -> Account:
            return Account(
                id=f"oc-a-{suffix}",
                name=f"Account {suffix}",
                provider="openai",
                credential_ref=f"codex-{suffix}.json",
                pool="work",
                routing_prefix=f"oc-r-{suffix}",
                priority=priority,
                state="active",
                original_prefix=None,
                original_priority=None,
            )

        primary = account("0000000000000001", 100)
        secondary = account("0000000000000002", 50)
        bindings = StackBindings(
            {"oc-c-2222222222222222": secondary.id}
        )
        available = {
            f"{primary.routing_prefix}/gpt-controller-live",
            f"{secondary.routing_prefix}/gpt-controller-live",
            f"{primary.routing_prefix}/gpt-worker-live",
            f"{secondary.routing_prefix}/gpt-worker-live",
        }

        plan = resolve_session_plan(
            config,
            (primary, secondary),
            pools=("work",),
            requested_stack=None,
            health={},
            selection_ordinal=0,
            bindings=bindings,
            available_models=available,
        )

        self.assertEqual(plan.controller.primary.account_id, secondary.id)
        self.assertEqual(plan.controller.fallbacks, ())
        self.assertEqual(
            plan.controller.primary.upstream_model,
            f"{secondary.routing_prefix}/gpt-controller-live",
        )


if __name__ == "__main__":
    unittest.main()
