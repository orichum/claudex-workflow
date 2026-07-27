#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common import graph_manager
from integrations.common import orichum_cli
from integrations.common import stack_bindings
from integrations.common.leanctx_monitor import LeanctxRun, LeanctxStats
from integrations.common.stack_bindings import (
    StackBindingError,
    StackBindings,
    save_stack_bindings,
)


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

        status, stdout, stderr = self.run_cli("models", "stacks")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("STACK", stdout)
        self.assertIn("DEFAULT", stdout)
        self.assertIn("balanced", stdout)
        self.assertIn("gpt-5.6-sol", stdout)

        status, stdout, stderr = self.run_cli(
            "models", "resolve", "missing-stack"
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: model stack is not configured: missing-stack; "
            "available stacks: balanced\n",
        )

        status, stdout, stderr = self.run_cli("provider", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("antigravity", stdout)
        self.assertIn("openai-compatible", stdout)

    def test_stack_available_is_read_only_and_redacts_route_metadata(self) -> None:
        accounts = (
            orichum_cli.Account(
                id="oc-a-1111111111111111",
                name="Work Claude",
                provider="anthropic",
                credential_ref="claude-work.json",
                pool="xebia",
                routing_prefix="oc-r-1111111111111111",
                priority=100,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
            orichum_cli.Account(
                id="oc-a-2222222222222222",
                name="Antigravity",
                provider="antigravity",
                credential_ref="antigravity-work.json",
                pool="shared",
                routing_prefix="oc-r-2222222222222222",
                priority=50,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
        )
        raw = {
            "object": "list",
            "data": [
                {
                    "id": (
                        "oc-r-1111111111111111/"
                        "claude-sonnet-5"
                    )
                },
                {
                    "id": (
                        "oc-r-2222222222222222/"
                        "future-model"
                    )
                },
            ],
        }

        with (
            mock.patch.object(orichum_cli, "_verify_runtime") as verify,
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "claudexProxyPort": 13457,
                    "cliproxyPort": 8317,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli, "load_accounts", return_value=accounts
            ),
            mock.patch.object(
                orichum_cli, "fetch_live_catalog", return_value=raw
            ) as fetch,
        ):
            status, stdout, stderr = self.run_cli(
                "stack", "available"
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        verify.assert_called_once()
        fetch.assert_called_once_with(8317)
        self.assertIn("PROVIDER", stdout)
        self.assertIn("FAMILY", stdout)
        self.assertIn("MODEL", stdout)
        self.assertIn("ACCOUNTS", stdout)
        self.assertIn("STATUS", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("claude-sonnet-5", stdout)
        self.assertIn("Work Claude", stdout)
        self.assertIn("future-model", stdout)
        self.assertIn("unclassified", stdout)
        self.assertIn("not selectable", stdout)
        self.assertNotIn("oc-r-", stdout)
        self.assertNotIn("oc-a-", stdout)
        self.assertNotIn("claude-work.json", stdout)
        self.assertNotIn("antigravity-work.json", stdout)

    def test_stack_list_and_show_are_scriptable_and_redacted(self) -> None:
        status, stdout, stderr = self.run_cli("stack", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("STACK", stdout)
        self.assertIn("balanced", stdout)

        status, stdout, stderr = self.run_cli(
            "stack", "show", "balanced"
        )
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("architecture-advisor", stdout)
        self.assertIn("claude-opus-4-8", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("Automatic within provider", stdout)
        self.assertNotIn("oc-a-", stdout)
        self.assertNotIn("oc-c-", stdout)
        self.assertNotIn("oc-r-", stdout)
        self.assertNotIn(".json", stdout)

    def test_stack_configure_rejects_non_tty_before_wizard_dispatch(
        self,
    ) -> None:
        with mock.patch.object(
            orichum_cli, "run_stack_wizard", return_value=0
        ) as wizard:
            status, stdout, stderr = self.run_cli(
                "stack", "configure"
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: stack configuration requires an interactive terminal\n",
        )
        wizard.assert_not_called()

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

        with mock.patch.object(
            orichum_cli, "_run_external", return_value=0
        ) as run:
            status, _, _ = self.run_cli("graph", "status", "/work/acme")
            self.assertEqual(status, 0)
            run.assert_called_once_with(
                "orichum-graph", ["status", "/work/acme"]
            )

    def test_graph_external_preserves_the_callers_working_directory(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with (
            mock.patch.object(orichum_cli.Path, "cwd", return_value=self.root),
            mock.patch.object(
                orichum_cli.subprocess, "run", return_value=completed
            ) as run,
        ):
            status = orichum_cli._run_external("orichum-graph", ["."])

        self.assertEqual(status, 0)
        self.assertEqual(run.call_args.kwargs["cwd"], self.root)

    def test_headroom_command_is_rejected_by_argparse(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            orichum_cli.build_parser().parse_args(["headroom", "status"])
        self.assertEqual(raised.exception.code, 2)

    def test_runtime_service_ports_accepts_only_three_distinct_ports(
        self,
    ) -> None:
        data_home = self.root / "data"
        data_home.mkdir(mode=0o700)
        ports = data_home / "service-ports.json"
        ports.write_text(
            json.dumps(
                {
                    "claudexProxyPort": 13456,
                    "cliproxyPort": 8317,
                    "routeProxyPort": 13457,
                }
            ),
            encoding="utf-8",
        )
        ports.chmod(0o600)

        self.assertEqual(
            orichum_cli._runtime_service_ports({"data": data_home}),
            {
                "claudexProxyPort": 13456,
                "cliproxyPort": 8317,
                "routeProxyPort": 13457,
            },
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

    def test_account_remove_rejects_stack_candidate_binding(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Bound Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        save_stack_bindings(
            config_home / "stack-bindings.json",
            StackBindings(
                {"oc-c-a69e16d6ee83ad12": account["id"]}
            ),
            expected_digest=None,
        )

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("balanced", stderr)
        self.assertIn("correctness-critic", stderr)
        self.assertNotIn(credential.name, stderr)
        self.assertNotIn(account["routingPrefix"], stderr)
        self.assertEqual(
            json.loads(
                (config_home / "accounts.json").read_text(encoding="utf-8")
            )["accounts"][0]["state"],
            "active",
        )

    def test_account_remove_prunes_orphan_candidate_binding(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Orphaned Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        bindings_path = config_home / "stack-bindings.json"
        save_stack_bindings(
            bindings_path,
            StackBindings(
                {"oc-c-ffffffffffffffff": account["id"]}
            ),
            expected_digest=None,
        )

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )

        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertEqual(
            json.loads(
                (config_home / "accounts.json").read_text(encoding="utf-8")
            )["accounts"],
            [],
        )
        self.assertEqual(
            orichum_cli.load_stack_bindings(bindings_path),
            StackBindings({}),
        )

    def test_account_remove_serializes_against_new_binding_save(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Racing Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account_id = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]["id"]
        with mock.patch.dict(os.environ, self.environment, clear=False):
            paths, config = orichum_cli._load()

        removal_inside = threading.Event()
        release_removal = threading.Event()
        binding_attempted = threading.Event()
        binding_completed = threading.Event()
        original_find = orichum_cli.find_account
        original_flock = stack_bindings.fcntl.flock
        remove_errors: list[BaseException] = []
        binding_errors: list[BaseException] = []

        def blocking_find(accounts, selector):
            if (
                threading.current_thread().name == "account-removal"
                and not removal_inside.is_set()
            ):
                removal_inside.set()
                if not release_removal.wait(timeout=2):
                    raise AssertionError("removal test was not released")
            return original_find(accounts, selector)

        def observed_flock(descriptor: int, operation: int) -> None:
            if threading.current_thread().name == "binding-save":
                binding_attempted.set()
            original_flock(descriptor, operation)

        def remove() -> None:
            try:
                orichum_cli._mutate_account(
                    SimpleNamespace(
                        account_command="remove",
                        selector=account_id,
                    ),
                    paths,
                    config,
                )
            except BaseException as error:
                remove_errors.append(error)

        def bind() -> None:
            try:
                save_stack_bindings(
                    config_home / "stack-bindings.json",
                    StackBindings(
                        {"oc-c-a69e16d6ee83ad12": account_id}
                    ),
                    expected_digest=None,
                )
            except BaseException as error:
                binding_errors.append(error)
            finally:
                binding_completed.set()

        with (
            mock.patch.object(
                orichum_cli, "find_account", side_effect=blocking_find
            ),
            mock.patch.object(
                stack_bindings.fcntl,
                "flock",
                side_effect=observed_flock,
            ),
        ):
            removal = threading.Thread(target=remove, name="account-removal")
            binding = threading.Thread(target=bind, name="binding-save")
            removal.start()
            self.assertTrue(removal_inside.wait(timeout=2))
            binding.start()
            self.assertTrue(binding_attempted.wait(timeout=2))
            self.assertFalse(binding_completed.is_set())
            release_removal.set()
            removal.join(timeout=2)
            binding.join(timeout=2)

        self.assertFalse(removal.is_alive())
        self.assertFalse(binding.is_alive())
        self.assertEqual(remove_errors, [])
        self.assertEqual(len(binding_errors), 1)
        self.assertIsInstance(binding_errors[0], StackBindingError)
        self.assertIn("not registered", str(binding_errors[0]))
        self.assertFalse((config_home / "stack-bindings.json").exists())

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
            "--allowedTools=mcp__other__*",
            "--allowed-tools",
            "--disallowedTools=mcp__leanctx__ctx_read",
            "--disallowed-tools",
            "--plugin-url=https://example.invalid/plugin.zip",
            "--worktree=review",
            "-wreview",
            "--tmux",
            "--bare",
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

    def test_session_launch_preapproves_only_bounded_leanctx_tools(self) -> None:
        data = self.root / "data"
        config_home = self.root / "config"
        state = data / "state"
        run_dir = state / "sessions" / "run.test"
        plugin = run_dir / "plugin"
        for directory in (data / "bin", data / "model-config" / "current",
                          config_home, state, run_dir, plugin):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        claudex = data / "bin" / "claudex"
        shared_config = data / "model-config" / "current" / "claudex.toml"
        policy = config_home / "controller-policy.md"
        for path in (claudex, shared_config, policy):
            path.write_text("test\n", encoding="utf-8")
            path.chmod(0o700 if path == claudex else 0o600)
        physical = SimpleNamespace(
            run_dir=run_dir,
            mcp_file=run_dir / "mcp.json",
            context_file=run_dir / "context.json",
            effective_models_file=run_dir / "effective-models.json",
            plugin_dir=plugin,
            controller_model="gpt-5.6-sol",
        )
        physical.context_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "launchDirReal": "/Users/example/xebia/project",
                    "repoRootReal": "/Users/example/xebia/project",
                    "route": {
                        "id": "xebia",
                        "contextRootReal": "/Users/example/xebia",
                        "dockerProfile": "xebia",
                        "modelStack": None,
                        "accountPools": ["xebia", "shared"],
                        "githubAccount": "athevar-xebia",
                        "memoryWing": "xebia",
                        "memoryAvailable": True,
                        "memoryFailureCode": None,
                        "palacePathReal": "/Users/example/.mempalace/xebia",
                    },
                }
            ),
            encoding="utf-8",
        )
        physical.context_file.chmod(0o600)
        prepared = SimpleNamespace(
            logical=SimpleNamespace(
                id="oc-s-0000000000000001",
                claude_session_id="00000000-0000-4000-8000-000000000001",
            ),
            physical=physical,
        )
        paths = {
            "data": data,
            "config": config_home,
            "state": state,
        }
        resolved = SimpleNamespace(documents={"runtime": {"controller": {
            "effort": "high",
            "maxToolUseConcurrency": 3,
            "maxSubagentsPerSession": 24,
        }}})
        with (
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "cliproxyPort": 8317,
                    "claudexProxyPort": 13457,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli,
                "_reserve_session_claudex_port",
                return_value=13458,
            ),
            mock.patch.object(
                orichum_cli,
                "_materialize_session_claudex_config",
                return_value=run_dir / "claudex.toml",
            ),
            mock.patch.object(
                orichum_cli,
                "_github_config_for_session",
                return_value=None,
            ),
            mock.patch.object(
                orichum_cli,
                "_session_environment",
                return_value={"ORICHUM_SESSION_ID": prepared.logical.id},
            ),
            mock.patch.object(orichum_cli.os, "execvpe") as execute,
        ):
            orichum_cli._launch_session(
                prepared,
                paths,
                resolved,
                resume=False,
                arguments=("-p", "read with LeanCTX"),
            )

        command = execute.call_args.args[1]
        self.assertIn("--allowedTools", command)
        allowed_index = command.index("--allowedTools")
        self.assertEqual(
            command[allowed_index + 1],
            ",".join((
                "mcp__leanctx__ctx_read",
                "mcp__leanctx__ctx_search",
                "mcp__leanctx__ctx_tree",
                "mcp__leanctx__ctx_expand",
                "mcp__graphify__get_community",
                "mcp__graphify__get_neighbors",
                "mcp__graphify__get_node",
                "mcp__graphify__get_pr_impact",
                "mcp__graphify__god_nodes",
                "mcp__graphify__graph_stats",
                "mcp__graphify__list_prs",
                "mcp__graphify__query_graph",
                "mcp__graphify__shortest_path",
                "mcp__graphify__triage_prs",
                "mcp__mempalace__mempalace_diary_read",
                "mcp__mempalace__mempalace_follow_tunnels",
                "mcp__mempalace__mempalace_list_drawers",
                "mcp__mempalace__mempalace_list_hallways",
                "mcp__mempalace__mempalace_list_rooms",
                "mcp__mempalace__mempalace_list_tunnels",
                "mcp__mempalace__mempalace_search",
            )),
        )
        self.assertNotIn(
            "mcp__leanctx__ctx_patch",
            command[allowed_index + 1].split(","),
        )
        self.assertNotIn(
            "mcp__leanctx__ctx_shell",
            command[allowed_index + 1].split(","),
        )
        self.assertNotIn(
            "mcp__mempalace__mempalace_status",
            command[allowed_index + 1].split(","),
        )
        self.assertNotIn(
            "mcp__mempalace__mempalace_get_taxonomy",
            command[allowed_index + 1].split(","),
        )
        policy_index = command.index("--append-system-prompt-file")
        launch_policy = Path(command[policy_index + 1])
        self.assertEqual(launch_policy, run_dir / "launch-policy.md")
        binding_prompt = launch_policy.read_text(encoding="utf-8")
        self.assertIn('MCP_DOCKER profile: "xebia"', binding_prompt)
        self.assertIn('GitHub account: "athevar-xebia"', binding_prompt)
        self.assertIn('Mempalace wing: "xebia"', binding_prompt)
        self.assertIn(
            "already bound to this physical session", binding_prompt
        )
        self.assertIn(
            "Never activate, switch, create, update, or remove Docker MCP "
            "profiles",
            binding_prompt,
        )
        self.assertNotIn("mcp__leanctx__*", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_sessions_empty_table_is_redacted(self) -> None:
        (self.root / "data" / "state").mkdir(parents=True, mode=0o700)
        status, stdout, stderr = self.run_cli("sessions")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("PROJECT", stdout)
        self.assertNotIn("credential", stdout.lower())
        self.assertNotIn("routing", stdout.lower())

    def test_leanctx_parser_exposes_bounded_monitoring_commands(self) -> None:
        parser = orichum_cli.build_parser()

        self.assertEqual(
            parser.parse_args(["leanctx", "stats", "--run", "run.one"])
            .leanctx_command,
            "stats",
        )
        dashboard = parser.parse_args(
            [
                "leanctx",
                "dashboard",
                "--run",
                "run.one",
                "--port",
                "3341",
                "--open",
                "none",
            ]
        )
        self.assertEqual(dashboard.port, 3341)
        self.assertEqual(dashboard.open_mode, "none")
        self.assertEqual(
            parser.parse_args(["leanctx", "watch"]).leanctx_command,
            "watch",
        )
        self.assertEqual(
            parser.parse_args(["leanctx", "list"]).leanctx_command,
            "list",
        )

    def test_leanctx_list_marks_newest_run_for_current_project(self) -> None:
        project = self.root / "project"
        project.mkdir()
        current = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        older = LeanctxRun(
            "run.older",
            self.root / "data" / "state" / "sessions" / "run.older",
            project,
            "2026-07-26T10:00:00Z",
            False,
        )
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(current, older),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "list")

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("RUN", stdout)
        self.assertIn("PROJECT", stdout)
        self.assertIn("run.current", stdout)
        current_row = next(
            line for line in stdout.splitlines() if "run.current" in line
        )
        older_row = next(
            line for line in stdout.splitlines() if "run.older" in line
        )
        self.assertIn("yes", current_row)
        self.assertIn("—", older_row)

    def test_leanctx_stats_selects_project_and_renders_exact_savings(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        selected = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        binary = self.root / "data" / "bin" / "lean-ctx"
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(selected,),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "managed_binary",
                return_value=binary,
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_stats",
                return_value=LeanctxStats(
                    4,
                    14261,
                    1590,
                    12671,
                    88.85,
                ),
            ) as read,
        ):
            status, stdout, stderr = self.run_cli("leanctx", "stats")

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("run.current", stdout)
        self.assertIn("14,261", stdout)
        self.assertIn("12,671", stdout)
        self.assertIn("88.9%", stdout)
        read.assert_called_once_with(binary, selected)

    def test_leanctx_dashboard_propagates_selected_options_and_status(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        selected = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        binary = self.root / "data" / "bin" / "lean-ctx"
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(selected,),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "managed_binary",
                return_value=binary,
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "run_dashboard",
                return_value=7,
            ) as dashboard,
        ):
            status, stdout, stderr = self.run_cli(
                "leanctx",
                "dashboard",
                "--run",
                "run.current",
                "--port",
                "3341",
                "--open",
                "none",
            )

        self.assertEqual((status, stdout, stderr), (7, "", ""))
        dashboard.assert_called_once_with(
            binary,
            selected,
            self.root / "data" / "state",
            port=3341,
            open_mode="none",
        )

    def test_leanctx_implicit_selection_error_is_concise(self) -> None:
        project = self.root / "project"
        project.mkdir()
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "stats")

        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(
            stderr,
            "ERROR: current project has no LeanCTX activity; "
            "run 'orichum leanctx list' to inspect available runs\n",
        )

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

    def test_session_claudex_ports_are_distinct_and_exclude_services(self) -> None:
        state = self.root / "data" / "state"
        state.mkdir(parents=True, mode=0o700)
        first_run = state / "sessions" / "run.first"
        second_run = state / "sessions" / "run.second"
        first_run.mkdir(parents=True, mode=0o700)
        second_run.mkdir(mode=0o700)

        first = orichum_cli._reserve_session_claudex_port(
            state,
            first_run,
            "oc-s-0000000000000001",
            13456,
            frozenset({8317, 8787, 13457}),
        )
        second = orichum_cli._reserve_session_claudex_port(
            state,
            second_run,
            "oc-s-0000000000000002",
            13456,
            frozenset({8317, 8787, 13457}),
        )

        self.assertNotEqual(first, second)
        self.assertNotIn(first, {8317, 8787, 13457})
        self.assertNotIn(second, {8317, 8787, 13457})
        self.assertEqual(
            (first_run / "claudex-proxy-port").read_text(
                encoding="ascii"
            ),
            f"{first}\n",
        )
        self.assertEqual(
            (second_run / "claudex-proxy-port").read_text(
                encoding="ascii"
            ),
            f"{second}\n",
        )

    def test_session_claudex_config_isolates_proxy_and_restores_user_env(
        self,
    ) -> None:
        source = self.root / "shared.toml"
        source.write_text(
            "\n".join(
                (
                    'claude_binary = "/usr/bin/claude"',
                    "proxy_port = 13456",
                    "",
                    "[[profiles]]",
                    'name = "gpt"',
                    "",
                    "[profiles.custom_headers]",
                    'X-Orichum-Session-ID = "unbound"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        source.chmod(0o600)
        run_dir = self.root / "run"
        run_dir.mkdir(mode=0o700)
        prepared = SimpleNamespace(
            logical=SimpleNamespace(id="oc-s-0000000000000001"),
            physical=SimpleNamespace(run_dir=run_dir),
        )

        output = orichum_cli._materialize_session_claudex_config(
            source,
            prepared,
            14567,
            {
                "HOME": "/Users/example",
                "XDG_CACHE_HOME": "/var/cache/example",
                "XDG_RUNTIME_DIR": "/var/run/example",
            },
        )

        rendered = output.read_text(encoding="utf-8")
        self.assertIn("proxy_port = 14567", rendered)
        self.assertNotIn("proxy_port = 13456", rendered)
        self.assertIn(
            'X-Orichum-Session-ID = "oc-s-0000000000000001"',
            rendered,
        )
        self.assertIn("[profiles.extra_env]", rendered)
        self.assertIn('HOME = "/Users/example"', rendered)
        self.assertIn(
            'XDG_CACHE_HOME = "/var/cache/example"', rendered
        )
        self.assertIn(
            'XDG_RUNTIME_DIR = "/var/run/example"', rendered
        )
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_runtime_verifier_timeout_is_reported_as_cli_error(self) -> None:
        with mock.patch.object(
            orichum_cli.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(
                ["orichum-runtime-ready"], 30
            ),
        ):
            with self.assertRaisesRegex(
                orichum_cli.CliError,
                "runtime health verification timed out",
            ):
                orichum_cli._verify_runtime(
                    {
                        "data": self.root / "data",
                        "config": self.root / "config",
                    }
                )

    def test_live_model_catalogue_uses_verified_cliproxy_endpoint(self) -> None:
        response = SimpleNamespace(
            status=200,
            read=lambda _maximum: json.dumps(
                {"data": [{"id": "gpt-5.6-sol"}]}
            ).encode("utf-8"),
        )
        connection = mock.MagicMock()
        connection.getresponse.return_value = response
        with (
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "claudexProxyPort": 13457,
                    "cliproxyPort": 8317,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli.http.client,
                "HTTPConnection",
                return_value=connection,
            ) as connect,
        ):
            models = orichum_cli._live_models(
                {"data": self.root / "data"}
            )
        connect.assert_called_once_with("127.0.0.1", 8317, timeout=3)
        self.assertEqual(models, frozenset({"gpt-5.6-sol"}))

    def test_missing_live_models_reports_roles_without_routing_prefixes(
        self,
    ) -> None:
        def route(model: str, upstream: str) -> SimpleNamespace:
            return SimpleNamespace(
                logical_model=model,
                upstream_model=upstream,
            )

        controller = SimpleNamespace(
            primary=route(
                "gpt-5.6-sol",
                "oc-r-1111111111111111/gpt-5.6-sol",
            ),
            fallbacks=(),
        )
        worker = SimpleNamespace(
            primary=route(
                "gpt-5.6-terra",
                "oc-r-2222222222222222/gpt-5.6-terra",
            ),
            fallbacks=(),
        )
        agents = {role: worker for role in orichum_cli.ROLES}

        def prepare(*_args: object, **_kwargs: object) -> object:
            orichum_cli._validate_live_models(
                {},
                controller,
                agents,
                available=frozenset(),
            )
            raise AssertionError("unreachable")

        with mock.patch.object(
            orichum_cli, "_prepare_new_session", side_effect=prepare
        ):
            status, stdout, stderr = self.run_cli("run")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("controller", stderr)
        self.assertIn("gpt-5.6-sol", stderr)
        self.assertIn("repository-explorer", stderr)
        self.assertNotIn("oc-r-", stderr)

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
        physical.run_dir.mkdir(mode=0o700)
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        managed_python = (
            paths["data"] / "python" / "cpython-3.14.6" / "bin" / "python3.14"
        )
        managed_python.parent.mkdir(mode=0o700, parents=True)
        managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_python.chmod(0o700)
        (paths["data"] / "bin").mkdir(mode=0o700)
        (paths["data"] / "bin" / "orichum-python").symlink_to(managed_python)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HOME": "/Users/example",
                    "XDG_CACHE_HOME": "/var/cache/example",
                    "XDG_RUNTIME_DIR": "/var/run/example",
                    "GH_TOKEN": "wrong-account",
                    "GITHUB_TOKEN": "wrong-account",
                    "GH_HOST": "enterprise.example",
                    "ORICHUM_PYTHON": "/tmp/caller-python",
                    "ORICHUM_PYTHON_VALIDATED": "/tmp/caller-python",
                },
                clear=False,
            ),
            mock.patch.object(
                orichum_cli.sys, "executable", str(managed_python)
            ),
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
        self.assertEqual(
            environment["HOME"],
            str(physical.run_dir / "claudex-home"),
        )
        self.assertEqual(
            environment["XDG_CACHE_HOME"],
            str(physical.run_dir / "claudex-home" / "cache"),
        )
        self.assertEqual(
            environment["XDG_RUNTIME_DIR"],
            str(physical.run_dir / "claudex-home" / "runtime"),
        )
        self.assertEqual(
            environment["ORICHUM_PYTHON"],
            str(paths["data"] / "bin" / "orichum-python"),
        )
        self.assertEqual(
            environment["ORICHUM_PYTHON_VALIDATED"],
            environment["ORICHUM_PYTHON"],
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
        physical.run_dir.mkdir(mode=0o700)
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        managed_python = (
            paths["data"] / "python" / "cpython-3.14.6" / "bin" / "python3.14"
        )
        managed_python.parent.mkdir(mode=0o700, parents=True)
        managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_python.chmod(0o700)
        (paths["data"] / "bin").mkdir(mode=0o700)
        (paths["data"] / "bin" / "orichum-python").symlink_to(managed_python)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "caller-token",
                    "GH_HOST": "github.example",
                    "GH_CONFIG_DIR": "/caller/github-config",
                },
                clear=False,
            ),
            mock.patch.object(
                orichum_cli.sys, "executable", str(managed_python)
            ),
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


class OrichumGraphCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data_root = self.root / "data"
        self.data_root.mkdir(mode=0o700)
        self.home = self.root / "home"
        skill = self.home / ".agents" / "skills" / "graphify"
        skill.mkdir(parents=True)
        (skill / ".graphify_version").write_text(
            "1.2.2\n", encoding="ascii"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.graphify = self.bin / "graphify"
        self.graphify.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

if sys.argv[1:] == ["--version"]:
    print("graphify 1.2.3")
    raise SystemExit()
if sys.argv[1:3] == ["hook", "status"]:
    print("installed")
    raise SystemExit()
repository = Path(sys.argv[2])
if (repository / "fail-graphify").exists():
    print("x" * 10000, file=sys.stderr)
    raise SystemExit(7)
output = Path(os.environ["GRAPHIFY_OUT"])
output.mkdir(parents=True, exist_ok=True)
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
(output / "graph.json").write_text(
    json.dumps({
        "built_at_commit": commit,
        "nodes": [{"id": repository.name, "source_file": "source.py"}],
        "links": [],
    }),
    encoding="utf-8",
)
""",
            encoding="utf-8",
        )
        self.graphify.chmod(0o755)
        self.environment = {
            "HOME": str(self.home),
            "ORICHUM_DATA_HOME": str(self.data_root),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.repository = self.make_repository(
            self.project_root / "api", "api"
        )
        self.web_repository = self.make_repository(
            self.project_root / "web", "web"
        )

    def make_repository(self, path: Path, name: str) -> Path:
        path.mkdir()
        self.git(path, "init", "-q")
        self.git(path, "config", "user.email", "tests@example.invalid")
        self.git(path, "config", "user.name", "Graph CLI tests")
        (path / "source.py").write_text(
            f"print({name!r})\n", encoding="utf-8"
        )
        self.git(path, "add", "source.py")
        self.git(path, "commit", "-qm", "Initial commit")
        self.git(
            path,
            "remote",
            "add",
            "origin",
            f"https://github.com/example/{name}.git",
        )
        return path

    def git(self, repository: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_graph(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            mock.patch.object(
                graph_manager.Path, "cwd", return_value=self.project_root
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = graph_manager.graph_main(list(arguments))
        return status, stdout.getvalue(), stderr.getvalue()

    def snapshot_tree(self, root: Path) -> tuple[tuple[str, str, bytes], ...]:
        snapshot = []
        if not root.exists():
            return ()
        for path in sorted(root.rglob("*")):
            kind = "d" if path.is_dir() else "f"
            content = path.read_bytes() if path.is_file() else b""
            snapshot.append((str(path.relative_to(root)), kind, content))
        return tuple(snapshot)

    def test_graph_path_syncs_automatic_scope_with_non_tty_progress(self) -> None:
        status, stdout, stderr = self.run_graph(str(self.project_root))

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("[discover] found 2 repositories", stdout)
        self.assertIn("[graphify 2/2]", stdout)
        self.assertNotIn("\r", stdout)

    def test_graph_defaults_to_current_directory(self) -> None:
        status, stdout, stderr = self.run_graph()

        self.assertEqual(status, 0, stderr)
        self.assertIn("[discover] found 2 repositories", stdout)

    def test_graph_path_repairs_stale_target_and_reinstalls_hooks(self) -> None:
        status, _, stderr = self.run_graph(str(self.repository))
        self.assertEqual(status, 0, stderr)
        target = graph_manager.resolve_graph_target(
            self.repository, self.data_root
        )
        metadata = json.loads(
            target.metadata_file.read_text(encoding="utf-8")
        )
        metadata["built_at_commit"] = "0" * 40
        target.metadata_file.write_text(
            json.dumps(metadata), encoding="utf-8"
        )
        post_commit = self.repository / ".git" / "hooks" / "post-commit"
        post_commit.unlink()
        self.assertEqual(
            graph_manager.graph_hook_status(self.repository), "missing"
        )

        status, stdout, stderr = self.run_graph(str(self.repository))

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("[graphify 1/1] updated api", stdout)
        self.assertEqual(graph_manager.inspect_graph(target).status, "current")
        self.assertEqual(
            graph_manager.graph_hook_status(self.repository), "installed"
        )

    def test_graph_rejects_invalid_path_and_accepts_empty_scope(self) -> None:
        status, stdout, stderr = self.run_graph(str(self.root / "missing"))
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("path is not a directory", stderr)

        empty = self.root / "empty"
        empty.mkdir()
        status, stdout, stderr = self.run_graph(str(empty))
        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("[discover] found 0 repositories", stdout)

    def test_graph_failure_is_nonzero_and_diagnostics_are_bounded(self) -> None:
        (self.web_repository / "fail-graphify").touch()

        status, stdout, stderr = self.run_graph(str(self.project_root))

        self.assertEqual(status, 2)
        self.assertIn("[graphify 1/2]", stdout)
        self.assertIn("[graphify 2/2]", stdout)
        self.assertIn("Graphify failed with exit code 7", stderr)
        self.assertLess(len(stderr), 4_000)
        self.assertTrue(
            any(self.data_root.rglob("api/revisions/*/graphify-out/graph.json"))
        )

    def test_graph_status_is_read_only_and_reports_version_drift(self) -> None:
        (self.repository / "dirty.py").write_text(
            "print('dirty')\n", encoding="utf-8"
        )
        before_data = self.snapshot_tree(self.data_root)
        before_repository = self.snapshot_tree(self.repository)

        status, stdout, stderr = self.run_graph(
            "status", str(self.repository)
        )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("REPOSITORY", stdout)
        self.assertIn("github.com/example/api", stdout)
        self.assertIn(str(self.repository), stdout)
        self.assertIn("Graphify package: 1.2.3", stdout)
        self.assertIn("Graphify skill: 1.2.2 (drift)", stdout)
        self.assertEqual(self.snapshot_tree(self.data_root), before_data)
        self.assertEqual(self.snapshot_tree(self.repository), before_repository)

    def test_graph_status_details_clean_repository_with_invalid_graph(
        self,
    ) -> None:
        status, _, stderr = self.run_graph(str(self.repository))
        self.assertEqual(status, 0, stderr)
        target = graph_manager.resolve_graph_target(
            self.repository, self.data_root
        )
        target.graph_file.write_text("{}", encoding="utf-8")
        before_data = self.snapshot_tree(self.data_root)
        before_repository = self.snapshot_tree(self.repository)

        status, stdout, stderr = self.run_graph(
            "status", str(self.repository)
        )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertIn("invalid", stdout)
        self.assertIn(f"  checkout: {self.repository}", stdout)
        self.assertEqual(self.snapshot_tree(self.data_root), before_data)
        self.assertEqual(self.snapshot_tree(self.repository), before_repository)

    def test_graph_identity_set_clear_and_validation(self) -> None:
        status, _, stderr = self.run_graph(
            "identity",
            str(self.repository),
            "--set",
            "git.example.com/platform/api",
        )
        self.assertEqual(status, 0, stderr)
        configured = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "--local",
                "--get",
                "orichum.repositoryIdentity",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            configured.stdout.strip(), "git.example.com/platform/api"
        )

        status, _, stderr = self.run_graph(
            "identity", str(self.repository), "--clear"
        )
        self.assertEqual(status, 0, stderr)
        configured = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "--local",
                "--get",
                "orichum.repositoryIdentity",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(configured.returncode, 0)

        status, _, stderr = self.run_graph(
            "identity", str(self.repository)
        )
        self.assertEqual(status, 2)
        self.assertIn("requires exactly one of --set ID or --clear", stderr)

        status, _, stderr = self.run_graph(
            "identity",
            str(self.repository),
            "--set",
            "git.example.com/platform/api",
            "--clear",
        )
        self.assertEqual(status, 2)
        self.assertIn("requires exactly one of --set ID or --clear", stderr)


if __name__ == "__main__":
    unittest.main()
