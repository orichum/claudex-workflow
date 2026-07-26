#!/usr/bin/env python3
"""Tests for the compact, session-aware Orichum status line."""

import unittest
import json
import io
import tempfile
from pathlib import Path

from integrations.common.account_registry import Account
from integrations.common.model_routing import ROLES
from integrations.common.orichum_sessions import LogicalSession, RouteBinding
from integrations.common.route_selection import Route


class OrichumStatusTests(unittest.TestCase):
    def test_controller_settings_enable_the_orichum_status_line(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        settings = json.loads(
            (repository / "controller" / "settings.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("statusLine", settings)
        self.assertEqual(
            settings["statusLine"],
            {
                "type": "command",
                "command": (
                    '"$CLAUDEX_WORKFLOW_ROOT/bin/orichum-statusline"'
                ),
                "padding": 0,
            },
        )
        self.assertTrue(
            (repository / "bin" / "orichum-statusline").is_file()
        )

    def test_main_degrades_to_orichum_identity_when_state_is_unavailable(
        self,
    ) -> None:
        from integrations.common.orichum_status import main

        output = io.StringIO()
        result = main(
            input_stream=io.StringIO("{}"),
            output_stream=output,
            environment={},
        )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "ORICHUM │ status unavailable\n")

    def test_main_does_not_leak_invalid_local_state_errors(self) -> None:
        from integrations.common.orichum_status import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            result = main(
                input_stream=io.StringIO("{}"),
                output_stream=output,
                environment={
                    "ORICHUM_SESSION_ID": "oc-s-0000000000000001",
                    "ORICHUM_STATE_HOME": str(root / "missing-state"),
                    "ORICHUM_CONFIG_HOME": str(root / "missing-config"),
                    "ORICHUM_DATA_HOME": str(root / "missing-data"),
                    "NO_COLOR": "1",
                },
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "ORICHUM │ status unavailable\n")

    def test_renders_active_fallback_account_and_claude_metrics(self) -> None:
        try:
            from integrations.common.orichum_status import render_status
        except ImportError as error:
            self.fail(f"Orichum status renderer is missing: {error}")

        primary = Route(
            account_id="oc-a-0000000000000001",
            provider="anthropic",
            family="claude",
            logical_model="claude-opus-4-8",
            upstream_model="oc-r-0000000000000001/claude-opus-4-8",
            claudex_profile="ocp-0000000000000001",
            priority=10,
            pool="claude",
        )
        fallback = Route(
            account_id="oc-a-0000000000000002",
            provider="antigravity",
            family="claude",
            logical_model="claude-opus-4-8",
            upstream_model="oc-r-0000000000000002/claude-opus-4-8",
            claudex_profile="ocp-0000000000000002",
            priority=20,
            pool="claude",
        )
        binding = RouteBinding(primary=primary, fallbacks=(fallback,))
        session = LogicalSession(
            id="oc-s-0000000000000001",
            claude_session_id="00000000-0000-4000-8000-000000000001",
            parent_id=None,
            project_root=Path("/work/demo"),
            stack="balanced",
            controller=binding,
            agents={role: binding for role in ROLES},
            created_at="2026-07-27T00:00:00+00:00",
        )
        accounts = (
            Account(
                id=primary.account_id,
                name="Work Claude",
                provider=primary.provider,
                credential_ref="shared",
                pool="claude",
                routing_prefix="oc-r-0000000000000001",
                priority=10,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
            Account(
                id=fallback.account_id,
                name="Backup Claude",
                provider=fallback.provider,
                credential_ref="shared",
                pool="claude",
                routing_prefix="oc-r-0000000000000002",
                priority=20,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
        )
        payload = {
            "model": {
                "id": fallback.upstream_model,
                "display_name": "Opus 4.8",
            },
            "context_window": {"used_percentage": 41.2},
            "rate_limits": {
                "five_hour": {"used_percentage": 63},
                "seven_day": {"used_percentage": None},
            },
        }
        route_status = {
            "sessionId": session.id,
            "accountId": fallback.account_id,
            "provider": fallback.provider,
            "family": fallback.family,
            "logicalModel": fallback.logical_model,
            "routeState": "fallback",
            "reason": "retry",
        }

        self.assertEqual(
            render_status(
                payload,
                session,
                accounts,
                route_status=route_status,
                color=False,
            ),
            "ORICHUM │ demo │ balanced\n"
            "Claude · Opus 4.8 │ Backup Claude [fallback: rate limit] │ "
            "context 41% │ 5h 63% │ 7d —",
        )


if __name__ == "__main__":
    unittest.main()
