#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest

from integrations.common.stack_bindings import (
    StackBindingError,
    StackBindings,
    load_stack_bindings,
    save_stack_bindings,
    stack_binding_digest,
)


class StackBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "stack-bindings.json"

    def rewrite_externally(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "candidateAccounts": {
                        "oc-c-2222222222222222": "oc-a-external"
                    },
                }
            ),
            encoding="utf-8",
        )
        self.path.chmod(0o600)

    def test_round_trip_is_private_and_rename_independent(self):
        updated = save_stack_bindings(
            self.path,
            StackBindings({"oc-c-1111111111111111": "oc-a-account"}),
            expected_digest=None,
        )
        self.assertEqual(
            updated.candidate_accounts["oc-c-1111111111111111"],
            "oc-a-account",
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(load_stack_bindings(self.path), updated)
        self.assertIsNotNone(stack_binding_digest(self.path))

    def test_digest_conflict_does_not_overwrite(self):
        original = load_stack_bindings(self.path)
        self.rewrite_externally()
        with self.assertRaisesRegex(StackBindingError, "changed during update"):
            save_stack_bindings(self.path, original, expected_digest="stale")
        self.assertEqual(
            load_stack_bindings(self.path).candidate_accounts,
            {"oc-c-2222222222222222": "oc-a-external"},
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
