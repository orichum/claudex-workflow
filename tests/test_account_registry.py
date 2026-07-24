#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from integrations.common import account_registry
from integrations.common.account_registry import (
    Account,
    AccountError,
    find_account,
    load_accounts,
    new_account,
    parse_priority,
    update_accounts,
    validate_account_bindings,
)


class AccountRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_home = self.root / "orichum"
        self.config_home.mkdir(mode=0o700)
        self.path = self.config_home / "accounts.json"
        self.providers = {
            "providers": {
                "anthropic": {
                    "credentialType": "claude",
                    "families": ["claude"],
                },
                "openai": {
                    "credentialType": "codex",
                    "families": ["gpt"],
                },
            },
            "accountPools": {
                "work": {"providers": ["anthropic", "openai"]},
                "shared": {"providers": ["anthropic", "openai"]},
            },
        }

    def account(self, *, name: str = "Work Claude") -> Account:
        return new_account(
            name=name,
            provider="anthropic",
            credential_ref="claude-work@example.com.json",
            pool="work",
            priority=100,
            existing=(),
        )

    def write_raw(self, payload: object, mode: int = 0o600) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.path.chmod(mode)

    def test_absent_registry_is_empty_and_update_is_private_atomic_state(self) -> None:
        self.assertEqual(load_accounts(self.path), ())
        account = self.account()

        updated = update_accounts(
            self.path, lambda accounts: (*accounts, account)
        )

        self.assertEqual(updated, (account,))
        self.assertEqual(load_accounts(self.path), (account,))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_home.stat().st_mode), 0o700)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["schemaVersion"],
            2,
        )
        self.assertEqual(
            list(self.config_home.glob(".accounts.json.*")), []
        )

    def test_generated_identifiers_are_opaque_unique_and_stable(self) -> None:
        first = self.account(name="Xebia Claude")
        second = new_account(
            name="Personal Claude",
            provider="anthropic",
            credential_ref="claude-personal.json",
            pool="shared",
            priority=50,
            existing=(first,),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.routing_prefix, second.routing_prefix)
        for generated in (
            first.id,
            first.routing_prefix,
            second.id,
            second.routing_prefix,
        ):
            self.assertNotIn("xebia", generated.lower())
            self.assertNotIn("claude", generated.lower())
            self.assertNotIn("personal", generated.lower())
        update_accounts(self.path, lambda _: (first, second))
        self.assertEqual(load_accounts(self.path), (first, second))

    def test_priority_aliases_and_range_are_strict(self) -> None:
        self.assertEqual(parse_priority("primary"), 100)
        self.assertEqual(parse_priority("secondary"), 50)
        self.assertEqual(parse_priority("reserve"), 10)
        self.assertEqual(parse_priority("750"), 750)
        for value in ("-1", "1001", "1.5", "ultra", ""):
            with self.subTest(value=value), self.assertRaises(AccountError):
                parse_priority(value)

    def test_registry_rejects_duplicate_fields_unknown_keys_and_unsafe_values(self) -> None:
        account = self.account()
        document = {
            "schemaVersion": 2,
            "accounts": [account.as_json(), account.as_json()],
        }
        self.write_raw(document)
        with self.assertRaises(AccountError):
            load_accounts(self.path)

        for field in ("id", "name", "credentialRef", "routingPrefix"):
            second = self.account(name="Second")
            second_json = second.as_json()
            second_json[field] = account.as_json()[field]
            self.write_raw(
                {
                    "schemaVersion": 2,
                    "accounts": [account.as_json(), second_json],
                }
            )
            with self.subTest(field=field), self.assertRaises(AccountError):
                load_accounts(self.path)

        invalid = account.as_json()
        invalid["unexpected"] = True
        self.write_raw({"schemaVersion": 2, "accounts": [invalid]})
        with self.assertRaises(AccountError):
            load_accounts(self.path)

    def test_registry_fails_closed_on_unsafe_parent_file_or_symlink(self) -> None:
        account = self.account()
        self.write_raw({"schemaVersion": 2, "accounts": [account.as_json()]})

        self.path.chmod(0o644)
        with self.assertRaises(AccountError):
            load_accounts(self.path)
        self.path.chmod(0o600)

        self.config_home.chmod(0o755)
        with self.assertRaises(AccountError):
            load_accounts(self.path)
        self.config_home.chmod(0o700)

        target = self.root / "target.json"
        target.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o600)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(AccountError):
            load_accounts(self.path)

    def test_bindings_require_provider_pool_authorization(self) -> None:
        account = self.account()
        validate_account_bindings((account,), self.providers)

        for replacement in (
            Account(**{**account.__dict__, "provider": "missing"}),
            Account(**{**account.__dict__, "pool": "missing"}),
        ):
            with self.subTest(replacement=replacement), self.assertRaises(
                AccountError
            ):
                validate_account_bindings((replacement,), self.providers)

        restricted = json.loads(json.dumps(self.providers))
        restricted["accountPools"]["work"]["providers"] = ["openai"]
        with self.assertRaises(AccountError):
            validate_account_bindings((account,), restricted)

    def test_find_account_accepts_unique_id_or_name(self) -> None:
        account = self.account()
        self.assertEqual(find_account((account,), account.id), account)
        self.assertEqual(find_account((account,), account.name), account)
        with self.assertRaises(AccountError):
            find_account((account,), "missing")

    def test_names_cannot_spoof_terminal_table_boundaries(self) -> None:
        for name in ("bad|column", "bad\tname", "bad\u202ename", "wide-😀"):
            with self.subTest(name=name), self.assertRaises(AccountError):
                self.account(name=name)

    def test_update_rejects_payload_that_cannot_be_read_back(self) -> None:
        account = self.account()
        with mock.patch.object(account_registry, "MAX_REGISTRY_BYTES", 32):
            with self.assertRaisesRegex(AccountError, "too large"):
                update_accounts(self.path, lambda _: (account,))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
