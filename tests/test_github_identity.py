from __future__ import annotations

import os
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from integrations.common.github_identity import (
    GithubIdentityError,
    ensure_github_identity,
)


class GithubIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve() / "data"
        self.data.mkdir(mode=0o700)

    def test_provisions_one_private_isolated_account_and_reuses_it(self) -> None:
        calls = []

        def run(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if arguments[1:3] == ["auth", "token"]:
                return mock.Mock(returncode=0, stdout="secret-token\n", stderr="")
            if arguments[1:3] == ["auth", "login"]:
                config = Path(kwargs["env"]["GH_CONFIG_DIR"])
                config.mkdir(parents=True, exist_ok=True)
                (config / "hosts.yml").write_text("fixture\n", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            if arguments[1] == "api":
                return mock.Mock(
                    returncode=0, stdout="athevar-xebia\n", stderr=""
                )
            raise AssertionError(arguments)

        with (
            mock.patch(
                "integrations.common.github_identity.shutil.which",
                return_value="/usr/local/bin/gh",
            ),
            mock.patch(
                "integrations.common.github_identity.subprocess.run",
                side_effect=run,
            ),
        ):
            first = ensure_github_identity(self.data, "athevar-xebia")
            second = ensure_github_identity(self.data, "athevar-xebia")

        self.assertEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        self.assertEqual((first / "hosts.yml").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            sum(args[1:3] == ["auth", "login"] for args, _ in calls), 1
        )
        for _args, kwargs in calls:
            environment = kwargs.get("env", {})
            self.assertNotIn("GH_TOKEN", environment)
            self.assertNotIn("GITHUB_TOKEN", environment)
            self.assertNotIn("GH_HOST", environment)

    def test_rejects_invalid_account_and_foreign_identity_state(self) -> None:
        with self.assertRaises(GithubIdentityError):
            ensure_github_identity(self.data, "../work")

        accounts = self.data / "github" / "accounts"
        accounts.mkdir(parents=True, mode=0o700)
        identity = accounts / hashlib.sha256(b"work").hexdigest()[:16]
        identity.symlink_to(self.data)
        with self.assertRaises(GithubIdentityError):
            ensure_github_identity(self.data, "work")

    def test_expired_isolated_identity_is_rebuilt_from_current_global_login(
        self,
    ) -> None:
        accounts = self.data / "github" / "accounts"
        accounts.mkdir(parents=True, mode=0o700)
        digest = hashlib.sha256(b"athevar-xebia").hexdigest()[:16]
        identity = accounts / digest
        identity.mkdir(mode=0o700)
        (identity / "hosts.yml").write_text("expired\n", encoding="utf-8")
        (identity / "hosts.yml").chmod(0o600)
        api_calls = 0

        def run(arguments, **kwargs):
            nonlocal api_calls
            if arguments[1] == "api":
                api_calls += 1
                login = "wrong-account" if api_calls == 1 else "athevar-xebia"
                return mock.Mock(
                    returncode=0, stdout=f"{login}\n", stderr=""
                )
            if arguments[1:3] == ["auth", "token"]:
                return mock.Mock(
                    returncode=0, stdout="replacement-token\n", stderr=""
                )
            if arguments[1:3] == ["auth", "login"]:
                config = Path(kwargs["env"]["GH_CONFIG_DIR"])
                (config / "hosts.yml").write_text(
                    "replacement\n", encoding="utf-8"
                )
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise AssertionError(arguments)

        with (
            mock.patch(
                "integrations.common.github_identity.shutil.which",
                return_value="/usr/local/bin/gh",
            ),
            mock.patch(
                "integrations.common.github_identity.subprocess.run",
                side_effect=run,
            ),
        ):
            resolved = ensure_github_identity(self.data, "athevar-xebia")

        self.assertEqual(resolved, identity)
        self.assertEqual(
            (identity / "hosts.yml").read_text(encoding="utf-8"),
            "replacement\n",
        )
        self.assertFalse((accounts / f".{digest}.stale").exists())
        self.assertEqual(api_calls, 3)

    def test_failed_replacement_verification_restores_prior_identity(self) -> None:
        accounts = self.data / "github" / "accounts"
        accounts.mkdir(parents=True, mode=0o700)
        digest = hashlib.sha256(b"work").hexdigest()[:16]
        identity = accounts / digest
        identity.mkdir(mode=0o700)
        (identity / "hosts.yml").write_text("prior\n", encoding="utf-8")
        (identity / "hosts.yml").chmod(0o600)
        api_calls = 0

        def run(arguments, **kwargs):
            nonlocal api_calls
            if arguments[1] == "api":
                api_calls += 1
                login = "work" if api_calls == 2 else "wrong"
                return mock.Mock(
                    returncode=0, stdout=f"{login}\n", stderr=""
                )
            if arguments[1:3] == ["auth", "token"]:
                return mock.Mock(returncode=0, stdout="token\n", stderr="")
            if arguments[1:3] == ["auth", "login"]:
                config = Path(kwargs["env"]["GH_CONFIG_DIR"])
                (config / "hosts.yml").write_text("new\n", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            raise AssertionError(arguments)

        with (
            mock.patch(
                "integrations.common.github_identity.shutil.which",
                return_value="/usr/local/bin/gh",
            ),
            mock.patch(
                "integrations.common.github_identity.subprocess.run",
                side_effect=run,
            ),
            self.assertRaises(GithubIdentityError),
        ):
            ensure_github_identity(self.data, "work")

        self.assertEqual(
            (identity / "hosts.yml").read_text(encoding="utf-8"),
            "prior\n",
        )
        self.assertFalse((accounts / f".{digest}.stale").exists())

    def test_interrupted_stale_transition_is_recovered_under_lock(self) -> None:
        accounts = self.data / "github" / "accounts"
        accounts.mkdir(parents=True, mode=0o700)
        digest = hashlib.sha256(b"work").hexdigest()[:16]
        stale = accounts / f".{digest}.stale"
        stale.mkdir(mode=0o700)
        (stale / "hosts.yml").write_text("valid\n", encoding="utf-8")
        (stale / "hosts.yml").chmod(0o600)

        def run(arguments, **_kwargs):
            if arguments[1] == "api":
                return mock.Mock(returncode=0, stdout="work\n", stderr="")
            raise AssertionError(arguments)

        with (
            mock.patch(
                "integrations.common.github_identity.shutil.which",
                return_value="/usr/local/bin/gh",
            ),
            mock.patch(
                "integrations.common.github_identity.subprocess.run",
                side_effect=run,
            ),
        ):
            resolved = ensure_github_identity(self.data, "work")

        self.assertEqual(resolved, accounts / digest)
        self.assertTrue((resolved / "hosts.yml").is_file())
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
