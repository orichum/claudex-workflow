#!/usr/bin/env python3
"""Safely inspect and update CLIProxyAPI OAuth credentials."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Mapping, Sequence


DEFAULT_PRIORITIES = {"claude": 100, "antigravity": 50}
PROVIDER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+:-]{0,127}")
CREDENTIAL_REF_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\.json"
)
MAX_CREDENTIAL_BYTES = 4 * 1024 * 1024


class CredentialError(RuntimeError):
    """A credential directory or file failed closed validation."""


@dataclass(frozen=True)
class Credential:
    path: Path
    provider: str
    account: str
    priority: int | None
    disabled: bool
    prefix: str | None


@dataclass(frozen=True)
class _LoadedCredential:
    metadata: Credential
    document: Mapping[str, object]
    device: int
    inode: int
    digest: bytes


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CredentialError(message)


def _validate_provider(value: object) -> str:
    if not isinstance(value, str) or not PROVIDER_PATTERN.fullmatch(value):
        raise CredentialError("credential provider is invalid")
    return value


def _validate_priority(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CredentialError("credential priority must be an integer")
    if value < 0 or value > 1000:
        raise CredentialError("credential priority must be between 0 and 1000")
    return value


def _safe_account(document: Mapping[str, object], filename: str) -> str:
    for key in ("email", "account_id"):
        value = document.get(key)
        if isinstance(value, str) and ACCOUNT_PATTERN.fullmatch(value):
            return value
    stem = Path(filename).stem
    if ACCOUNT_PATTERN.fullmatch(stem):
        return stem
    return "hidden"


def _validate_auth_directory(auth_dir: Path) -> os.stat_result:
    try:
        details = os.lstat(auth_dir)
    except OSError as error:
        raise CredentialError("credential directory is unavailable") from error
    if not stat.S_ISDIR(details.st_mode):
        raise CredentialError("credential directory must be a regular directory")
    if details.st_uid != os.getuid():
        raise CredentialError("credential directory must belong to current user")
    if stat.S_IMODE(details.st_mode) & 0o077:
        raise CredentialError("credential directory must not allow group or other access")
    return details


def _open_auth_directory(auth_dir: Path) -> int:
    before = _validate_auth_directory(auth_dir)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(auth_dir, flags)
    except OSError as error:
        raise CredentialError("credential directory could not be opened safely") from error
    opened = os.fstat(dir_fd)
    if not _same_file(opened, before.st_dev, before.st_ino):
        os.close(dir_fd)
        raise CredentialError("credential directory changed while opening")
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.getuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(dir_fd)
        raise CredentialError("opened credential directory is unsafe")
    return dir_fd


def _same_file(details: os.stat_result, device: int, inode: int) -> bool:
    return details.st_dev == device and details.st_ino == inode


def _validate_open_credential(details: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(details.st_mode):
        raise CredentialError(f"credential {name!r} must be a regular file")
    if details.st_uid != os.getuid():
        raise CredentialError(f"credential {name!r} must belong to current user")
    if stat.S_IMODE(details.st_mode) != 0o600:
        raise CredentialError(f"credential {name!r} must use mode 0600")


def _load_credential(auth_dir: Path, dir_fd: int, name: str) -> _LoadedCredential:
    path = auth_dir / name
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as error:
        raise CredentialError(f"credential {name!r} is unavailable") from error
    if not stat.S_ISREG(before.st_mode):
        raise CredentialError(f"credential {name!r} must be a regular file")
    if before.st_uid != os.getuid():
        raise CredentialError(f"credential {name!r} must belong to current user")
    if stat.S_IMODE(before.st_mode) != 0o600:
        raise CredentialError(f"credential {name!r} must use mode 0600")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        raise CredentialError(f"credential {name!r} could not be opened safely") from error
    try:
        opened = os.fstat(file_fd)
        _validate_open_credential(opened, name)
        if not _same_file(opened, before.st_dev, before.st_ino):
            raise CredentialError(f"credential {name!r} changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(file_fd, min(65536, MAX_CREDENTIAL_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_CREDENTIAL_BYTES:
                raise CredentialError(f"credential {name!r} is too large")
        after = os.fstat(file_fd)
        _validate_open_credential(after, name)
        if not _same_file(after, before.st_dev, before.st_ino):
            raise CredentialError(f"credential {name!r} changed while reading")
    finally:
        os.close(file_fd)

    content = b"".join(chunks)

    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON constant {value}")

    try:
        document = json.loads(
            content.decode("utf-8"),
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError) as error:
        raise CredentialError(f"credential {name!r} is not valid JSON") from error
    if not isinstance(document, dict):
        raise CredentialError(f"credential {name!r} must contain a JSON object")
    provider = _validate_provider(document.get("type"))
    raw_priority = document.get("priority")
    priority = (
        None if raw_priority is None else _validate_priority(raw_priority)
    )
    disabled = document.get("disabled", False)
    if not isinstance(disabled, bool):
        raise CredentialError(f"credential {name!r} has invalid disabled state")
    prefix = document.get("prefix")
    if prefix == "":
        prefix = None
    elif prefix is not None and (
        not isinstance(prefix, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", prefix)
    ):
        raise CredentialError(f"credential {name!r} has invalid routing prefix")
    metadata = Credential(
        path=path,
        provider=provider,
        account=_safe_account(document, name),
        priority=priority,
        disabled=disabled,
        prefix=prefix,
    )
    return _LoadedCredential(
        metadata,
        document,
        before.st_dev,
        before.st_ino,
        hashlib.sha256(content).digest(),
    )


def _load_all_from_fd(
    auth_dir: Path, dir_fd: int
) -> tuple[_LoadedCredential, ...]:
    try:
        names = sorted(
            name for name in os.listdir(dir_fd) if name.endswith(".json")
        )
    except OSError as error:
        raise CredentialError("credential directory could not be read") from error
    return tuple(_load_credential(auth_dir, dir_fd, name) for name in names)


def list_credentials(auth_dir: Path) -> tuple[Credential, ...]:
    auth_dir = Path(auth_dir)
    dir_fd = _open_auth_directory(auth_dir)
    try:
        return tuple(
            item.metadata for item in _load_all_from_fd(auth_dir, dir_fd)
        )
    finally:
        os.close(dir_fd)


def resolve_credential_ref(
    auth_dir: Path,
    credential_ref: str,
    *,
    expected_provider: str | None = None,
) -> Credential:
    """Resolve one safe credential basename without exposing its document."""
    if (
        not isinstance(credential_ref, str)
        or not CREDENTIAL_REF_PATTERN.fullmatch(credential_ref)
        or Path(credential_ref).name != credential_ref
    ):
        raise CredentialError("credential reference is invalid")
    expected = (
        None
        if expected_provider is None
        else _validate_provider(expected_provider)
    )
    auth_dir = Path(auth_dir)
    dir_fd = _open_auth_directory(auth_dir)
    try:
        credential = _load_credential(
            auth_dir, dir_fd, credential_ref
        ).metadata
    finally:
        os.close(dir_fd)
    if expected is not None and credential.provider != expected:
        raise CredentialError("credential provider does not match account provider")
    return credential


def load_credential_fields(
    auth_dir: Path,
    credential_ref: str,
    *,
    expected_provider: str,
    fields: Sequence[str],
) -> dict[str, object]:
    """Read only explicitly requested fields from one safely opened credential."""
    if (
        not isinstance(credential_ref, str)
        or not CREDENTIAL_REF_PATTERN.fullmatch(credential_ref)
        or Path(credential_ref).name != credential_ref
    ):
        raise CredentialError("credential reference is invalid")
    expected = _validate_provider(expected_provider)
    requested = tuple(fields)
    if (
        not requested
        or len(requested) != len(set(requested))
        or any(
            not isinstance(field, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field)
            for field in requested
        )
    ):
        raise CredentialError("credential field selection is invalid")
    auth_dir = Path(auth_dir)
    dir_fd = _open_auth_directory(auth_dir)
    try:
        loaded = _load_credential(auth_dir, dir_fd, credential_ref)
    finally:
        os.close(dir_fd)
    if loaded.metadata.provider != expected:
        raise CredentialError("credential provider does not match account provider")
    return {
        field: loaded.document[field]
        for field in requested
        if field in loaded.document
    }


@contextmanager
def credential_metadata_transaction(auth_dir: Path):
    """Serialize every Orichum metadata writer for one CLIProxy auth store."""
    auth_dir = Path(auth_dir)
    dir_fd = _open_auth_directory(auth_dir)
    descriptor = -1
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            ".orichum-metadata.lock", flags, 0o600, dir_fd=dir_fd
        )
        os.fchmod(descriptor, 0o600)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise CredentialError("credential metadata lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as error:
        raise CredentialError("credential metadata lock is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(dir_fd)


def default_priority(provider: str) -> int | None:
    provider = _validate_provider(provider)
    return DEFAULT_PRIORITIES.get(provider)


def _write_all(file_fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(file_fd, content[offset:])
        if written <= 0:
            raise OSError("credential write made no progress")
        offset += written


def _atomic_update(
    auth_dir: Path,
    dir_fd: int,
    loaded: _LoadedCredential,
    priority: int,
) -> bool:
    name = loaded.metadata.path.name
    current = _load_credential(auth_dir, dir_fd, name)
    if current.metadata.provider != loaded.metadata.provider:
        raise CredentialError(f"credential {name!r} changed provider")
    if current.metadata.priority == priority:
        return False
    document = dict(current.document)
    document["priority"] = priority
    content = (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")
    temp_name = f".{name}.claudex-{secrets.token_hex(8)}.tmp"
    temp_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        temp_created = True
        try:
            os.fchmod(file_fd, 0o600)
            if stat.S_IMODE(os.fstat(file_fd).st_mode) != 0o600:
                raise CredentialError(
                    f"temporary credential for {name!r} is not private"
                )
            _write_all(file_fd, content)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        confirmed = _load_credential(auth_dir, dir_fd, name)
        if (
            confirmed.device != current.device
            or confirmed.inode != current.inode
            or confirmed.digest != current.digest
        ):
            raise CredentialError(f"credential {name!r} changed during update")
        os.replace(
            temp_name,
            name,
            src_dir_fd=dir_fd,
            dst_dir_fd=dir_fd,
        )
        temp_created = False
        os.fsync(dir_fd)
    except CredentialError:
        raise
    except OSError as error:
        raise CredentialError(f"credential {name!r} could not be updated") from error
    finally:
        if temp_created:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except OSError:
                pass
    return True


def _update_loaded(
    auth_dir: Path,
    dir_fd: int,
    credentials: Sequence[_LoadedCredential],
    priorities: Mapping[str, int],
) -> int:
    changed = 0
    for credential in credentials:
        priority = priorities.get(credential.metadata.provider)
        if priority is not None and _atomic_update(
            auth_dir, dir_fd, credential, priority
        ):
            changed += 1
    return changed


def set_provider_priority(auth_dir: Path, provider: str, priority: int) -> int:
    provider = _validate_provider(provider)
    priority = _validate_priority(priority)
    auth_dir = Path(auth_dir)
    with credential_metadata_transaction(auth_dir):
        dir_fd = _open_auth_directory(auth_dir)
        try:
            credentials = _load_all_from_fd(auth_dir, dir_fd)
            matches = tuple(
                item for item in credentials if item.metadata.provider == provider
            )
            if not matches:
                raise CredentialError(f"provider {provider!r} is not installed")
            return _update_loaded(
                auth_dir, dir_fd, matches, {provider: priority}
            )
        finally:
            os.close(dir_fd)


def set_default_priorities(auth_dir: Path) -> int:
    auth_dir = Path(auth_dir)
    with credential_metadata_transaction(auth_dir):
        dir_fd = _open_auth_directory(auth_dir)
        try:
            credentials = _load_all_from_fd(auth_dir, dir_fd)
            return _update_loaded(
                auth_dir, dir_fd, credentials, DEFAULT_PRIORITIES
            )
        finally:
            os.close(dir_fd)


def _render_table(credentials: Sequence[Credential]) -> str:
    headers = ("PROVIDER", "ACCOUNT", "PRIORITY", "STATE")
    rows = [
        (
            credential.provider,
            credential.account,
            "default" if credential.priority is None else str(credential.priority),
            "disabled" if credential.disabled else "active",
        )
        for credential in credentials
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(headers)
        )
    ]
    lines.extend(
        "  ".join(
            value.ljust(widths[index])
            for index, value in enumerate(row)
        ).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def _create_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orichum provider credential")
    parser.add_argument("--auth-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("defaults")
    priority = commands.add_parser("priority")
    priority.add_argument("provider")
    priority.add_argument("priority")
    return parser


def _internal_default_for(
    arguments: Sequence[str],
) -> tuple[Path, str] | None:
    if (
        len(arguments) == 4
        and arguments[0] == "--auth-dir"
        and arguments[2] == "default-for"
    ):
        return Path(arguments[1]), _validate_provider(arguments[3])
    return None


def _apply_default_for(auth_dir: Path, provider: str) -> None:
    priority = default_priority(provider)
    if priority is not None:
        changed = set_provider_priority(auth_dir, provider, priority)
        print(
            f"Applied default priority for {provider}; "
            f"changed {changed} credential(s)."
        )
    else:
        print(
            f"No managed default priority for {provider}; "
            "changed 0 credential(s)."
        )


def main(arguments: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        internal = _internal_default_for(raw_arguments)
        if internal is not None:
            _apply_default_for(*internal)
            return 0
        parsed = _create_parser().parse_args(raw_arguments)
        if parsed.command == "list":
            print(_render_table(list_credentials(parsed.auth_dir)))
        elif parsed.command == "defaults":
            changed = set_default_priorities(parsed.auth_dir)
            print(f"Applied provider defaults; changed {changed} credential(s).")
        elif parsed.command == "priority":
            try:
                priority = int(parsed.priority, 10)
            except ValueError as error:
                raise CredentialError("priority must be an integer") from error
            changed = set_provider_priority(
                parsed.auth_dir, parsed.provider, priority
            )
            print(
                f"Applied priority for {parsed.provider}; "
                f"changed {changed} credential(s)."
            )
    except CredentialError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
