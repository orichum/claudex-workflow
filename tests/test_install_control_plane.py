#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations.common.account_registry import Account, update_accounts
from integrations.common.install_control_plane import (
    InstallControlPlaneError,
    activate,
    rollback,
    stage,
)


class InstallControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = Path(__file__).resolve().parents[1]
        self.installed = self.root / "installed"
        self.candidate = self.root / "candidate"
        self.installed.mkdir(mode=0o700)
        for name in (
            "model-stacks.json",
            "projects.json",
            "providers.json",
            "plugins.json",
            "runtime.json",
            "controller-policy.md",
        ):
            destination = self.installed / name
            destination.write_bytes(
                (self.repository / "config" / name).read_bytes()
            )
            destination.chmod(0o600)
        self.original_model = (
            self.installed / "model-stacks.json"
        ).read_bytes()
        stage(self.repository, self.installed, self.candidate)

    def _assert_original_state(self) -> None:
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            self.original_model,
        )
        self.assertFalse((self.installed / "stack-bindings.json").exists())
        self.assertFalse((self.installed / "accounts.json").exists())
        self.assertFalse(
            any(
                self.installed.glob(".model-stacks.transaction*")
            )
        )

    def test_rollback_is_idempotent_across_activation_interruptions(
        self,
    ) -> None:
        for checkpoint in (
            "prepared",
            "bootstrap:accounts.json",
            "stack-saved",
            "committed",
        ):
            with self.subTest(checkpoint=checkpoint):
                snapshot = self.root / f"snapshot-{checkpoint.replace(':', '-')}"

                def interrupt(observed: str) -> None:
                    if observed == checkpoint:
                        raise KeyboardInterrupt(checkpoint)

                with mock.patch(
                    "integrations.common.install_control_plane."
                    "_activation_checkpoint",
                    side_effect=interrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        activate(
                            self.candidate,
                            self.installed,
                            snapshot,
                        )
                rollback(self.installed, snapshot)
                rollback(self.installed, snapshot)
                self._assert_original_state()

    def test_rollback_before_manifest_is_a_safe_noop(self) -> None:
        snapshot = self.root / "pre-manifest-snapshot"
        snapshot.mkdir(mode=0o700)
        rollback(self.installed, snapshot)
        self._assert_original_state()

    def test_rollback_preserves_concurrently_updated_bootstrap_account(
        self,
    ) -> None:
        snapshot = self.root / "account-snapshot"
        activate(self.candidate, self.installed, snapshot)
        account = Account(
            id="oc-a-1111111111111111",
            name="concurrent",
            provider="openai",
            credential_ref="concurrent.json",
            pool="shared",
            routing_prefix="oc-r-1111111111111111",
            priority=100,
            state="active",
            original_prefix=None,
            original_priority=None,
        )
        update_accounts(
            self.installed / "accounts.json",
            lambda current: (*current, account),
        )

        with self.assertRaisesRegex(
            InstallControlPlaneError,
            "accounts.json changed after installer activation",
        ):
            rollback(self.installed, snapshot)

        accounts = json.loads(
            (self.installed / "accounts.json").read_text()
        )["accounts"]
        self.assertEqual([entry["id"] for entry in accounts], [account.id])
        self.assertEqual(
            (self.installed / "model-stacks.json").read_bytes(),
            self.original_model,
        )
        manifest = json.loads(
            (snapshot / "installed-control-plane.json").read_text()
        )
        self.assertEqual(manifest["phase"], "rollbackConflict")
        self.assertEqual(manifest["conflicts"], ["accounts.json"])


if __name__ == "__main__":
    unittest.main()
