from pathlib import Path
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import integrations.common.model_routing as model_routing
from integrations.common.model_routing import (
    EffectiveStack,
    ROLES,
    RoutingError,
    load_catalog,
    load_routing,
    materialize_runtime_plugin,
    resolve_effective,
    validate_stack_name,
)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.routing_path = self.root / "model-routing.json"
        self.catalog_path = self.root / "models.json"
        agents = {
            role: ["preferred/" + role, "fallback/" + role]
            for role in ROLES
        }
        self.routing = {
            "schemaVersion": 1,
            "defaultStack": "balanced",
            "stacks": {
                "balanced": {
                    "controller": "controller/main",
                    "agents": agents,
                },
                "xebia": {
                    "controller": "controller/xebia",
                    "agents": agents,
                },
            },
        }
        self.routing_path.write_text(json.dumps(self.routing), encoding="utf-8")
        available = ["controller/main"] + [
            "fallback/" + role for role in ROLES
        ]
        self.catalog_path.write_text(
            json.dumps({"object": "list", "data": [
                {"id": model} for model in available
            ]}),
            encoding="utf-8",
        )
        self.run_dir = self.root / "run.test"
        self.run_dir.mkdir(mode=0o700)
        self.source_plugin = self.root / "source-plugin"
        (self.source_plugin / "agents").mkdir(parents=True)
        (self.source_plugin / "workflows").mkdir()
        for role in ROLES:
            (self.source_plugin / "agents" / f"{role}.md").write_text(
                "---\n"
                f"name: {role}\n"
                "model: inherit\n"
                "---\n"
                f"{role} instructions\n",
                encoding="utf-8",
            )
        (self.source_plugin / "workflows" / "review.js").write_text(
            "export default 'unchanged';\n",
            encoding="utf-8",
        )
        self.effective = EffectiveStack(
            "balanced",
            "controller/main",
            {role: ("preferred/" + role,) for role in ROLES},
            {role: "preferred/" + role for role in ROLES},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_stack_uses_ordered_agent_fallbacks(self) -> None:
        routing = load_routing(self.routing_path)
        effective = resolve_effective(
            routing, load_catalog(self.catalog_path)
        )
        self.assertEqual(effective.stack_name, "balanced")
        self.assertEqual(effective.controller, "controller/main")
        self.assertEqual(
            effective.agents,
            {role: "fallback/" + role for role in ROLES},
        )

    def test_materialized_plugin_rewrites_only_model_frontmatter(self) -> None:
        plugin = materialize_runtime_plugin(
            self.source_plugin, self.run_dir / "plugin", self.effective
        )
        for role in ROLES:
            text = (plugin / "agents" / f"{role}.md").read_text()
            self.assertEqual(
                [
                    line
                    for line in text.splitlines()
                    if line.startswith("model: ")
                ],
                [f"model: {self.effective.agents[role]}"],
            )
        self.assertEqual(
            (plugin / "workflows" / "review.js").read_bytes(),
            (self.source_plugin / "workflows" / "review.js").read_bytes(),
        )

    def test_materialized_plugin_is_private_and_preserves_executable_files(
        self,
    ) -> None:
        executable = self.source_plugin / "workflows" / "run.sh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        plugin = materialize_runtime_plugin(
            self.source_plugin, self.run_dir / "plugin", self.effective
        )

        self.assertEqual(plugin.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (plugin / "workflows" / "review.js").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            (plugin / "workflows" / "run.sh").stat().st_mode & 0o777,
            0o700,
        )

    def test_materialized_plugin_rejects_symlink(self) -> None:
        link = self.source_plugin / "workflows" / "linked.js"
        link.symlink_to(self.source_plugin / "workflows" / "review.js")
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_existing_destination(self) -> None:
        (self.run_dir / "plugin").mkdir()
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

    def test_materialized_plugin_rejects_missing_agent(self) -> None:
        (self.source_plugin / "agents" / f"{ROLES[0]}.md").unlink()
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_invalid_agent_frontmatter(self) -> None:
        agent = self.source_plugin / "agents" / f"{ROLES[0]}.md"
        agent.write_text("model: inherit\n", encoding="utf-8")
        with self.assertRaisesRegex(RoutingError, "invalid frontmatter"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_special_file(self) -> None:
        os.mkfifo(self.source_plugin / "workflows" / "special")
        with self.assertRaisesRegex(RoutingError, "special file"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_requires_private_destination_parent(
        self,
    ) -> None:
        self.run_dir.chmod(0o755)
        with self.assertRaisesRegex(RoutingError, "unsafe permissions"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

    def test_materialized_plugin_rejects_foreign_owned_nested_directory(
        self,
    ) -> None:
        nested = self.source_plugin / "workflows"
        real_lstat = os.lstat

        def foreign_owner(path, *args, **kwargs):
            observed = real_lstat(path, *args, **kwargs)
            if not args and not kwargs and Path(path) == nested:
                values = list(observed)
                values[4] = observed.st_uid + 1
                return os.stat_result(values)
            return observed

        with mock.patch.object(
            model_routing.os, "lstat", side_effect=foreign_owner
        ):
            with self.assertRaisesRegex(RoutingError, "owner"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )

    def test_materialized_plugin_rejects_checked_directory_replacement(
        self,
    ) -> None:
        source_agents = self.source_plugin / "agents"
        replacement = self.root / "replacement-agents"
        displaced = self.root / "displaced-agents"
        shutil.copytree(source_agents, replacement)
        real_mkdir = os.mkdir
        swapped = False

        def swap_after_check(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            path = Path(path)
            if (
                not swapped
                and dir_fd is None
                and path == self.run_dir / "plugin" / "agents"
            ):
                source_agents.rename(displaced)
                replacement.rename(source_agents)
                swapped = True
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch.object(
            model_routing.os, "mkdir", side_effect=swap_after_check
        ):
            with self.assertRaisesRegex(RoutingError, "changed"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )
        self.assertTrue(swapped)

    def test_materialized_plugin_rejects_in_place_source_mutation(
        self,
    ) -> None:
        target = self.source_plugin / "workflows" / "review.js"
        target_inode = os.lstat(target).st_ino
        real_read = os.read
        mutated = False

        def mutate_during_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            data = real_read(descriptor, size)
            if not mutated and os.fstat(descriptor).st_ino == target_inode:
                target.write_text("changed during read with a new size\n")
                mutated = True
            return data

        with mock.patch.object(
            model_routing.os, "read", side_effect=mutate_during_read
        ):
            with self.assertRaisesRegex(RoutingError, "changed"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )
        self.assertTrue(mutated)

    def test_explicit_stack_with_missing_controller_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            RoutingError, "controller/xebia.*unavailable"
        ):
            resolve_effective(
                load_routing(self.routing_path),
                load_catalog(self.catalog_path),
                "xebia",
            )

    def test_unknown_stack_is_rejected(self) -> None:
        with self.assertRaisesRegex(RoutingError, "stack.*missing"):
            resolve_effective(
                load_routing(self.routing_path),
                load_catalog(self.catalog_path),
                "missing",
            )

    def test_model_id_cannot_inject_frontmatter(self) -> None:
        self.routing["stacks"]["balanced"]["controller"] = (
            "safe\nmodel: injected"
        )
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "model ID"):
            load_routing(self.routing_path)

    def test_unknown_role_is_rejected(self) -> None:
        agents = self.routing["stacks"]["balanced"]["agents"]
        agents["unknown-role"] = ["provider/model"]
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaises(RoutingError):
            load_routing(self.routing_path)

    def test_duplicate_candidates_are_rejected(self) -> None:
        agents = self.routing["stacks"]["balanced"]["agents"]
        agents["repository-explorer"] = ["same/model", "same/model"]
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaises(RoutingError):
            load_routing(self.routing_path)

    def test_boolean_schema_version_is_rejected(self) -> None:
        self.routing["schemaVersion"] = True
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "schemaVersion"):
            load_routing(self.routing_path)

    def test_float_schema_version_is_rejected(self) -> None:
        self.routing["schemaVersion"] = 1.0
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "schemaVersion"):
            load_routing(self.routing_path)

    def test_validate_stack_name_returns_safe_name(self) -> None:
        self.assertEqual(validate_stack_name("balanced"), "balanced")

    def test_validate_stack_name_rejects_invalid_values(self) -> None:
        for value in (None, "", "Upper", "safe\nstack", "a" * 64):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RoutingError, "project model stack.*invalid"
                ):
                    validate_stack_name(value, "project model stack")
