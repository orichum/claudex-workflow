#!/usr/bin/env python3
"""Provision isolated, project-selected GitHub CLI identities."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


_ACCOUNT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


class GithubIdentityError(RuntimeError):
    """A GitHub account cannot be isolated safely."""


def validate_github_account(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ACCOUNT.fullmatch(value):
        raise GithubIdentityError("GitHub account is invalid")
    return value


def _private_directory(path: Path, *, create: bool) -> Path:
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        observed = os.lstat(path)
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GithubIdentityError("GitHub identity directory is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or real != path
    ):
        raise GithubIdentityError("GitHub identity directory is unsafe")
    return real


def _environment(config: Path | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST",
        }
    }
    if config is not None:
        environment["GH_CONFIG_DIR"] = str(config)
    return environment


def _run(
    gh: str,
    arguments: list[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [gh, *arguments],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GithubIdentityError("GitHub identity command failed") from error


def _verify(gh: str, directory: Path, account: str) -> None:
    hosts = directory / "hosts.yml"
    try:
        observed = os.lstat(hosts)
    except OSError as error:
        raise GithubIdentityError("isolated GitHub authentication is missing") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise GithubIdentityError("isolated GitHub authentication is unsafe")
    result = _run(
        gh,
        ["api", "--hostname", "github.com", "user", "--jq", ".login"],
        environment=_environment(directory),
    )
    if result.returncode != 0 or result.stdout.strip().lower() != account.lower():
        raise GithubIdentityError(
            f"GitHub account {account} is not authenticated"
        )


def ensure_github_identity(data_home: Path, account: str) -> Path:
    """Return a private GH_CONFIG_DIR containing exactly the selected account."""
    account = validate_github_account(account)
    if account is None:
        raise GithubIdentityError("GitHub account is required")
    data_home = _private_directory(Path(data_home), create=False)
    accounts = _private_directory(data_home / "github" / "accounts", create=True)
    digest = hashlib.sha256(account.lower().encode("utf-8")).hexdigest()[:16]
    destination = accounts / digest
    stale = accounts / f".{digest}.stale"
    lock = accounts / f".{digest}.lock"
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        gh = shutil.which("gh")
        if gh is None:
            raise GithubIdentityError("GitHub CLI is not installed")
        if stale.exists() or stale.is_symlink():
            stale_directory = _private_directory(stale, create=False)
            if not destination.exists() and not destination.is_symlink():
                os.rename(stale_directory, destination)
            else:
                directory = _private_directory(destination, create=False)
                try:
                    _verify(gh, directory, account)
                except GithubIdentityError:
                    try:
                        _verify(gh, stale_directory, account)
                    except GithubIdentityError:
                        shutil.rmtree(stale_directory)
                    else:
                        shutil.rmtree(directory)
                        os.rename(stale_directory, destination)
                else:
                    shutil.rmtree(stale_directory)
                    return directory
        replace_existing = destination.exists() or destination.is_symlink()
        if replace_existing:
            directory = _private_directory(destination, create=False)
            try:
                _verify(gh, directory, account)
            except GithubIdentityError:
                pass
            else:
                return directory

        token = _run(
            gh,
            ["auth", "token", "--hostname", "github.com", "--user", account],
            environment=_environment(),
        )
        if token.returncode != 0 or not token.stdout.strip():
            raise GithubIdentityError(
                f"GitHub account {account} is not available in gh auth"
            )
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{digest}.", dir=accounts)
        )
        os.chmod(temporary, 0o700)
        try:
            login = _run(
                gh,
                ["auth", "login", "--hostname", "github.com", "--with-token"],
                environment=_environment(temporary),
                input_text=token.stdout,
            )
            token = None
            if login.returncode != 0:
                raise GithubIdentityError(
                    f"GitHub account {account} could not be isolated"
                )
            for root, directories, files in os.walk(
                temporary, topdown=True, followlinks=False
            ):
                for name in directories:
                    target = Path(root) / name
                    if target.is_symlink():
                        raise GithubIdentityError(
                            "isolated GitHub authentication contains a symlink"
                        )
                    os.chmod(target, 0o700)
                for name in files:
                    target = Path(root) / name
                    if target.is_symlink():
                        raise GithubIdentityError(
                            "isolated GitHub authentication contains a symlink"
                        )
                    os.chmod(target, 0o600)
            _verify(gh, temporary, account)
            if replace_existing:
                if stale.exists() or stale.is_symlink():
                    raise GithubIdentityError(
                        "stale GitHub identity recovery state exists"
                    )
                os.rename(destination, stale)
                try:
                    os.rename(temporary, destination)
                    _verify(gh, destination, account)
                except BaseException:
                    if destination.exists() or destination.is_symlink():
                        shutil.rmtree(destination)
                    os.rename(stale, destination)
                    raise
                shutil.rmtree(stale)
            else:
                os.rename(temporary, destination)
            return _private_directory(destination, create=False)
        finally:
            token = None
            if temporary.exists():
                shutil.rmtree(temporary)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
