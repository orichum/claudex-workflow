#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from integrations.common.account_registry import Account, update_accounts
from integrations.common.stack_bindings import (
    StackBindingError,
    StackBindings,
    load_stack_bindings,
    save_stack_bindings,
    stack_binding_transaction,
    stack_binding_digest,
)


class StackBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "stack-bindings.json"

    def register_account(self, identifier: str) -> None:
        account = Account(
            id=identifier,
            name=identifier,
            provider="anthropic",
            credential_ref=f"{identifier}.json",
            pool="shared",
            routing_prefix=f"oc-r-{identifier.removeprefix('oc-a-')}",
            priority=100,
            state="active",
            original_prefix=None,
            original_priority=None,
        )
        update_accounts(
            self.root / "accounts.json", lambda accounts: (*accounts, account)
        )

    def rewrite_externally(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "candidateAccounts": {
                        "oc-c-2222222222222222": "oc-a-2222222222222222"
                    },
                }
            ),
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def test_round_trip_is_private_and_rename_independent(self):
        self.register_account("oc-a-1111111111111111")
        updated = save_stack_bindings(
            self.path,
            StackBindings(
                {"oc-c-1111111111111111": "oc-a-1111111111111111"}
            ),
            expected_digest=None,
        )
        self.assertEqual(
            updated.candidate_accounts["oc-c-1111111111111111"],
            "oc-a-1111111111111111",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(load_stack_bindings(self.path), updated)
        self.assertIsNotNone(stack_binding_digest(self.path))

    def test_transaction_is_reentrant_for_the_same_binding_path(self):
        with stack_binding_transaction(self.path) as outer:
            with stack_binding_transaction(self.path) as inner:
                self.assertIs(inner, outer)

    def test_digest_conflict_does_not_overwrite(self):
        original = load_stack_bindings(self.path)
        self.rewrite_externally()
        with self.assertRaisesRegex(StackBindingError, "changed during update"):
            save_stack_bindings(self.path, original, expected_digest="stale")
        self.assertEqual(
            load_stack_bindings(self.path).candidate_accounts,
            {"oc-c-2222222222222222": "oc-a-2222222222222222"},
        )

    def test_account_ids_are_canonical_and_registered(self) -> None:
        self.register_account("oc-a-1111111111111111")
        self.assertEqual(
            StackBindings(
                {"oc-c-1111111111111111": "oc-a-1111111111111111"}
            ).candidate_accounts["oc-c-1111111111111111"],
            "oc-a-1111111111111111",
        )
        for invalid in (
            "oc-a-account",
            "oc-a-111111111111111",
            "oc-a-111111111111111G",
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                StackBindingError, "account ID"
            ):
                StackBindings({"oc-c-1111111111111111": invalid})
        with self.assertRaisesRegex(StackBindingError, "not registered"):
            save_stack_bindings(
                self.path,
                StackBindings(
                    {"oc-c-1111111111111111": "oc-a-2222222222222222"}
                ),
                expected_digest=None,
            )

    def test_missing_empty_bindings_do_not_create_a_file(self) -> None:
        self.assertEqual(load_stack_bindings(self.path), StackBindings({}))
        self.assertIsNone(stack_binding_digest(self.path))

        saved = save_stack_bindings(
            self.path, StackBindings({}), expected_digest=None
        )

        self.assertEqual(saved, StackBindings({}))
        self.assertFalse(self.path.exists())

    def test_load_rejects_unsafe_permissions_and_unknown_fields(self) -> None:
        self.rewrite_externally()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(StackBindingError, "unsafe"):
            load_stack_bindings(self.path)

        self.path.chmod(0o600)
        self.path.write_text(
            '{"schemaVersion":1,"candidateAccounts":{},"extra":true}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StackBindingError, "invalid fields"):
            load_stack_bindings(self.path)


if __name__ == "__main__":
    unittest.main()
