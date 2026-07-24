#!/usr/bin/env python3
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common import orichum_cli


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class OrichumCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = {
            "ORICHUM_CONFIG_HOME": str(REPOSITORY_ROOT / "config"),
            "ORICHUM_DATA_HOME": str(self.root / "data"),
            "ORICHUM_CACHE_HOME": str(self.root / "cache"),
        }

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = orichum_cli.main(list(arguments))
        return status, stdout.getvalue(), stderr.getvalue()

    def test_config_validate_paths_and_redacted_show(self) -> None:
        status, stdout, stderr = self.run_cli("config", "validate")
        self.assertEqual((status, stdout, stderr), (0, "", ""))

        status, stdout, stderr = self.run_cli("config", "paths")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "cache": str(self.root / "cache"),
                "config": str(REPOSITORY_ROOT / "config"),
                "data": str(self.root / "data"),
                "state": str(self.root / "data" / "state"),
            },
        )
        self.assertFalse((self.root / "data").exists())

        status, stdout, stderr = self.run_cli("config", "show")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        shown = json.loads(stdout)
        self.assertEqual(
            shown["controller-policy"]["value"], "<policy omitted>"
        )
        self.assertEqual(
            shown["model-stacks"]["source"], "config/model-stacks.json"
        )
        self.assertNotIn("secret", stdout.lower())
        self.assertNotIn("authorization:", stdout.lower())

    def test_context_models_provider_and_plugin_read_only_commands(self) -> None:
        status, stdout, stderr = self.run_cli("context", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("ACCOUNT POOLS", stdout)
        self.assertNotIn("~/xebia", stdout)

        status, stdout, stderr = self.run_cli("models", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("gpt-5.6-sol", stdout)
        self.assertIn("openai", stdout)

        status, stdout, stderr = self.run_cli("models", "validate")
        self.assertEqual((status, stdout, stderr), (0, "", ""))

        status, stdout, stderr = self.run_cli("models", "resolve")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        resolved = json.loads(stdout)
        self.assertEqual(resolved["stack"], "balanced")
        self.assertEqual(resolved["controller"], "gpt-5.6-sol")

        status, stdout, stderr = self.run_cli("provider", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("antigravity", stdout)
        self.assertIn("openai-compatible", stdout)

    def test_external_diagnostics_use_argv_runner_without_shell(self) -> None:
        with mock.patch.object(orichum_cli, "_run_external", return_value=8) as run:
            status, _, _ = self.run_cli("doctor")
            self.assertEqual(status, 8)
            run.assert_called_once_with("orichum-doctor", [])

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli("provider", "login", "codex")
            self.assertEqual(status, 0)
            run.assert_called_once_with("orichum-login", ["codex"])

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli(
                "plugin", "add", "github@official"
            )
            self.assertEqual(status, 0)
            run.assert_called_once_with(
                "orichum-plugin", ["add", "github@official"]
            )

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli(
                "context", "add", "/work/acme", "--pool", "shared"
            )
            self.assertEqual(status, 0)
            run.assert_called_once_with(
                "orichum-context",
                ["add", "/work/acme", "--pool", "shared"],
            )

    def test_headroom_status_reports_only_orichum_proxy_health(self) -> None:
        payload = json.dumps(
            {
                "service": "headroom-proxy",
                "status": "healthy",
                "ready": True,
                "version": "0.32.1",
            }
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        data_home = self.root / "data"
        data_home.mkdir(mode=0o700)
        ports = data_home / "service-ports.json"
        ports.write_text(
            json.dumps({"headroomPort": server.server_port}),
            encoding="utf-8",
        )
        ports.chmod(0o600)

        status, stdout, stderr = self.run_cli("headroom", "status")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            f"Headroom 0.32.1: healthy and ready at "
            f"http://127.0.0.1:{server.server_port}\n",
        )

    def provision_account_runtime(self) -> tuple[Path, Path]:
        config_home = self.root / "private-config"
        shutil.copytree(REPOSITORY_ROOT / "config", config_home)
        config_home.chmod(0o700)
        data_home = self.root / "private-data"
        auth_dir = data_home / "auth"
        auth_dir.mkdir(parents=True, mode=0o700)
        data_home.chmod(0o700)
        ports = data_home / "service-ports.json"
        ports.write_text(
            json.dumps({"cliproxyPort": 18317}), encoding="utf-8"
        )
        ports.chmod(0o600)
        key = data_home / "cliproxy-management.key"
        key.write_text("a" * 48 + "\n", encoding="ascii")
        key.chmod(0o600)
        credential = auth_dir / "claude-work.json"
        credential.write_text(
            json.dumps(
                {
                    "type": "claude",
                    "email": "work@example.com",
                    "access_token": "DO-NOT-PRINT",
                }
            ),
            encoding="utf-8",
        )
        credential.chmod(0o600)
        self.environment["ORICHUM_CONFIG_HOME"] = str(config_home)
        self.environment["ORICHUM_DATA_HOME"] = str(data_home)
        management = mock.patch.object(orichum_cli, "patch_auth_fields")
        self.management_patch = management.start()
        self.addCleanup(management.stop)
        def apply_fields(_endpoint, reference: str, fields: dict[str, object]):
            target = auth_dir / reference
            document = json.loads(target.read_text(encoding="utf-8"))
            document.update(fields)
            target.write_text(json.dumps(document), encoding="utf-8")
            target.chmod(0o600)
        self.management_patch.side_effect = apply_fields
        return config_home, credential

    def test_provider_account_lifecycle_is_named_private_and_redacted(self) -> None:
        config_home, credential = self.provision_account_runtime()

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Xebia Claude",
            "anthropic",
            credential.name,
            "xebia",
            "--priority",
            "primary",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        registry = config_home / "accounts.json"
        self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
        document = json.loads(registry.read_text(encoding="utf-8"))
        account_id = document["accounts"][0]["id"]
        self.assertEqual(document["accounts"][0]["priority"], 100)
        self.management_patch.assert_called_once()
        self.assertEqual(
            self.management_patch.call_args.args[1], credential.name
        )
        self.assertEqual(
            self.management_patch.call_args.args[2],
            {
                "prefix": document["accounts"][0]["routingPrefix"],
                "priority": 100,
            },
        )

        status, stdout, stderr = self.run_cli("provider", "accounts")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Xebia Claude", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("ACTIVE", stdout)
        self.assertNotIn(credential.name, stdout)
        self.assertNotIn("DO-NOT-PRINT", stdout)
        self.assertNotIn(document["accounts"][0]["routingPrefix"], stdout)

        for arguments in (
            ("priority", account_id, "secondary"),
            ("rename", account_id, "Primary Work"),
            ("disable", account_id),
            ("enable", account_id),
        ):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.run_cli(
                    "provider", "account", *arguments
                )
                self.assertEqual((status, stdout, stderr), (0, "", ""))

        updated = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(updated["name"], "Primary Work")
        self.assertEqual(updated["priority"], 50)
        self.assertEqual(updated["state"], "active")

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account_id
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["accounts"], []
        )

    def test_account_add_rejects_provider_pool_and_credential_mismatch(self) -> None:
        _, credential = self.provision_account_runtime()
        cases = (
            ("missing", credential.name, "xebia"),
            ("anthropic", credential.name, "missing"),
            ("openai", credential.name, "xebia"),
            ("anthropic", "../claude-work.json", "xebia"),
        )
        for provider, reference, pool in cases:
            with self.subTest(provider=provider, reference=reference, pool=pool):
                status, stdout, stderr = self.run_cli(
                    "provider",
                    "account",
                    "add",
                    "Rejected",
                    provider,
                    reference,
                    pool,
                )
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("ERROR:", stderr)

    def test_account_add_failure_leaves_recoverable_pending_route(self) -> None:
        config_home, credential = self.provision_account_runtime()
        self.management_patch.side_effect = orichum_cli.ManagementError(
            "injected management failure"
        )

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Work Claude",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("injected management failure", stderr)
        registry = config_home / "accounts.json"
        pending = json.loads(
            registry.read_text(encoding="utf-8")
        )["accounts"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending-add")

    def test_account_add_timeout_reconciles_and_remove_restores_metadata(self) -> None:
        config_home, credential = self.provision_account_runtime()
        original = json.loads(credential.read_text(encoding="utf-8"))
        original.update({"prefix": "prior-route", "priority": 7})
        credential.write_text(json.dumps(original), encoding="utf-8")
        credential.chmod(0o600)

        apply_fields = self.management_patch.side_effect
        first = True

        def apply_then_timeout(endpoint, reference, fields):
            nonlocal first
            apply_fields(endpoint, reference, fields)
            if first:
                first = False
                raise orichum_cli.ManagementError("ambiguous timeout")

        self.management_patch.side_effect = apply_then_timeout
        status, _, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Recoverable",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual(status, 2)
        self.assertIn("ambiguous timeout", stderr)
        registry = config_home / "accounts.json"
        account = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(account["state"], "pending-add")

        self.management_patch.side_effect = apply_fields
        status, stdout, stderr = self.run_cli(
            "provider", "account", "sync", account["id"]
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        active = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(active["state"], "active")

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        restored = json.loads(credential.read_text(encoding="utf-8"))
        self.assertEqual(restored["prefix"], "prior-route")
        self.assertEqual(restored["priority"], 7)
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["accounts"], []
        )

    def test_account_publication_requires_readback_before_activation(self) -> None:
        config_home, credential = self.provision_account_runtime()
        self.management_patch.side_effect = None

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Unverified",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("not verified", stderr)
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        self.assertEqual(account["state"], "pending-add")

    def test_account_add_and_enable_reject_disabled_live_credential(self) -> None:
        config_home, credential = self.provision_account_runtime()
        document = json.loads(credential.read_text(encoding="utf-8"))
        document["disabled"] = True
        credential.write_text(json.dumps(document), encoding="utf-8")
        credential.chmod(0o600)

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Disabled",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("disabled", stderr)
        self.assertFalse((config_home / "accounts.json").exists())

    def test_context_mutations_delegate_without_loading_control_plane(self) -> None:
        cases = (
            ("context", "add", "/tmp/project"),
            ("context", "populate", "/tmp/project"),
            ("context", "remove", "/tmp/project"),
        )
        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    status, stdout, stderr = self.run_cli(*arguments)
                    self.assertEqual(status, 0)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "")
            self.assertEqual(
                run.call_args_list,
                [
                    mock.call("orichum-context", ["add", "/tmp/project"]),
                    mock.call("orichum-context", ["populate", "/tmp/project"]),
                    mock.call("orichum-context", ["remove", "/tmp/project"]),
                ],
            )

    def test_paths_and_context_delegation_do_not_require_valid_config(self) -> None:
        self.environment["ORICHUM_CONFIG_HOME"] = str(
            self.root / "missing-config"
        )

        status, stdout, stderr = self.run_cli("config", "paths")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["config"], str(self.root / "missing-config")
        )

        with mock.patch.object(
            orichum_cli, "_run_external", return_value=7
        ) as run:
            for arguments in (("context", "add", "/tmp/project"),):
                with self.subTest(arguments=arguments):
                    status, stdout, stderr = self.run_cli(*arguments)
                    self.assertEqual(status, 7)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "")
            run.assert_called_once_with(
                "orichum-context", ["add", "/tmp/project"]
            )

    def test_plain_orichum_is_an_explicit_session_runtime_gate(self) -> None:
        status, stdout, stderr = self.run_cli()
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("installed launcher", stderr)

    def test_caller_cannot_override_pinned_models_agents_or_transcript(self) -> None:
        blocked = (
            "--agents={}",
            "--fallback-model=other",
            "--continue",
            "-c",
            "--resume=00000000-0000-4000-8000-000000000000",
            "-r00000000-0000-4000-8000-000000000000",
            "--from-pr=123",
            "--safe-mode",
        )
        for argument in blocked:
            with self.subTest(argument=argument):
                with self.assertRaises(orichum_cli.CliError):
                    orichum_cli._validate_user_claude_arguments([argument])

    def test_run_and_resume_dispatch_owned_session_launch(self) -> None:
        prepared = object()
        with (
            mock.patch.object(
                orichum_cli, "_prepare_new_session", return_value=prepared
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli("run", "review", "this")
        prepare.assert_called_once()
        self.assertFalse(launch.call_args.kwargs["resume"])
        self.assertEqual(
            launch.call_args.kwargs["arguments"], ["review", "this"]
        )

        with (
            mock.patch.object(
                orichum_cli, "_prepare_resume", return_value=prepared
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli("resume", "oc-s-0000000000000001", "continue")
        self.assertEqual(
            prepare.call_args.kwargs["identifier"],
            "oc-s-0000000000000001",
        )
        self.assertTrue(launch.call_args.kwargs["resume"])
        self.assertEqual(launch.call_args.kwargs["arguments"], ["continue"])

    def test_sessions_empty_table_is_redacted(self) -> None:
        (self.root / "data" / "state").mkdir(parents=True, mode=0o700)
        status, stdout, stderr = self.run_cli("sessions")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("PROJECT", stdout)
        self.assertNotIn("credential", stdout.lower())
        self.assertNotIn("routing", stdout.lower())

    def test_session_routes_prints_opaque_account_ids_not_display_names(
        self,
    ) -> None:
        def route(account_id: str, provider: str, model: str):
            return SimpleNamespace(
                account_id=account_id,
                provider=provider,
                logical_model=model,
            )

        controller = SimpleNamespace(
            primary=route("oc-a-gpt", "openai", "gpt-5.6-sol"),
            fallbacks=(),
        )
        critic = SimpleNamespace(
            primary=route(
                "oc-a-claude", "anthropic", "claude-sonnet-5"
            ),
            fallbacks=(
                route(
                    "oc-a-antigravity",
                    "antigravity",
                    "claude-sonnet-5",
                ),
            ),
        )
        session = SimpleNamespace(
            controller=controller,
            agents={
                role: critic if role == "correctness-critic" else controller
                for role in orichum_cli.ROLES
            },
        )
        accounts = (
            SimpleNamespace(id="oc-a-gpt", name="Personal GPT"),
            SimpleNamespace(id="oc-a-claude", name="Work Claude"),
            SimpleNamespace(
                id="oc-a-antigravity", name="Antigravity Reserve"
            ),
        )

        output = orichum_cli._session_routes(session, accounts)

        self.assertIn("oc-a-gpt", output)
        self.assertIn("oc-a-claude", output)
        self.assertIn("oc-a-antigravity (antigravity)", output)
        self.assertNotIn("Personal GPT", output)
        self.assertNotIn("Work Claude", output)
        self.assertNotIn("Antigravity Reserve", output)

    def test_fork_dispatches_fresh_session_with_bounded_handoff(self) -> None:
        prepared = object()
        with (
            mock.patch.object(
                orichum_cli,
                "_prepare_fork",
                return_value=(prepared, "bounded handoff"),
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli(
                "fork",
                "oc-s-0000000000000001",
                "--stack",
                "balanced",
            )
        self.assertEqual(
            prepare.call_args.kwargs["identifier"],
            "oc-s-0000000000000001",
        )
        self.assertEqual(prepare.call_args.kwargs["requested_stack"], "balanced")
        self.assertFalse(launch.call_args.kwargs["resume"])
        self.assertEqual(launch.call_args.kwargs["handoff"], "bounded handoff")

    def test_handoff_reader_rejects_public_symlink_and_oversized_files(self) -> None:
        handoff = self.root / "handoff.md"
        handoff.write_text("Current task and verified state.", encoding="utf-8")
        handoff.chmod(0o600)
        self.assertEqual(
            orichum_cli._read_handoff(handoff),
            "Current task and verified state.",
        )
        handoff.chmod(0o644)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(handoff)
        handoff.chmod(0o600)
        linked = self.root / "linked.md"
        linked.symlink_to(handoff)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(linked)
        handoff.write_bytes(b"x" * (16 * 1024 + 1))
        handoff.chmod(0o600)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(handoff)

    def test_session_github_identity_is_resolved_from_immutable_context(self) -> None:
        physical = mock.Mock(context_file=self.root / "context.json")
        expected = self.root / "github" / "work"
        with (
            mock.patch.object(
                orichum_cli,
                "_read_stable_file",
                return_value=json.dumps(
                    {"route": {"githubAccount": "athevar-xebia"}}
                ).encode(),
            ),
            mock.patch.object(
                orichum_cli,
                "ensure_github_identity",
                return_value=expected,
            ) as ensure,
        ):
            resolved = orichum_cli._github_config_for_session(
                {"data": self.root / "data"}, physical
            )

        self.assertEqual(resolved, expected)
        ensure.assert_called_once_with(
            self.root / "data", "athevar-xebia"
        )

    def test_session_environment_scrubs_token_identity_overrides(self) -> None:
        physical = SimpleNamespace(
            mcp_file=self.root / "mcp.json",
            run_dir=self.root / "run",
            context_file=self.root / "context.json",
            context_sha256="a" * 64,
            effective_models_file=self.root / "models.json",
            run_id="run-1",
        )
        prepared = SimpleNamespace(
            logical=SimpleNamespace(id="oc-s-0000000000000001"),
            physical=physical,
        )
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        with mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "wrong-account",
                "GITHUB_TOKEN": "wrong-account",
                "GH_HOST": "enterprise.example",
            },
            clear=False,
        ):
            environment = orichum_cli._session_environment(
                prepared,
                paths,
                {
                    "maxToolUseConcurrency": 3,
                    "maxSubagentsPerSession": 24,
                },
                self.root / "github" / "work",
                self.root / "claudex.toml",
            )

        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GH_HOST", environment)
        self.assertEqual(
            environment["GH_CONFIG_DIR"],
            str(self.root / "github" / "work"),
        )

    def test_session_without_selected_identity_preserves_github_environment(
        self,
    ) -> None:
        physical = SimpleNamespace(
            mcp_file=self.root / "mcp.json",
            run_dir=self.root / "run",
            context_file=self.root / "context.json",
            context_sha256="a" * 64,
            effective_models_file=self.root / "models.json",
            run_id="run-1",
        )
        prepared = SimpleNamespace(
            logical=SimpleNamespace(id="oc-s-0000000000000001"),
            physical=physical,
        )
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        with mock.patch.dict(
            os.environ,
            {
                "GH_TOKEN": "caller-token",
                "GH_HOST": "github.example",
                "GH_CONFIG_DIR": "/caller/github-config",
            },
            clear=False,
        ):
            environment = orichum_cli._session_environment(
                prepared,
                paths,
                {
                    "maxToolUseConcurrency": 3,
                    "maxSubagentsPerSession": 24,
                },
                None,
                self.root / "claudex.toml",
            )

        self.assertEqual(environment["GH_TOKEN"], "caller-token")
        self.assertEqual(environment["GH_HOST"], "github.example")
        self.assertEqual(
            environment["GH_CONFIG_DIR"], "/caller/github-config"
        )


if __name__ == "__main__":
    unittest.main()
