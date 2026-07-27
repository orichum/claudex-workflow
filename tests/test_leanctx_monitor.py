#!/usr/bin/env python3
"""Focused tests for project-aware LeanCTX monitoring."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common import leanctx_monitor
from integrations.common.leanctx_monitor import (
    LeanctxMonitorError,
    LeanctxRun,
    discover_runs,
    leanctx_environment,
    read_stats,
    select_run,
)
from integrations.common.model_routing import EffectiveStack, ROLES
from integrations.common.session_config import create_resolved_session


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class LeanctxMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data_root = self.root / "data"
        self.data_root.mkdir(mode=0o700)
        self.projects = self.root / "projects"
        self.xebia = self.projects / "xebia"
        self.complion = self.projects / "complion"
        self.xebia.mkdir(parents=True)
        self.complion.mkdir()
        binary_dir = self.data_root / "bin"
        binary_dir.mkdir(mode=0o700)
        self.binary = binary_dir / "lean-ctx"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.effective = EffectiveStack(
            "balanced",
            "route/controller",
            {role: (f"route/{role}",) for role in ROLES},
            {role: f"route/{role}" for role in ROLES},
        )

    def create_run(
        self,
        project: Path,
        *,
        context_root: Path | None = None,
    ):
        context_root = project if context_root is None else context_root
        context = {
            "schemaVersion": 1,
            "launchDirReal": str(project),
            "repoRootReal": str(project),
            "route": {
                "id": project.name,
                "contextRootReal": str(context_root),
                "dockerProfile": None,
                "modelStack": None,
                "memoryWing": project.name,
                "memoryAvailable": False,
                "memoryFailureCode": "not-configured",
                "palacePathReal": None,
            },
        }
        return create_resolved_session(
            REPOSITORY_ROOT,
            data_root=self.data_root,
            context=context,
            effective=self.effective,
            plugin_source=REPOSITORY_ROOT / "controller" / "plugin",
        )

    @staticmethod
    def descriptor(session, project: Path) -> LeanctxRun:
        return LeanctxRun(
            run_id=session.run_id,
            run_dir=session.run_dir,
            project_root=project,
            created_at="2026-07-27T10:00:00Z",
            has_activity=False,
        )

    def test_discovers_only_verified_leanctx_runs(self) -> None:
        session = self.create_run(self.xebia)
        incomplete = session.run_dir.parent / "run.incomplete"
        incomplete.mkdir(mode=0o700)

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)

        self.assertEqual([run.run_id for run in runs], [session.run_id])
        self.assertEqual(runs[0].project_root, self.xebia)

    def test_completed_run_without_leanctx_is_reported_as_unattached(
        self,
    ) -> None:
        legacy = self.create_run(self.complion)
        for path in sorted(
            (legacy.run_dir / "leanctx").rglob("*"),
            reverse=True,
        ):
            path.unlink() if path.is_file() else path.rmdir()
        (legacy.run_dir / "leanctx").rmdir()
        current = self.create_run(self.xebia)

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)

        self.assertEqual(
            {run.run_id for run in runs},
            {legacy.run_id, current.run_id},
        )
        observed = next(
            run for run in runs if run.run_id == legacy.run_id
        )
        self.assertFalse(observed.attached)
        self.assertFalse(observed.has_activity)

    def test_existing_leanctx_directory_without_config_is_unattached(
        self,
    ) -> None:
        session = self.create_run(self.xebia)
        (session.run_dir / "leanctx" / "config.toml").unlink()

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)

        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0].attached)

    def test_tampered_leanctx_config_is_unattached(self) -> None:
        session = self.create_run(self.xebia)
        config = session.run_dir / "leanctx" / "config.toml"
        config.write_text("tools_enabled = []\n", encoding="utf-8")
        config.chmod(0o600)

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)

        self.assertEqual(len(runs), 1)
        self.assertFalse(runs[0].attached)

    def test_implicit_selection_uses_newest_exact_project(self) -> None:
        older = LeanctxRun(
            "run.older",
            self.root / "run.older",
            self.xebia,
            "2026-07-26T10:00:00Z",
            True,
        )
        newer = LeanctxRun(
            "run.newer",
            self.root / "run.newer",
            self.xebia,
            "2026-07-27T10:00:00Z",
            True,
        )
        other = LeanctxRun(
            "run.other",
            self.root / "run.other",
            self.complion,
            "2026-07-27T11:00:00Z",
            True,
        )

        selected = select_run((older, other, newer), self.xebia, None)

        self.assertEqual(selected.run_id, "run.newer")

    def test_implicit_selection_prefers_newest_run_even_without_activity(
        self,
    ) -> None:
        active = LeanctxRun(
            "run.active",
            self.root / "run.active",
            self.xebia,
            "2026-07-26T10:00:00Z",
            True,
        )
        inactive = LeanctxRun(
            "run.inactive",
            self.root / "run.inactive",
            self.xebia,
            "2026-07-27T10:00:00Z",
            False,
        )

        selected = select_run((inactive, active), self.xebia, None)

        self.assertEqual(selected.run_id, "run.inactive")

    def test_current_run_wins_even_before_it_records_activity(self) -> None:
        active = LeanctxRun(
            "run.active",
            self.root / "run.active",
            self.xebia,
            "2026-07-27T10:00:00Z",
            True,
        )
        current = LeanctxRun(
            "run.current",
            self.root / "run.current",
            self.xebia,
            "2026-07-26T10:00:00Z",
            False,
        )

        selected = select_run(
            (active, current),
            self.xebia,
            None,
            current_run_id="run.current",
        )

        self.assertEqual(selected.run_id, "run.current")

    def test_unattached_run_is_rejected_with_explicit_remediation(
        self,
    ) -> None:
        run = LeanctxRun(
            "run.unattached",
            self.root / "run.unattached",
            self.xebia,
            "2026-07-27T10:00:00Z",
            False,
            attached=False,
        )

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "LeanCTX is not attached to run run.unattached; "
            "rerun install.sh and start a new Orichum session",
        ):
            leanctx_monitor.require_attached(run)

    def test_implicit_selection_never_crosses_projects(self) -> None:
        other = LeanctxRun(
            "run.other",
            self.root / "run.other",
            self.complion,
            "2026-07-27T11:00:00Z",
            True,
        )

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "current project has no LeanCTX activity",
        ):
            select_run((other,), self.xebia, None)

    def test_repository_runs_remain_distinct_below_one_context_root(
        self,
    ) -> None:
        first = self.xebia / "first"
        second = self.xebia / "second"
        first.mkdir()
        second.mkdir()
        first_session = self.create_run(first, context_root=self.xebia)
        second_session = self.create_run(second, context_root=self.xebia)

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)
        first_run = next(
            run for run in runs if run.run_id == first_session.run_id
        )
        second_run = next(
            run for run in runs if run.run_id == second_session.run_id
        )

        self.assertEqual(first_run.project_root, first)
        self.assertEqual(second_run.project_root, second)
        self.assertEqual(
            select_run(runs, first, None).run_id,
            first_session.run_id,
        )

    def test_same_second_selection_uses_completion_nanoseconds(self) -> None:
        older = LeanctxRun(
            "run.zzz",
            self.root / "run.zzz",
            self.xebia,
            "2026-07-27T10:00:00Z",
            True,
            created_at_ns=1,
        )
        newer = LeanctxRun(
            "run.aaa",
            self.root / "run.aaa",
            self.xebia,
            "2026-07-27T10:00:00Z",
            True,
            created_at_ns=2,
        )

        selected = select_run((older, newer), self.xebia, None)

        self.assertEqual(selected.run_id, "run.aaa")

    def test_explicit_selection_rejects_path_syntax(self) -> None:
        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "run identifier is invalid",
        ):
            select_run((), self.xebia, "../run.other")

    def test_explicit_selection_can_select_another_project(self) -> None:
        other = LeanctxRun(
            "run.other",
            self.root / "run.other",
            self.complion,
            "2026-07-27T11:00:00Z",
            True,
        )

        selected = select_run((other,), self.xebia, "run.other")

        self.assertEqual(selected, other)

    def test_completed_tampered_run_is_rejected(self) -> None:
        session = self.create_run(self.xebia)
        session.context_file.write_text("{}", encoding="utf-8")
        session.context_file.chmod(0o600)

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "completed LeanCTX run is invalid",
        ):
            discover_runs(REPOSITORY_ROOT, self.data_root)

    def test_historical_leanctx_contract_is_listed_as_unattached(self) -> None:
        session = self.create_run(self.xebia)
        config = session.run_dir / "leanctx" / "config.toml"
        config.write_text(
            'tools_enabled = ["ctx_read"]\n',
            encoding="utf-8",
        )
        config.chmod(0o600)

        runs = discover_runs(REPOSITORY_ROOT, self.data_root)

        self.assertEqual([run.run_id for run in runs], [session.run_id])
        self.assertFalse(runs[0].attached)

    def test_symlinked_run_is_rejected(self) -> None:
        sessions = self.data_root / "state" / "sessions"
        sessions.mkdir(parents=True, mode=0o700)
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        (sessions / "run.link").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "completed LeanCTX run is invalid",
        ):
            discover_runs(REPOSITORY_ROOT, self.data_root)

    def test_environment_points_all_stores_at_selected_run(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)

        environment = leanctx_environment(run, {"PATH": "/bin"})

        self.assertEqual(environment["PATH"], "/bin")
        for name in (
            "LEAN_CTX_CACHE_DIR",
            "LEAN_CTX_CONFIG_DIR",
            "LEAN_CTX_DATA_DIR",
            "LEAN_CTX_STATE_DIR",
        ):
            self.assertEqual(
                environment[name],
                str(session.run_dir / "leanctx"),
            )
        self.assertEqual(
            environment["LEAN_CTX_PROJECT_ROOT"],
            str(self.xebia),
        )

    def test_read_stats_uses_native_json_and_computes_exact_savings(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "total_commands": 4,
                    "total_input_tokens": 14261,
                    "total_output_tokens": 1590,
                }
            ),
            stderr="",
        )
        with mock.patch.object(
            leanctx_monitor.subprocess,
            "run",
            return_value=completed,
        ) as invoked:
            stats = read_stats(self.binary, run, {"PATH": "/bin"})

        self.assertEqual(
            invoked.call_args.args[0],
            [str(self.binary), "stats", "json"],
        )
        self.assertEqual(stats.total_commands, 4)
        self.assertEqual(stats.saved_tokens, 12671)
        self.assertAlmostEqual(stats.savings_percent, 88.85, places=2)

    def test_read_stats_rejects_invalid_native_values(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "total_commands": True,
                    "total_input_tokens": 10,
                    "total_output_tokens": 2,
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(
                leanctx_monitor.subprocess,
                "run",
                return_value=completed,
            ),
            self.assertRaisesRegex(
                LeanctxMonitorError,
                "statistics are invalid",
            ),
        ):
            read_stats(self.binary, run)

    def test_read_stats_preserves_bounded_native_failure(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        completed = SimpleNamespace(
            returncode=7,
            stdout="",
            stderr="native failure\n",
        )
        with (
            mock.patch.object(
                leanctx_monitor.subprocess,
                "run",
                return_value=completed,
            ),
            self.assertRaisesRegex(
                LeanctxMonitorError,
                "native failure",
            ),
        ):
            read_stats(self.binary, run)

    def test_managed_binary_uses_only_the_orichum_data_directory(self) -> None:
        self.assertEqual(
            leanctx_monitor.managed_binary(self.data_root),
            self.binary,
        )

    def test_managed_binary_rejects_a_symlink(self) -> None:
        self.binary.unlink()
        external = self.root / "external-leanctx"
        external.write_text("#!/bin/sh\n", encoding="utf-8")
        external.chmod(0o755)
        self.binary.symlink_to(external)

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "managed LeanCTX is unavailable",
        ):
            leanctx_monitor.managed_binary(self.data_root)

    def test_first_free_port_skips_an_occupied_loopback_port(self) -> None:
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        start = occupied.getsockname()[1]
        if start == 65535:
            self.skipTest("ephemeral port leaves no search range")

        selected = leanctx_monitor.first_free_loopback_port(start)

        self.assertGreater(selected, start)
        probe = socket.socket()
        self.addCleanup(probe.close)
        probe.bind(("127.0.0.1", selected))

    def test_watch_runs_the_native_tui_in_the_foreground(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        with mock.patch.object(
            leanctx_monitor.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=7),
        ) as invoked:
            status = leanctx_monitor.run_watch(self.binary, run)

        self.assertEqual(status, 7)
        self.assertEqual(
            invoked.call_args.args[0],
            [str(self.binary), "watch"],
        )
        self.assertFalse(invoked.call_args.kwargs.get("capture_output", False))

    def test_dashboard_is_local_authenticated_and_config_isolated(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        source_config = session.run_dir / "leanctx" / "config.toml"
        original = source_config.read_bytes()
        captured: dict[str, object] = {}

        def inspect_call(command, **kwargs):
            environment = kwargs["env"]
            private_config = Path(environment["LEAN_CTX_CONFIG_DIR"])
            captured["command"] = command
            captured["environment"] = environment
            captured["config_dir"] = private_config
            captured["config"] = (private_config / "config.toml").read_bytes()
            return SimpleNamespace(returncode=0)

        with (
            mock.patch.dict(
                os.environ,
                {
                    "LEAN_CTX_DASHBOARD_AUTH": "false",
                    "LEAN_CTX_HTTP_TOKEN": "inherited-token",
                    "LEAN_CTX_DASHBOARD_ALLOWED_HOSTS": "0.0.0.0",
                },
                clear=False,
            ),
            mock.patch.object(
                leanctx_monitor,
                "first_free_loopback_port",
                return_value=3341,
            ),
            mock.patch.object(
                leanctx_monitor.subprocess,
                "run",
                side_effect=inspect_call,
            ),
        ):
            status = leanctx_monitor.run_dashboard(
                self.binary,
                run,
                self.data_root / "state",
                port=None,
                open_mode="none",
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            captured["command"],
            [
                str(self.binary),
                "dashboard",
                "--host=127.0.0.1",
                "--port=3341",
                "--open=none",
            ],
        )
        environment = captured["environment"]
        self.assertEqual(environment["LEAN_CTX_DASHBOARD_AUTH"], "true")
        self.assertNotIn("LEAN_CTX_HTTP_TOKEN", environment)
        self.assertNotIn("LEAN_CTX_DASHBOARD_ALLOWED_HOSTS", environment)
        self.assertEqual(captured["config"], original)
        self.assertFalse(Path(captured["config_dir"]).exists())
        self.assertEqual(source_config.read_bytes(), original)

    def test_dashboard_rejects_an_occupied_explicit_port(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))

        with self.assertRaisesRegex(
            LeanctxMonitorError,
            "port is already occupied",
        ):
            leanctx_monitor.run_dashboard(
                self.binary,
                run,
                self.data_root / "state",
                port=occupied.getsockname()[1],
                open_mode="none",
            )

    def test_dashboard_cleans_up_after_interrupt(self) -> None:
        session = self.create_run(self.xebia)
        run = self.descriptor(session, self.xebia)
        captured: dict[str, Path] = {}

        def interrupt(_command, **kwargs):
            captured["config_dir"] = Path(
                kwargs["env"]["LEAN_CTX_CONFIG_DIR"]
            )
            raise KeyboardInterrupt

        with (
            mock.patch.object(
                leanctx_monitor,
                "first_free_loopback_port",
                return_value=3341,
            ),
            mock.patch.object(
                leanctx_monitor.subprocess,
                "run",
                side_effect=interrupt,
            ),
        ):
            status = leanctx_monitor.run_dashboard(
                self.binary,
                run,
                self.data_root / "state",
                port=None,
                open_mode="browser",
            )

        self.assertEqual(status, 130)
        self.assertFalse(captured["config_dir"].exists())


if __name__ == "__main__":
    unittest.main()
