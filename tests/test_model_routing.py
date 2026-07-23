from pathlib import Path
import json
import tempfile
import unittest

from integrations.common.model_routing import (
    ROLES,
    RoutingError,
    load_catalog,
    load_routing,
    resolve_effective,
    validate_stack_name,
)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
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
