#!/usr/bin/env python3
"""Private machine-local account bindings for model-stack candidates."""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from types import MappingProxyType
from typing import Mapping

from .account_registry import AccountError, load_accounts


MAX_BINDING_BYTES = 1024 * 1024
_CANDIDATE_ID = re.compile(r"oc-c-[0-9a-f]{16}")
_ACCOUNT_ID = re.compile(r"oc-a-[a-f0-9]{16}")
_ACTIVE_TRANSACTION: contextvars.ContextVar[
    tuple[Path, "StackBindingTransaction"] | None
] = contextvars.ContextVar("active_stack_binding_transaction", default=None)


class StackBindingError(RuntimeError):
    """Stack binding metadata failed validation or safe persistence."""


@dataclass(frozen=True)
class StackBindings:
    candidate_accounts: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_accounts, Mapping):
            raise StackBindingError("candidateAccounts must be an object")
        normalized: dict[str, str] = {}
        for candidate, account in self.candidate_accounts.items():
            if (
                not isinstance(candidate, str)
                or not _CANDIDATE_ID.fullmatch(candidate)
            ):
                raise StackBindingError("stack binding candidate ID is invalid")
            if (
                not isinstance(account, str)
                or not _ACCOUNT_ID.fullmatch(account)
            ):
                raise StackBindingError("stack binding account ID is invalid")
            normalized[candidate] = account
        object.__setattr__(
            self,
            "candidate_accounts",
            MappingProxyType(dict(sorted(normalized.items()))),
        )


def _private_parent(path: Path) -> Path:
    parent = Path(path).parent
    try:
        observed = os.lstat(parent)
        resolved = parent.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise StackBindingError("stack binding parent is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or observed.st_dev != confirmed.st_dev
        or observed.st_ino != confirmed.st_ino
        or resolved != parent
    ):
        raise StackBindingError("stack binding parent is unsafe")
    return resolved


def _file_state(path: Path) -> tuple[int, int, int, int] | None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StackBindingError("stack bindings are unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise StackBindingError("stack bindings are unsafe")
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
    )


def _read(path: Path) -> bytes | None:
    state = _file_state(path)
    if state is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StackBindingError(
            "stack bindings could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != state[:2]
        ):
            raise StackBindingError("stack bindings changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor, min(65536, MAX_BINDING_BYTES + 1 - size)
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BINDING_BYTES:
                raise StackBindingError("stack bindings are too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise StackBindingError("stack bindings changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StackBindingError(f"duplicate stack binding field {key!r}")
        result[key] = value
    return result


def _decode(content: bytes) -> StackBindings:
    def reject_constant(value: str) -> object:
        raise StackBindingError(f"non-finite stack binding value {value}")

    try:
        raw = json.loads(
            content.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise StackBindingError("stack bindings are not valid JSON") from error
    if not isinstance(raw, dict) or set(raw) != {
        "schemaVersion",
        "candidateAccounts",
    }:
        raise StackBindingError("stack bindings have invalid fields")
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise StackBindingError(
            "stack bindings schemaVersion must be exactly 1"
        )
    return StackBindings(raw["candidateAccounts"])


def load_stack_bindings(path: Path) -> StackBindings:
    path = Path(path)
    _private_parent(path)
    content = _read(path)
    return StackBindings({}) if content is None else _decode(content)


def stack_binding_digest(path: Path) -> str | None:
    path = Path(path)
    _private_parent(path)
    content = _read(path)
    return None if content is None else hashlib.sha256(content).hexdigest()


def _write(path: Path, updated: StackBindings) -> None:
    parent = _private_parent(path)
    payload = (
        json.dumps(
            {
                "schemaVersion": 1,
                "candidateAccounts": dict(updated.candidate_accounts),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_BINDING_BYTES:
        raise StackBindingError("stack bindings would be too large")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("stack binding write made no progress")
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


class StackBindingTransaction:
    """Exclusive account/binding transaction held in one canonical order."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise StackBindingError("stack binding transaction is closed")

    def load(self) -> StackBindings:
        self._require_active()
        content = _read(self._path)
        return StackBindings({}) if content is None else _decode(content)

    def digest(self) -> str | None:
        self._require_active()
        content = _read(self._path)
        return (
            None
            if content is None
            else hashlib.sha256(content).hexdigest()
        )

    def save(
        self,
        updated: StackBindings,
        expected_digest: str | None,
    ) -> StackBindings:
        self._require_active()
        current = _read(self._path)
        observed_digest = (
            None
            if current is None
            else hashlib.sha256(current).hexdigest()
        )
        if observed_digest != expected_digest:
            raise StackBindingError("stack bindings changed during update")
        if updated.candidate_accounts:
            try:
                registered = {
                    account.id
                    for account in load_accounts(
                        self._path.parent / "accounts.json"
                    )
                }
            except AccountError as error:
                raise StackBindingError(
                    "account registry is unavailable for stack bindings"
                ) from error
            missing = sorted(
                set(updated.candidate_accounts.values()) - registered
            )
            if missing:
                raise StackBindingError(
                    "stack binding account is not registered"
                )
        if current is None and not updated.candidate_accounts:
            return updated
        _write(self._path, updated)
        return self.load()


@contextmanager
def stack_binding_transaction(path: Path):
    """Serialize account lifecycle and stack-binding mutations."""
    path = Path(path)
    parent = _private_parent(path)
    canonical = parent / path.name
    active = _ACTIVE_TRANSACTION.get()
    if active is not None:
        active_path, transaction = active
        if active_path != canonical:
            raise StackBindingError(
                "nested stack binding transactions require one path"
            )
        yield transaction
        return
    lock_path = parent / ".account-stack-transaction.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise StackBindingError("stack binding lock is unavailable") from error
    try:
        details = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise StackBindingError("stack binding lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        transaction = StackBindingTransaction(path)
        token = _ACTIVE_TRANSACTION.set((canonical, transaction))
        try:
            yield transaction
        finally:
            _ACTIVE_TRANSACTION.reset(token)
            transaction._active = False
    finally:
        os.close(lock_descriptor)


def save_stack_bindings(
    path: Path,
    updated: StackBindings,
    expected_digest: str | None,
) -> StackBindings:
    with stack_binding_transaction(path) as transaction:
        return transaction.save(updated, expected_digest)
