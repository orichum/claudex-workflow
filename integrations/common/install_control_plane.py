#!/usr/bin/env python3
"""Short-lived installer transactions for the live Orichum control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .account_registry import load_accounts
from .orichum_config import (
    MAX_CONFIG_BYTES,
    default_config_paths,
    load_control_plane,
)
from .project_context import control_plane_transaction
from .stack_bindings import MAX_BINDING_BYTES, load_stack_bindings
from .stack_definition import serialize_model_stacks
from .stack_store import (
    load_stack_snapshot,
    restore_stack_files,
    save_stack,
    validate_stack_bindings,
)


class InstallControlPlaneError(RuntimeError):
    """Installed control-plane migration failed closed."""


_BOOTSTRAP_FILES = (
    "projects.json",
    "providers.json",
    "plugins.json",
    "runtime.json",
    "controller-policy.md",
    "accounts.json",
)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _private_bytes(path: Path, label: str, limit: int) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise InstallControlPlaneError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise InstallControlPlaneError(f"{label} is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallControlPlaneError(
            f"{label} could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise InstallControlPlaneError(
                f"{label} changed while opening"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise InstallControlPlaneError(f"{label} is too large")
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
            raise InstallControlPlaneError(
                f"{label} changed while reading"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_private_root(path: Path) -> None:
    try:
        observed = os.lstat(path)
        resolved = path.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise InstallControlPlaneError(
            "installed configuration root is unavailable"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or (observed.st_dev, observed.st_ino)
        != (confirmed.st_dev, confirmed.st_ino)
        or resolved != path
    ):
        raise InstallControlPlaneError(
            "installed configuration root is unsafe"
        )


def _atomic_private(path: Path, payload: bytes, *, exclusive: bool) -> None:
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        temporary = None
    else:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("private write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if temporary is not None:
            os.replace(temporary, path)
            temporary = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _snapshot(path: Path, root: Path, name: str, limit: int) -> bytes | None:
    for suffix in ("data", "present", "absent"):
        try:
            (root / f"{name}.{suffix}").unlink()
        except FileNotFoundError:
            pass
    if not _lexists(path):
        _atomic_private(root / f"{name}.absent", b"", exclusive=True)
        return None
    payload = _private_bytes(path, name, limit)
    _atomic_private(root / f"{name}.data", payload, exclusive=True)
    _atomic_private(root / f"{name}.present", b"", exclusive=True)
    return payload


def _candidate_payload(
    repository_root: Path, installed_root: Path, name: str
) -> bytes:
    installed = installed_root / name
    if _lexists(installed):
        return _private_bytes(installed, name, MAX_CONFIG_BYTES)
    if name == "accounts.json":
        return b'{"schemaVersion":2,"accounts":[]}\n'
    return (repository_root / "config" / name).read_bytes()


def stage(
    repository_root: Path, installed_root: Path, candidate_root: Path
) -> None:
    repository_root = Path(repository_root).resolve(strict=True)
    installed_root = Path(installed_root).resolve(strict=True)
    candidate_root = Path(candidate_root).resolve(strict=False)
    _require_private_root(installed_root)
    if candidate_root.exists():
        shutil.rmtree(candidate_root)
    candidate_root.mkdir(mode=0o700, parents=True)
    with control_plane_transaction(installed_root):
        model_path = installed_root / "model-stacks.json"
        binding_path = installed_root / "stack-bindings.json"
        if _lexists(model_path):
            current = load_stack_snapshot(model_path, binding_path)
            bindings = load_stack_bindings(binding_path)
            validate_stack_bindings(
                current.stacks,
                bindings,
                load_accounts(installed_root / "accounts.json"),
            )
            model_payload = (
                json.dumps(
                    serialize_model_stacks(current.stacks),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
        else:
            if _lexists(binding_path):
                raise InstallControlPlaneError(
                    "stack bindings exist without model stacks"
                )
            model_payload = (
                repository_root / "config/model-stacks.json"
            ).read_bytes()
        payloads = {
            name: _candidate_payload(
                repository_root, installed_root, name
            )
            for name in _BOOTSTRAP_FILES
        }
        payloads["model-stacks.json"] = model_payload
        if _lexists(binding_path):
            payloads["stack-bindings.json"] = _private_bytes(
                binding_path, "stack bindings", MAX_BINDING_BYTES
            )
        for name, payload in payloads.items():
            destination = candidate_root / name
            destination.write_bytes(payload)
            destination.chmod(0o600)
    load_control_plane(default_config_paths(candidate_root))
    if (candidate_root / "stack-bindings.json").exists():
        load_stack_bindings(candidate_root / "stack-bindings.json")


def activate(
    candidate_root: Path,
    installed_root: Path,
    snapshot_root: Path,
) -> None:
    candidate_root = Path(candidate_root).resolve(strict=True)
    installed_root = Path(installed_root).resolve(strict=True)
    snapshot_root = Path(snapshot_root).resolve(strict=False)
    _require_private_root(installed_root)
    snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot_root.chmod(0o700)
    created: list[str] = []
    with control_plane_transaction(installed_root):
        model_path = installed_root / "model-stacks.json"
        binding_path = installed_root / "stack-bindings.json"
        if _lexists(model_path):
            load_stack_snapshot(model_path, binding_path)
        elif _lexists(binding_path):
            raise InstallControlPlaneError(
                "stack bindings exist without model stacks"
            )
        prior_model = _snapshot(
            model_path,
            snapshot_root,
            "installed-model-stacks",
            MAX_CONFIG_BYTES,
        )
        prior_binding = _snapshot(
            binding_path,
            snapshot_root,
            "installed-stack-bindings",
            MAX_BINDING_BYTES,
        )
        committed: tuple[str, str | None] | None = None
        try:
            for name in _BOOTSTRAP_FILES:
                destination = installed_root / name
                if _lexists(destination):
                    continue
                _atomic_private(
                    destination,
                    (candidate_root / name).read_bytes(),
                    exclusive=True,
                )
                created.append(name)
            if prior_model is None:
                _atomic_private(
                    model_path,
                    (candidate_root / "model-stacks.json").read_bytes(),
                    exclusive=True,
                )
            current = load_stack_snapshot(model_path, binding_path)
            bindings = load_stack_bindings(binding_path)
            validate_stack_bindings(
                current.stacks,
                bindings,
                load_accounts(installed_root / "accounts.json"),
            )
            save_stack(current, current.stacks, bindings)
            saved = load_stack_snapshot(model_path, binding_path)
            committed = (
                saved.stack_digest,
                saved.binding_digest,
            )
            manifest = {
                "schemaVersion": 1,
                "priorModelPresent": prior_model is not None,
                "priorBindingPresent": prior_binding is not None,
                "committedModelDigest": committed[0],
                "committedBindingDigest": committed[1],
                "bootstrapCreated": created,
            }
            _atomic_private(
                snapshot_root / "installed-control-plane.json",
                (
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                exclusive=False,
            )
            load_control_plane(default_config_paths(installed_root))
        except BaseException:
            if committed is not None:
                restore_stack_files(
                    model_path,
                    binding_path,
                    expected_stack_digest=committed[0],
                    expected_binding_digest=committed[1],
                    original_model=prior_model,
                    original_binding=prior_binding,
                )
            for name in reversed(created):
                try:
                    (installed_root / name).unlink()
                except FileNotFoundError:
                    pass
            if prior_model is None and committed is None:
                try:
                    model_path.unlink()
                except FileNotFoundError:
                    pass
            raise


def rollback(installed_root: Path, snapshot_root: Path) -> None:
    installed_root = Path(installed_root).resolve(strict=True)
    snapshot_root = Path(snapshot_root).resolve(strict=True)
    manifest = json.loads(
        _private_bytes(
            snapshot_root / "installed-control-plane.json",
            "installer control-plane manifest",
            MAX_CONFIG_BYTES,
        )
    )
    with control_plane_transaction(installed_root):
        original_model = (
            _private_bytes(
                snapshot_root / "installed-model-stacks.data",
                "installed model-stack snapshot",
                MAX_CONFIG_BYTES,
            )
            if manifest["priorModelPresent"]
            else None
        )
        original_binding = (
            _private_bytes(
                snapshot_root / "installed-stack-bindings.data",
                "installed stack-binding snapshot",
                MAX_BINDING_BYTES,
            )
            if manifest["priorBindingPresent"]
            else None
        )
        restore_stack_files(
            installed_root / "model-stacks.json",
            installed_root / "stack-bindings.json",
            expected_stack_digest=manifest["committedModelDigest"],
            expected_binding_digest=manifest["committedBindingDigest"],
            original_model=original_model,
            original_binding=original_binding,
        )
        for name in reversed(manifest["bootstrapCreated"]):
            try:
                (installed_root / name).unlink()
            except FileNotFoundError:
                pass
