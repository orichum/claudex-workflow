#!/usr/bin/env python3
"""Behavioral contract for LeanCTX-backed durable project knowledge."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations.common import leanctx_contract
from integrations.common.install_control_plane import (
    InstallControlPlaneError,
    stage,
)
from integrations.common.project_context import validate_config_document
from integrations.common.session_config import _session_mcp_payload


class LeanctxMemoryTests(unittest.TestCase):
    def test_contract_exposes_compact_project_memory(self) -> None:
        self.assertEqual(
            leanctx_contract.AUTO_APPROVED_TOOLS,
            (
                "ctx_read",
                "ctx_search",
                "ctx_tree",
                "ctx_expand",
                "ctx_graph",
                "ctx_impact",
                "ctx_callgraph",
                "ctx_knowledge",
                "ctx_overview",
            ),
        )
        config = leanctx_contract.config_bytes().decode("utf-8")
        self.assertIn("auto_capture = true", config)
        self.assertIn("enable_wakeup_ctx = true", config)
        self.assertIn("minimal_overhead = true", config)
        self.assertIn("journal_enabled = false", config)

    def test_server_shares_data_but_isolates_session_runtime(self) -> None:
        root = Path("/work/project")
        session = Path("/private/runs/run.one/leanctx")
        shared = Path("/private/data/leanctx")

        server = leanctx_contract.mcp_server(
            Path("/private/bin/lean-ctx"),
            root,
            session,
            shared,
        )

        environment = server["env"]
        self.assertEqual(
            environment["LEAN_CTX_DATA_DIR"],
            "/private/data/leanctx/lean-ctx",
        )
        self.assertEqual(
            environment["XDG_DATA_HOME"],
            "/private/data/leanctx",
        )
        for name in (
            "LEAN_CTX_CONFIG_DIR",
            "LEAN_CTX_STATE_DIR",
            "LEAN_CTX_CACHE_DIR",
        ):
            self.assertTrue(environment[name].startswith(str(session)))
        self.assertNotEqual(
            environment["LEAN_CTX_STATE_DIR"],
            environment["LEAN_CTX_CACHE_DIR"],
        )

    def test_session_payload_never_attaches_a_second_memory_server(self) -> None:
        context = {
            "repoRootReal": "/work/project",
            "route": {
                "contextRootReal": "/work",
                "dockerProfile": None,
                "memoryAvailable": True,
                "palacePathReal": "/private/retired",
            },
        }
        with (
            mock.patch(
                "integrations.common.session_config._leanctx_binary",
                return_value=Path("/private/bin/lean-ctx"),
            ),
            mock.patch(
                "integrations.common.session_config.shutil.which",
                return_value="/private/bin/unrelated-memory-server",
            ),
        ):
            payload = _session_mcp_payload(
                context,
                Path("/private/runs/run.one"),
                Path("/private/data"),
            )

        self.assertEqual(set(payload["mcpServers"]), {"leanctx"})

    def test_project_context_requires_only_routing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / "project"
            root.mkdir()
            document = {
                "schemaVersion": 1,
                "contexts": [
                    {
                        "root": str(root),
                        "dockerProfile": None,
                        "modelStack": None,
                        "accountPools": ["shared"],
                        "githubAccount": None,
                    }
                ],
            }

            validate_config_document(
                document,
                home,
                stacks={"balanced": object()},
                account_pools={"shared"},
            )

    def test_installer_normalizes_preserved_project_contexts(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed"
            candidate = root / "candidate"
            installed.mkdir(mode=0o700)
            for name in (
                "model-stacks.json",
                "providers.json",
                "plugins.json",
                "runtime.json",
                "controller-policy.md",
            ):
                destination = installed / name
                destination.write_bytes(
                    (repository / "config" / name).read_bytes()
                )
                destination.chmod(0o600)
            projects = {
                "schemaVersion": 1,
                "contexts": [
                        {
                            "root": str(root / "project"),
                            "dockerProfile": None,
                            "modelStack": None,
                            "accountPools": ["shared"],
                            "githubAccount": None,
                            "memoryPalace": "/private/old",
                            "memoryWing": "old",
                        }
                    ],
                }
            (root / "project").mkdir()
            project_path = installed / "projects.json"
            project_path.write_text(json.dumps(projects), encoding="utf-8")
            project_path.chmod(0o600)

            stage(repository, installed, candidate)

            staged = json.loads(
                (candidate / "projects.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                staged,
                {
                    "schemaVersion": 1,
                    "contexts": [
                        {
                            "root": str(root / "project"),
                            "dockerProfile": None,
                            "modelStack": None,
                            "accountPools": ["shared"],
                            "githubAccount": None,
                        }
                    ],
                },
            )

    def test_installer_rejects_unknown_project_context_fields(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed"
            candidate = root / "candidate"
            installed.mkdir(mode=0o700)
            for name in (
                "model-stacks.json",
                "providers.json",
                "plugins.json",
                "runtime.json",
                "controller-policy.md",
            ):
                destination = installed / name
                destination.write_bytes(
                    (repository / "config" / name).read_bytes()
                )
                destination.chmod(0o600)
            projects = {
                "schemaVersion": 1,
                "contexts": [
                    {
                        "root": str(root / "project"),
                        "dockerProfile": None,
                        "modelStack": None,
                        "accountPools": ["shared"],
                        "githubAccount": None,
                        "unexpected": True,
                    }
                ],
            }
            project_path = installed / "projects.json"
            project_path.write_text(json.dumps(projects), encoding="utf-8")
            project_path.chmod(0o600)

            with self.assertRaisesRegex(
                InstallControlPlaneError,
                "installed projects.json is invalid",
            ):
                stage(repository, installed, candidate)


if __name__ == "__main__":
    unittest.main()
