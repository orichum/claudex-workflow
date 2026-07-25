#!/usr/bin/env python3
"""Coordinated persistence for portable stacks and local account locks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator, Mapping, Sequence

from .account_registry import Account, AccountError, load_accounts
from .orichum_config import MAX_CONFIG_BYTES
from .stack_bindings import (
    MAX_BINDING_BYTES,
    StackBindingError,
    StackBindings,
    stack_binding_transaction,
)
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    StackDefinitionError,
    normalize_model_stacks,
    serialize_model_stacks,
)
from .stack_catalog import LiveCatalog


_TRANSACTION_MARKER = ".model-stacks.transaction.json"
_MODEL_ORIGINAL = ".model-stacks.transaction.model.original"
_BINDING_ORIGINAL = ".model-stacks.transaction.bindings.original"
_TRANSACTION_STATES = frozenset({"pending", "committed"})


class StackStoreError(RuntimeError):
    """A coordinated stack update failed validation or safe persistence."""


@dataclass(frozen=True)
class StackSnapshot:
    stacks: NormalizedStacks
    bindings: StackBindings
    stack_digest: str
    binding_digest: str | None
    _model_path: Path | None = field(
        default=None, repr=False, compare=False
    )
    _binding_path: Path | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class _TransactionState:
    state: str
    binding_existed: bool


def _require_shared_private_parent(
    model_path: Path, binding_path: Path
) -> Path:
    if model_path.parent != binding_path.parent:
        raise StackStoreError(
            "model stacks and bindings must share the same private parent"
        )
    return binding_path.parent


def _file_bytes(
    path: Path,
    *,
    label: str,
    limit: int,
    required_mode: int | None = None,
) -> bytes:
    try:
        before = os.lstat(path)
    except (OSError, RuntimeError) as error:
        raise StackStoreError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or (
            required_mode is not None
            and stat.S_IMODE(before.st_mode) != required_mode
        )
    ):
        raise StackStoreError(f"{label} is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StackStoreError(f"{label} could not be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
            or (
                required_mode is not None
                and stat.S_IMODE(opened.st_mode) != required_mode
            )
        ):
            raise StackStoreError(f"{label} changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise StackStoreError(f"{label} is too large")
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
            raise StackStoreError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise StackStoreError(
                f"model stacks contain duplicate field {key!r}"
            )
        document[key] = value
    return document


def _decode_stacks(content: bytes) -> NormalizedStacks:
    def reject_constant(value: str) -> object:
        raise StackStoreError(f"model stacks contain non-finite {value}")

    try:
        document = json.loads(
            content.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=_unique_object,
        )
        return normalize_model_stacks(document)
    except StackStoreError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        StackDefinitionError,
    ) as error:
        raise StackStoreError("model stacks are invalid") from error


def _candidate_ids(stacks: NormalizedStacks) -> set[str]:
    return {
        candidate.id
        for stack in stacks.stacks.values()
        for candidates in (
            stack.controller,
            *stack.agents.values(),
        )
        for candidate in candidates
    }


@contextmanager
def _model_lock(private_parent: Path) -> Iterator[None]:
    lock_path = private_parent / ".model-stacks.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise StackStoreError("model stack lock is unsafe") from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise StackStoreError("model stack lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as error:
        raise StackStoreError("model stack lock is unavailable") from error
    finally:
        os.close(descriptor)


def load_stack_snapshot(
    model_path: Path, binding_path: Path
) -> StackSnapshot:
    model_path = Path(model_path)
    binding_path = Path(binding_path)
    private_parent = _require_shared_private_parent(
        model_path, binding_path
    )
    try:
        with stack_binding_transaction(binding_path) as transaction:
            with _model_lock(private_parent):
                _recover_transaction(model_path, binding_path)
                model_content = _file_bytes(
                    model_path,
                    label="model stacks",
                    limit=MAX_CONFIG_BYTES,
                    required_mode=0o600,
                )
                stacks = _decode_stacks(model_content)
                binding_digest = transaction.digest()
                bindings = transaction.load()
                if transaction.digest() != binding_digest:
                    raise StackStoreError(
                        "stack bindings changed while loading"
                    )
    except StackStoreError:
        raise
    except StackBindingError as error:
        raise StackStoreError(str(error)) from error
    candidates = _candidate_ids(stacks)
    current_bindings = StackBindings(
        {
            candidate: account
            for candidate, account in bindings.candidate_accounts.items()
            if candidate in candidates
        }
    )
    return StackSnapshot(
        stacks=stacks,
        bindings=current_bindings,
        stack_digest=hashlib.sha256(model_content).hexdigest(),
        binding_digest=binding_digest,
        _model_path=model_path,
        _binding_path=binding_path,
    )


def _serialize_stacks(updated: NormalizedStacks) -> bytes:
    if not isinstance(updated, NormalizedStacks):
        raise StackStoreError("updated model stacks are invalid")
    try:
        document = serialize_model_stacks(updated)
        normalized = normalize_model_stacks(document)
    except StackDefinitionError as error:
        raise StackStoreError("updated model stacks are invalid") from error
    if normalized != updated:
        raise StackStoreError("updated model stacks are not normalized")
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise StackStoreError("updated model stacks are too large")
    return payload


def _serialize_bindings(updated: StackBindings) -> bytes:
    if not isinstance(updated, StackBindings):
        raise StackStoreError("updated stack bindings are invalid")
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
        raise StackStoreError("updated stack bindings are too large")
    return payload


def _validate_bindings(
    stacks: NormalizedStacks, bindings: StackBindings, binding_path: Path
) -> None:
    try:
        accounts = load_accounts(binding_path.parent / "accounts.json")
    except AccountError as error:
        raise StackStoreError(
            "account registry is unavailable for stack bindings"
        ) from error
    validate_stack_bindings(stacks, bindings, accounts)


def validate_stack_bindings(
    stacks: NormalizedStacks,
    bindings: StackBindings,
    accounts: Sequence[Account],
) -> None:
    """Validate machine-local locks against stack candidates and accounts."""
    candidates = {
        candidate.id: candidate
        for stack in stacks.stacks.values()
        for choices in (stack.controller, *stack.agents.values())
        for candidate in choices
    }
    missing = sorted(
        set(bindings.candidate_accounts) - set(candidates)
    )
    if missing:
        raise StackStoreError(
            "stack binding references an unknown candidate"
        )
    active = {
        account.id: account
        for account in accounts
        if account.state == "active"
    }
    inactive = sorted(
        set(bindings.candidate_accounts.values()) - set(active)
    )
    if inactive:
        raise StackStoreError(
            "stack binding must reference an active account"
        )
    incompatible = [
        candidate
        for candidate, account_id in bindings.candidate_accounts.items()
        if active[account_id].provider
        not in candidates[candidate].providers
    ]
    if incompatible:
        raise StackStoreError(
            "stack binding account must use an allowed provider"
        )


def _stage(path: Path, payload: bytes, mode: int) -> Path:
    if mode != 0o600:
        raise StackStoreError("stack store staging mode is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("stack store write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _recovery_path(parent: Path, name: str) -> Path:
    return parent / name


def _optional_private_bytes(
    path: Path, *, label: str, limit: int
) -> bytes | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StackStoreError(f"{label} is unavailable") from error
    return _file_bytes(
        path,
        label=label,
        limit=limit,
        required_mode=0o600,
    )


def _replace_private_file(path: Path, payload: bytes) -> None:
    staged = _stage(path, payload, 0o600)
    try:
        os.replace(staged, path)
        staged = None
    finally:
        _unlink(staged)


def _marker_payload(
    state: str, binding_existed: bool
) -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "state": state,
                "bindingExisted": binding_existed,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_transaction_marker(
    parent: Path, state: str, binding_existed: bool
) -> None:
    if state not in _TRANSACTION_STATES:
        raise StackStoreError("stack transaction state is invalid")
    _replace_private_file(
        _recovery_path(parent, _TRANSACTION_MARKER),
        _marker_payload(state, binding_existed),
    )


def _load_transaction_marker(
    parent: Path,
) -> _TransactionState | None:
    content = _optional_private_bytes(
        _recovery_path(parent, _TRANSACTION_MARKER),
        label="stack transaction marker",
        limit=4096,
    )
    if content is None:
        return None
    try:
        raw = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        StackStoreError,
    ) as error:
        raise StackStoreError(
            "stack transaction marker is invalid"
        ) from error
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {"schemaVersion", "state", "bindingExisted"}
        or type(raw["schemaVersion"]) is not int
        or raw["schemaVersion"] != 1
        or raw["state"] not in _TRANSACTION_STATES
        or type(raw["bindingExisted"]) is not bool
    ):
        raise StackStoreError("stack transaction marker is invalid")
    return _TransactionState(
        state=raw["state"],
        binding_existed=raw["bindingExisted"],
    )


def _remove_recovery_artifacts(parent: Path) -> None:
    for name in (_MODEL_ORIGINAL, _BINDING_ORIGINAL):
        path = _recovery_path(parent, name)
        content = _optional_private_bytes(
            path,
            label="stack recovery file",
            limit=max(MAX_CONFIG_BYTES, MAX_BINDING_BYTES),
        )
        if content is not None:
            path.unlink()
    marker = _recovery_path(parent, _TRANSACTION_MARKER)
    if _optional_private_bytes(
        marker, label="stack transaction marker", limit=4096
    ) is not None:
        marker.unlink()
    _fsync_directory(parent)


def _cleanup_after_commit(parent: Path) -> None:
    try:
        _remove_recovery_artifacts(parent)
    except BaseException:
        pass


def _restore_originals(
    model_path: Path,
    binding_path: Path,
    *,
    binding_existed: bool,
) -> None:
    parent = model_path.parent
    original_model = _optional_private_bytes(
        _recovery_path(parent, _MODEL_ORIGINAL),
        label="model stack recovery file",
        limit=MAX_CONFIG_BYTES,
    )
    if original_model is None:
        raise StackStoreError(
            "model stack recovery file is unavailable"
        )
    original_binding = _optional_private_bytes(
        _recovery_path(parent, _BINDING_ORIGINAL),
        label="stack binding recovery file",
        limit=MAX_BINDING_BYTES,
    )
    if binding_existed and original_binding is None:
        raise StackStoreError(
            "stack binding recovery file is unavailable"
        )
    staged_model = _stage(model_path, original_model, 0o600)
    staged_binding = (
        _stage(binding_path, original_binding, 0o600)
        if original_binding is not None
        else None
    )
    try:
        os.replace(staged_model, model_path)
        staged_model = None
        if binding_existed:
            os.replace(staged_binding, binding_path)
            staged_binding = None
        else:
            _unlink(binding_path)
        _fsync_directory(parent)
    finally:
        _unlink(staged_model)
        _unlink(staged_binding)


def _finish_recovery(
    parent: Path, binding_existed: bool
) -> None:
    _write_transaction_marker(
        parent, "committed", binding_existed
    )
    _fsync_directory(parent)
    _cleanup_after_commit(parent)


def _recover_transaction(
    model_path: Path, binding_path: Path
) -> None:
    parent = model_path.parent
    marker = _load_transaction_marker(parent)
    if marker is None:
        if any(
            _optional_private_bytes(
                _recovery_path(parent, name),
                label="stack recovery file",
                limit=max(MAX_CONFIG_BYTES, MAX_BINDING_BYTES),
            )
            is not None
            for name in (_MODEL_ORIGINAL, _BINDING_ORIGINAL)
        ):
            _cleanup_after_commit(parent)
        return
    if marker.state == "pending":
        _restore_originals(
            model_path,
            binding_path,
            binding_existed=marker.binding_existed,
        )
        _finish_recovery(parent, marker.binding_existed)
        return
    _cleanup_after_commit(parent)


def _rollback_transaction(
    model_path: Path,
    binding_path: Path,
    *,
    binding_existed: bool,
) -> None:
    parent = model_path.parent
    marker_error: BaseException | None = None
    try:
        _write_transaction_marker(
            parent, "pending", binding_existed
        )
    except BaseException as error:
        marker_error = error
    try:
        _restore_originals(
            model_path,
            binding_path,
            binding_existed=binding_existed,
        )
        _finish_recovery(parent, binding_existed)
    except BaseException as error:
        raise StackStoreError(
            "stack transaction rollback failed"
        ) from error
    if marker_error is not None:
        raise StackStoreError(
            "stack transaction rollback marker failed"
        ) from marker_error


def _discard_staged(*paths: Path | None) -> None:
    for path in paths:
        try:
            _unlink(path)
        except BaseException:
            pass


def save_stack(
    snapshot: StackSnapshot,
    updated: NormalizedStacks,
    updated_bindings: StackBindings,
) -> None:
    if not isinstance(snapshot, StackSnapshot):
        raise StackStoreError("stack snapshot is invalid")
    model_path = getattr(snapshot, "_model_path", None)
    binding_path = getattr(snapshot, "_binding_path", None)
    if model_path is None or binding_path is None:
        raise StackStoreError("stack snapshot has no persistence paths")
    _save_stack_at(
        model_path,
        binding_path,
        snapshot,
        updated,
        updated_bindings,
    )


def planned_stack_digests(
    snapshot: StackSnapshot,
    updated: NormalizedStacks,
    updated_bindings: StackBindings,
) -> tuple[str, str | None]:
    """Return the exact digests that ``save_stack`` will publish."""
    if not isinstance(snapshot, StackSnapshot):
        raise StackStoreError("stack snapshot is invalid")
    model_payload = _serialize_stacks(updated)
    binding_payload = (
        None
        if snapshot.binding_digest is None
        and not updated_bindings.candidate_accounts
        else _serialize_bindings(updated_bindings)
    )
    return (
        hashlib.sha256(model_payload).hexdigest(),
        (
            None
            if binding_payload is None
            else hashlib.sha256(binding_payload).hexdigest()
        ),
    )


def restore_stack_files(
    model_path: Path,
    binding_path: Path,
    *,
    expected_stack_digest: str,
    expected_binding_digest: str | None,
    original_model: bytes | None,
    original_binding: bytes | None,
) -> None:
    """Restore exact installer snapshots if no later writer changed them."""
    model_path = Path(model_path)
    binding_path = Path(binding_path)
    parent = _require_shared_private_parent(model_path, binding_path)
    try:
        with stack_binding_transaction(binding_path) as transaction:
            with _model_lock(parent):
                _recover_transaction(model_path, binding_path)
                current = _file_bytes(
                    model_path,
                    label="model stacks",
                    limit=MAX_CONFIG_BYTES,
                    required_mode=0o600,
                )
                if (
                    hashlib.sha256(current).hexdigest()
                    != expected_stack_digest
                    or transaction.digest() != expected_binding_digest
                ):
                    raise StackStoreError(
                        "stack files changed after installer activation"
                    )
                if original_model is None:
                    _unlink(binding_path)
                    _unlink(model_path)
                    _fsync_directory(parent)
                    return
                _replace_private_file(
                    _recovery_path(parent, _MODEL_ORIGINAL),
                    original_model,
                )
                if original_binding is None:
                    _unlink(_recovery_path(parent, _BINDING_ORIGINAL))
                else:
                    _replace_private_file(
                        _recovery_path(parent, _BINDING_ORIGINAL),
                        original_binding,
                    )
                _write_transaction_marker(
                    parent,
                    "pending",
                    original_binding is not None,
                )
                _fsync_directory(parent)
                _recover_transaction(model_path, binding_path)
    except StackStoreError:
        raise
    except StackBindingError as error:
        raise StackStoreError(str(error)) from error


def _save_stack_at(
    model_path: Path,
    binding_path: Path,
    snapshot: StackSnapshot,
    updated: NormalizedStacks,
    updated_bindings: StackBindings,
) -> None:
    parent = _require_shared_private_parent(
        model_path, binding_path
    )
    durable_commit = False
    try:
        with stack_binding_transaction(binding_path) as transaction:
            with _model_lock(parent):
                _recover_transaction(model_path, binding_path)
                current_model = _file_bytes(
                    model_path,
                    label="model stacks",
                    limit=MAX_CONFIG_BYTES,
                    required_mode=0o600,
                )
                if (
                    hashlib.sha256(current_model).hexdigest()
                    != snapshot.stack_digest
                    or transaction.digest() != snapshot.binding_digest
                ):
                    raise StackStoreError(
                        "stack files changed during update"
                    )
                model_payload = _serialize_stacks(updated)
                _validate_bindings(
                    updated, updated_bindings, binding_path
                )
                original_binding = (
                    None
                    if snapshot.binding_digest is None
                    else _file_bytes(
                        binding_path,
                        label="stack bindings",
                        limit=MAX_BINDING_BYTES,
                        required_mode=0o600,
                    )
                )
                binding_payload = (
                    None
                    if original_binding is None
                    and not updated_bindings.candidate_accounts
                    else _serialize_bindings(updated_bindings)
                )
                staged_model: Path | None = None
                staged_binding: Path | None = None
                recovery_ready = False
                binding_existed = original_binding is not None
                try:
                    staged_model = _stage(
                        model_path, model_payload, 0o600
                    )
                    if binding_payload is not None:
                        staged_binding = _stage(
                            binding_path, binding_payload, 0o600
                        )
                    _replace_private_file(
                        _recovery_path(parent, _MODEL_ORIGINAL),
                        current_model,
                    )
                    if binding_existed:
                        _replace_private_file(
                            _recovery_path(
                                parent, _BINDING_ORIGINAL
                            ),
                            original_binding,
                        )
                    else:
                        _unlink(
                            _recovery_path(
                                parent, _BINDING_ORIGINAL
                            )
                        )
                    _write_transaction_marker(
                        parent, "pending", binding_existed
                    )
                    recovery_ready = True
                    _fsync_directory(parent)
                    if staged_binding is not None:
                        os.replace(staged_binding, binding_path)
                        staged_binding = None
                    os.replace(staged_model, model_path)
                    staged_model = None
                    _fsync_directory(parent)
                    _write_transaction_marker(
                        parent, "committed", binding_existed
                    )
                    _fsync_directory(parent)
                    durable_commit = True
                except BaseException as error:
                    _discard_staged(staged_model, staged_binding)
                    if recovery_ready:
                        try:
                            _rollback_transaction(
                                model_path,
                                binding_path,
                                binding_existed=binding_existed,
                            )
                        except BaseException as rollback_error:
                            raise StackStoreError(
                                "stack transaction rollback failed"
                            ) from rollback_error
                    else:
                        _cleanup_after_commit(parent)
                    if isinstance(error, StackStoreError):
                        raise
                    raise StackStoreError(
                        "stack files could not be saved"
                    ) from error
                finally:
                    _discard_staged(staged_model, staged_binding)
                _cleanup_after_commit(parent)
    except BaseException as error:
        if durable_commit:
            return
        if isinstance(error, StackStoreError):
            raise
        raise StackStoreError("stack files could not be saved") from error


def delete_stack(
    snapshot: StackSnapshot,
    name: str,
    projects: Mapping[str, object],
) -> tuple[NormalizedStacks, StackBindings]:
    if name == snapshot.stacks.default_stack:
        raise StackStoreError("cannot delete the default stack")
    if name not in snapshot.stacks.stacks:
        raise StackStoreError("model stack is unknown")
    contexts = projects.get("contexts")
    if not isinstance(contexts, list):
        raise StackStoreError("projects document is invalid")
    references = [
        str(context.get("root", "project"))
        for context in contexts
        if isinstance(context, Mapping)
        and context.get("modelStack") == name
    ]
    if references:
        raise StackStoreError(
            f"stack is referenced by {references[0]}"
        )
    document = serialize_model_stacks(snapshot.stacks)
    del document["stacks"][name]
    updated = normalize_model_stacks(document)
    candidates = _candidate_ids(updated)
    bindings = StackBindings(
        {
            candidate: account
            for candidate, account in (
                snapshot.bindings.candidate_accounts.items()
            )
            if candidate in candidates
        }
    )
    return updated, bindings


def _provider_supports_family(
    provider: object, family: str
) -> bool:
    families = (
        provider.get("families")
        if isinstance(provider, Mapping)
        else provider
    )
    return (
        isinstance(families, (list, tuple, set, frozenset))
        and family in families
    )


def validate_stack_assignment(
    stack: str,
    context: Mapping[str, object],
    stacks: NormalizedStacks,
    bindings: StackBindings,
    accounts: Sequence[Account],
    providers: Mapping[str, object],
    catalog: LiveCatalog,
) -> None:
    selected = stacks.stacks.get(stack)
    if selected is None:
        raise StackStoreError("model stack is unknown")
    raw_pools = context.get("accountPools")
    if (
        not isinstance(raw_pools, list)
        or not raw_pools
        or any(
            not isinstance(pool, str) or not pool
            for pool in raw_pools
        )
    ):
        raise StackStoreError(
            "project context has no valid account pools"
        )
    provider_map = providers.get("providers", providers)
    if not isinstance(provider_map, Mapping):
        raise StackStoreError("provider configuration is invalid")
    active = {
        account.id: account
        for account in accounts
        if account.state == "active"
        and account.pool in raw_pools
        and account.provider in provider_map
    }
    live_routes = {
        (
            choice.family,
            choice.provider,
            choice.upstream,
            account_id,
        )
        for choice in catalog.choices
        for account_id in choice.account_ids
    }

    def viable(candidate: StackCandidate) -> bool:
        model = stacks.models[candidate.model]
        locked_id = bindings.candidate_accounts.get(candidate.id)
        candidate_accounts = (
            (active.get(locked_id),)
            if locked_id is not None
            else tuple(active.values())
        )
        return any(
            account is not None
            and account.provider in candidate.providers
            and account.provider in model.routes
            and _provider_supports_family(
                provider_map[account.provider], model.family
            )
            and (
                model.family,
                account.provider,
                model.routes[account.provider],
                account.id,
            )
            in live_routes
            for account in candidate_accounts
        )

    selected_candidates = (
        selected.controller,
        *selected.agents.values(),
    )
    for candidate in (
        item
        for candidates in selected_candidates
        for item in candidates
    ):
        if (
            candidate.id in bindings.candidate_accounts
            and not viable(candidate)
        ):
            raise StackStoreError(
                f"stack {stack} has a non-viable locked candidate"
            )
    if not any(viable(candidate) for candidate in selected.controller):
        raise StackStoreError("stack has no viable controller candidate")
    for role, candidates in selected.agents.items():
        if not any(viable(candidate) for candidate in candidates):
            raise StackStoreError(
                f"stack has no viable candidate for agent role {role}"
            )
