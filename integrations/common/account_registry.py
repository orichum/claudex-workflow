#!/usr/bin/env python3
"""Secure machine-local metadata for named Orichum provider accounts."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from typing import Callable, Mapping, Sequence


MAX_REGISTRY_BYTES = 2 * 1024 * 1024
PRIORITY_ALIASES = {"primary": 100, "secondary": 50, "reserve": 10}
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ACCOUNT_ID = re.compile(r"oc-a-[a-f0-9]{16}")
_ROUTING_PREFIX = re.compile(r"oc-r-[a-f0-9]{16}")
_ORIGINAL_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CREDENTIAL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\.json")
_DISPLAY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,127}")
ACCOUNT_STATES = frozenset(
    {"pending-add", "active", "disabled", "pending-remove"}
)


class AccountError(RuntimeError):
    """Account metadata failed validation or safe persistence."""


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    provider: str
    credential_ref: str
    pool: str
    routing_prefix: str
    priority: int
    state: str
    original_prefix: str | None
    original_priority: int | None

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "credentialRef": self.credential_ref,
            "pool": self.pool,
            "routingPrefix": self.routing_prefix,
            "priority": self.priority,
            "state": self.state,
            "originalPrefix": self.original_prefix,
            "originalPriority": self.original_priority,
        }


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AccountError(f"{label} is invalid")
    return value


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise AccountError("account name is invalid")
    normalized = value.strip()
    if not _DISPLAY_NAME.fullmatch(normalized):
        raise AccountError("account name is invalid")
    return normalized


def _priority(value: object) -> int:
    if type(value) is not int or value < 0 or value > 1000:
        raise AccountError("account priority must be between 0 and 1000")
    return value


def parse_priority(value: str) -> int:
    if value in PRIORITY_ALIASES:
        return PRIORITY_ALIASES[value]
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as error:
        raise AccountError("priority must be an alias or integer") from error
    return _priority(parsed)


def _parse_account(value: object) -> Account:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "name",
        "provider",
        "credentialRef",
        "pool",
        "routingPrefix",
        "priority",
        "state",
        "originalPrefix",
        "originalPriority",
    }:
        raise AccountError("account must contain the exact supported fields")
    identifier = value["id"]
    prefix = value["routingPrefix"]
    credential_ref = value["credentialRef"]
    if not isinstance(identifier, str) or not _ACCOUNT_ID.fullmatch(identifier):
        raise AccountError("account ID is invalid")
    if not isinstance(prefix, str) or not _ROUTING_PREFIX.fullmatch(prefix):
        raise AccountError("account routing prefix is invalid")
    if (
        not isinstance(credential_ref, str)
        or not _CREDENTIAL_REF.fullmatch(credential_ref)
        or Path(credential_ref).name != credential_ref
    ):
        raise AccountError("credential reference is invalid")
    state = value["state"]
    if not isinstance(state, str) or state not in ACCOUNT_STATES:
        raise AccountError("account state is invalid")
    original_prefix = value["originalPrefix"]
    if original_prefix is not None and (
        not isinstance(original_prefix, str)
        or not _ORIGINAL_PREFIX.fullmatch(original_prefix)
    ):
        raise AccountError("account original prefix is invalid")
    original_priority = value["originalPriority"]
    if original_priority is not None:
        original_priority = _priority(original_priority)
    return Account(
        id=identifier,
        name=_name(value["name"]),
        provider=_identifier(value["provider"], "account provider"),
        credential_ref=credential_ref,
        pool=_identifier(value["pool"], "account pool"),
        routing_prefix=prefix,
        priority=_priority(value["priority"]),
        state=state,
        original_prefix=original_prefix,
        original_priority=original_priority,
    )


def _validate_accounts(accounts: Sequence[Account]) -> tuple[Account, ...]:
    normalized = tuple(_parse_account(account.as_json()) for account in accounts)
    for label, values in (
        ("IDs", [account.id for account in normalized]),
        ("names", [account.name for account in normalized]),
        (
            "credential references",
            [account.credential_ref for account in normalized],
        ),
        ("routing prefixes", [account.routing_prefix for account in normalized]),
    ):
        if len(values) != len(set(values)):
            raise AccountError(f"account {label} must be unique")
    return normalized


def _reject_constant(value: str) -> object:
    raise AccountError(f"non-finite JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise AccountError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _private_parent(path: Path) -> Path:
    parent = Path(path).parent
    try:
        observed = os.lstat(parent)
        real = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise AccountError("account registry parent is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or real != parent
    ):
        raise AccountError("account registry parent is unsafe")
    return real


def _file_state(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AccountError("account registry is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise AccountError("account registry is unsafe")
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_uid,
        stat.S_IMODE(details.st_mode),
    )


def load_accounts(path: Path) -> tuple[Account, ...]:
    path = Path(path)
    _private_parent(path)
    state = _file_state(path)
    if state is None:
        return ()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AccountError("account registry could not be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != state[0]
            or opened.st_ino != state[1]
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise AccountError("account registry changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_REGISTRY_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_REGISTRY_BYTES:
                raise AccountError("account registry is too large")
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise AccountError("account registry changed while reading")
    finally:
        os.close(descriptor)
    try:
        raw = json.loads(
            b"".join(chunks).decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise AccountError("account registry is not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "accounts"}:
        raise AccountError("account registry has invalid fields")
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 2:
        raise AccountError("account registry schemaVersion must be exactly 2")
    if not isinstance(raw["accounts"], list):
        raise AccountError("accounts must be an array")
    return _validate_accounts(tuple(_parse_account(value) for value in raw["accounts"]))


def _write_registry(path: Path, accounts: Sequence[Account]) -> None:
    parent = _private_parent(path)
    payload = (
        json.dumps(
            {
                "schemaVersion": 2,
                "accounts": [account.as_json() for account in accounts],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_REGISTRY_BYTES:
        raise AccountError("account registry would be too large")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".accounts.json.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("account registry write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def update_accounts(
    path: Path,
    transform: Callable[[tuple[Account, ...]], Sequence[Account]],
) -> tuple[Account, ...]:
    path = Path(path)
    parent = _private_parent(path)
    lock_path = parent / ".accounts.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise AccountError("account registry lock is unavailable") from error
    try:
        lock_details = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_details.st_mode)
            or lock_details.st_uid != os.getuid()
            or stat.S_IMODE(lock_details.st_mode) != 0o600
        ):
            raise AccountError("account registry lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        before = _file_state(path)
        current = load_accounts(path)
        updated = _validate_accounts(tuple(transform(current)))
        if _file_state(path) != before:
            raise AccountError("account registry changed during update")
        _write_registry(path, updated)
        return updated
    finally:
        os.close(lock_descriptor)


@contextmanager
def account_transaction(path: Path):
    """Serialize registry and upstream publication as one recoverable operation."""
    parent = _private_parent(Path(path))
    lock_path = parent / ".accounts-transaction.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise AccountError("account transaction lock is unavailable") from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise AccountError("account transaction lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def new_account(
    *,
    name: str,
    provider: str,
    credential_ref: str,
    pool: str,
    priority: int,
    existing: Sequence[Account],
    state: str = "active",
    original_prefix: str | None = None,
    original_priority: int | None = None,
) -> Account:
    existing_ids = {account.id for account in existing}
    existing_prefixes = {account.routing_prefix for account in existing}
    for _ in range(32):
        identifier = f"oc-a-{secrets.token_hex(8)}"
        prefix = f"oc-r-{secrets.token_hex(8)}"
        if identifier not in existing_ids and prefix not in existing_prefixes:
            return _parse_account(
                {
                    "id": identifier,
                    "name": name,
                    "provider": provider,
                    "credentialRef": credential_ref,
                    "pool": pool,
                    "routingPrefix": prefix,
                    "priority": priority,
                    "state": state,
                    "originalPrefix": original_prefix,
                    "originalPriority": original_priority,
                }
            )
    raise AccountError("could not allocate opaque account identifiers")


def find_account(accounts: Sequence[Account], selector: str) -> Account:
    matches = [
        account
        for account in accounts
        if account.id == selector or account.name == selector
    ]
    if len(matches) != 1:
        raise AccountError("account selector does not name exactly one account")
    return matches[0]


def validate_account_bindings(
    accounts: Sequence[Account], provider_document: Mapping[str, object]
) -> None:
    providers = provider_document.get("providers")
    pools = provider_document.get("accountPools")
    if not isinstance(providers, dict) or not isinstance(pools, dict):
        raise AccountError("provider configuration is unavailable")
    for account in accounts:
        if account.provider not in providers:
            raise AccountError(f"account {account.id} names an unknown provider")
        pool = pools.get(account.pool)
        if (
            not isinstance(pool, dict)
            or not isinstance(pool.get("providers"), list)
            or account.provider not in pool["providers"]
        ):
            raise AccountError(
                f"account {account.id} is not authorized by its pool"
            )
