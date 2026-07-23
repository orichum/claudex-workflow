from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from integrations.common import provider_credentials
from integrations.common.provider_credentials import (
    CredentialError,
    default_priority,
    list_credentials,
    main,
    set_default_priorities,
    set_provider_priority,
)


class ProviderCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.auth_dir = Path(self.temporary.name) / "auth"
        self.auth_dir.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def credential(
        self,
        name: str,
        provider: str,
        *,
        email: str = "user@example.com",
        priority: int | None = None,
        disabled: bool = False,
    ) -> Path:
        path = self.auth_dir / name
        document: dict[str, object] = {
            "type": provider,
            "email": email,
            "disabled": disabled,
            "access_token": "ACCESS-SECRET",
            "refresh_token": "REFRESH-SECRET",
        }
        if priority is not None:
            document["priority"] = priority
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_defaults_and_atomic_update_preserve_secrets(self) -> None:
        claude = self.credential("claude-user.json", "claude")
        antigravity = self.credential(
            "antigravity-user.json", "antigravity"
        )
        self.assertEqual(default_priority("claude"), 100)
        self.assertEqual(default_priority("antigravity"), 50)
        self.assertIsNone(default_priority("codex"))

        self.assertEqual(set_default_priorities(self.auth_dir), 2)

        claude_document = json.loads(claude.read_text(encoding="utf-8"))
        antigravity_document = json.loads(
            antigravity.read_text(encoding="utf-8")
        )
        self.assertEqual(claude_document["priority"], 100)
        self.assertEqual(antigravity_document["priority"], 50)
        self.assertEqual(claude_document["access_token"], "ACCESS-SECRET")
        self.assertEqual(
            antigravity_document["refresh_token"], "REFRESH-SECRET"
        )
        self.assertEqual(stat.S_IMODE(claude.stat().st_mode), 0o600)
        before = claude.read_bytes()
        self.assertEqual(set_default_priorities(self.auth_dir), 0)
        self.assertEqual(claude.read_bytes(), before)

    def test_list_returns_only_safe_metadata(self) -> None:
        self.credential(
            "claude-user.json",
            "claude",
            email="person@example.com",
            priority=100,
        )
        credentials = list_credentials(self.auth_dir)
        self.assertEqual(len(credentials), 1)
        self.assertEqual(credentials[0].provider, "claude")
        self.assertEqual(credentials[0].account, "person@example.com")
        self.assertEqual(credentials[0].priority, 100)
        self.assertFalse(credentials[0].disabled)
        self.assertNotIn("SECRET", repr(credentials[0]))

        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(
                main(["--auth-dir", str(self.auth_dir), "list"]), 0
            )
        output = stdout.getvalue()
        self.assertIn("PROVIDER", output)
        self.assertIn("person@example.com", output)
        self.assertNotIn("ACCESS-SECRET", output)
        self.assertNotIn("REFRESH-SECRET", output)

    def test_empty_store_lists_headers_and_defaults_are_idempotent(self) -> None:
        self.assertEqual(set_default_priorities(self.auth_dir), 0)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(
                main(["--auth-dir", str(self.auth_dir), "list"]), 0
            )
        self.assertEqual(
            stdout.getvalue().strip(),
            "PROVIDER  ACCOUNT  PRIORITY  STATE",
        )

    def test_priority_updates_only_requested_provider(self) -> None:
        claude = self.credential("claude-user.json", "claude")
        codex = self.credential("codex-user.json", "codex")
        codex_before = codex.read_bytes()
        self.assertEqual(
            set_provider_priority(self.auth_dir, "claude", 250), 1
        )
        self.assertEqual(
            json.loads(claude.read_text(encoding="utf-8"))["priority"], 250
        )
        self.assertEqual(codex.read_bytes(), codex_before)
        with self.assertRaisesRegex(CredentialError, "not installed"):
            set_provider_priority(self.auth_dir, "kimi", 10)

    def test_defaults_validate_every_file_before_writing(self) -> None:
        claude = self.credential("claude-user.json", "claude")
        before = claude.read_bytes()
        unsafe = self.credential("antigravity-user.json", "antigravity")
        unsafe.chmod(0o644)
        with self.assertRaisesRegex(CredentialError, "mode 0600"):
            set_default_priorities(self.auth_dir)
        self.assertEqual(claude.read_bytes(), before)

    def test_rejects_symlink_foreign_owner_and_malformed_documents(self) -> None:
        target = self.credential("target", "claude")
        (self.auth_dir / "linked.json").symlink_to(target)
        with self.assertRaisesRegex(CredentialError, "regular file"):
            list_credentials(self.auth_dir)
        (self.auth_dir / "linked.json").unlink()

        malformed = self.auth_dir / "malformed.json"
        malformed.write_text("{", encoding="utf-8")
        malformed.chmod(0o600)
        with self.assertRaisesRegex(CredentialError, "valid JSON"):
            list_credentials(self.auth_dir)
        malformed.unlink()

        credential = self.credential("claude-user.json", "claude")
        real_stat = os.stat

        def foreign_stat(path: object, *args: object, **kwargs: object):
            result = real_stat(path, *args, **kwargs)
            if path == credential.name and kwargs.get("dir_fd") is not None:
                values = list(result)
                values[stat.ST_UID] = os.getuid() + 1
                return os.stat_result(values)
            return result

        with mock.patch.object(
            provider_credentials.os, "stat", side_effect=foreign_stat
        ):
            with self.assertRaisesRegex(CredentialError, "current user"):
                list_credentials(self.auth_dir)

    def test_rejects_directory_swap_between_validation_and_open(self) -> None:
        other_dir = Path(self.temporary.name) / "other"
        other_dir.mkdir(mode=0o700)
        real_open = os.open

        def swapped_open(path: object, flags: int, *args: object, **kwargs: object):
            if Path(path) == self.auth_dir:
                return real_open(other_dir, flags, *args, **kwargs)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            provider_credentials.os, "open", side_effect=swapped_open
        ):
            with self.assertRaisesRegex(CredentialError, "changed"):
                list_credentials(self.auth_dir)

    def test_rejects_invalid_shape_provider_priority_and_terminal_label(
        self,
    ) -> None:
        for document, message in (
            ([], "JSON object"),
            ({"type": "../claude"}, "provider"),
            ({"type": "claude", "priority": True}, "priority"),
            ({"type": "claude", "priority": 1001}, "priority"),
        ):
            path = self.auth_dir / "invalid.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaisesRegex(CredentialError, message):
                list_credentials(self.auth_dir)
            path.unlink()

        self.credential(
            "claude-user.json",
            "claude",
            email="\x1b[31mTOKEN\x1b[0m",
        )
        self.assertEqual(
            list_credentials(self.auth_dir)[0].account,
            "claude-user",
        )

    def test_rejects_non_finite_json_constant(self) -> None:
        path = self.auth_dir / "invalid.json"
        path.write_text(
            '{"type":"claude","priority":NaN}',
            encoding="utf-8",
        )
        path.chmod(0o600)
        with self.assertRaisesRegex(CredentialError, "valid JSON"):
            list_credentials(self.auth_dir)

    def test_detects_file_swap_between_validation_and_open(self) -> None:
        self.credential("claude-user.json", "claude")
        real_fstat = os.fstat

        def changed_fstat(fd: int):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[stat.ST_INO] += 1
                return os.stat_result(values)
            return result

        with mock.patch.object(
            provider_credentials.os, "fstat", side_effect=changed_fstat
        ):
            with self.assertRaisesRegex(CredentialError, "changed"):
                list_credentials(self.auth_dir)

    def test_rejects_mode_change_after_open(self) -> None:
        self.credential("claude-user.json", "claude")
        real_fstat = os.fstat

        def public_fstat(fd: int):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[stat.ST_MODE] = stat.S_IFREG | 0o644
                return os.stat_result(values)
            return result

        with mock.patch.object(
            provider_credentials.os, "fstat", side_effect=public_fstat
        ):
            with self.assertRaisesRegex(CredentialError, "mode 0600"):
                list_credentials(self.auth_dir)

    def test_update_reloads_current_document_before_idempotence_check(
        self,
    ) -> None:
        path = self.credential(
            "claude-user.json", "claude", priority=100
        )
        real_load = provider_credentials._load_credential
        calls = 0

        def refresh_before_update(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 2:
                document = json.loads(path.read_text(encoding="utf-8"))
                document["priority"] = 50
                document["access_token"] = "REFRESHED-SECRET"
                path.write_text(json.dumps(document), encoding="utf-8")
                path.chmod(0o600)
            return real_load(*args, **kwargs)

        with mock.patch.object(
            provider_credentials,
            "_load_credential",
            side_effect=refresh_before_update,
        ):
            self.assertEqual(
                set_provider_priority(self.auth_dir, "claude", 100),
                1,
            )
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["priority"], 100)
        self.assertEqual(document["access_token"], "REFRESHED-SECRET")

    def test_atomic_update_forces_mode_0600_under_restrictive_umask(self) -> None:
        path = self.credential("claude-user.json", "claude")
        previous = os.umask(0o777)
        try:
            self.assertEqual(
                set_provider_priority(self.auth_dir, "claude", 100),
                1,
            )
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_cli_rejects_unsafe_arguments_without_secret_output(self) -> None:
        self.credential("claude-user.json", "claude")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(
                main(
                    [
                        "--auth-dir",
                        str(self.auth_dir),
                        "priority",
                        "../claude",
                        "100",
                    ]
                ),
                2,
            )
        self.assertNotIn("ACCESS-SECRET", stderr.getvalue())

    def test_internal_default_for_reports_managed_and_unmanaged_providers(
        self,
    ) -> None:
        self.assertNotIn(
            "default-for",
            provider_credentials._create_parser().format_help(),
        )
        claude = self.credential("claude-user.json", "claude")
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(
                main(
                    [
                        "--auth-dir",
                        str(self.auth_dir),
                        "default-for",
                        "claude",
                    ]
                ),
                0,
            )
        self.assertEqual(
            json.loads(claude.read_text(encoding="utf-8"))["priority"],
            100,
        )
        self.assertIn("claude", stdout.getvalue())
        self.assertIn("changed 1 credential", stdout.getvalue())
        self.assertNotIn("ACCESS-SECRET", stdout.getvalue())

        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            self.assertEqual(
                main(
                    [
                        "--auth-dir",
                        str(self.auth_dir),
                        "default-for",
                        "codex",
                    ]
                ),
                0,
            )
        self.assertIn("codex", stdout.getvalue())
        self.assertIn("changed 0 credential", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
