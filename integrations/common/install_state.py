#!/usr/bin/env python3
"""Private verified state for fast Orichum installer reconciliation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Sequence


class InstallStateError(RuntimeError):
    """Installer state could not be handled safely."""


SCHEMA_VERSION = 1
MAX_STATE_BYTES = 64 * 1024
COMPONENTS = frozenset({
    "python",
    "cliproxy",
    "claudex",
    "leanctx",
    "routing",
    "completion",
    "controllerPlugin",
})
_TOP_LEVEL_KEYS = frozenset({
    "schemaVersion",
    "platform",
    "components",
})
_COMPONENT_KEYS = frozenset({
    "version",
    "sourceIdentity",
    "artifactSha256",
    "inputSha256",
    "probeSha256",
})


def _canonical_bytes(document: object) -> bytes:
    return (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _load_json(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def _printable(value: object, limit: int) -> bool:
    return (
        isinstance(value, str)
        and type(value) is str
        and 0 < len(value.encode("utf-8")) <= limit
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_components(value: object) -> bool:
    if type(value) is not dict:
        return False
    for name, component in value.items():
        if name not in COMPONENTS or type(component) is not dict:
            return False
        if frozenset(component) != _COMPONENT_KEYS:
            return False
        if not _printable(component["version"], 128):
            return False
        if not _printable(component["sourceIdentity"], 256):
            return False
        if not all(
            _digest(component[key])
            for key in (
                "artifactSha256",
                "inputSha256",
                "probeSha256",
            )
        ):
            return False
    return True


def _valid_document(document: object, platform: str) -> bool:
    return (
        type(document) is dict
        and frozenset(document) == _TOP_LEVEL_KEYS
        and type(document["schemaVersion"]) is int
        and document["schemaVersion"] == SCHEMA_VERSION
        and document["platform"] == platform
        and _printable(document["platform"], 64)
        and _valid_components(document["components"])
    )


def _private_parent(path: Path) -> Path:
    requested = Path(path).absolute().parent
    try:
        before = os.lstat(requested)
        resolved = requested.resolve(strict=True)
        after = os.lstat(requested)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise InstallStateError(
            "install state parent is unavailable"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o700
        or resolved != requested
        or (before.st_dev, before.st_ino)
        != (after.st_dev, after.st_ino)
        or (after.st_dev, after.st_ino)
        != (confirmed.st_dev, confirmed.st_ino)
    ):
        raise InstallStateError("install state parent is unsafe")
    return resolved


def _read_state(path: Path) -> bytes | None:
    parent = _private_parent(path)
    target = parent / Path(path).name
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise InstallStateError(
            "install state is unavailable"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
    ):
        raise InstallStateError("install state is unsafe")
    if stat.S_IMODE(before.st_mode) != 0o600:
        return None
    if before.st_size > MAX_STATE_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise InstallStateError(
            "install state could not be opened safely"
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
            raise InstallStateError("install state changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65536, MAX_STATE_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_STATE_BYTES:
                return None
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise InstallStateError("install state changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_manifest(
    path: Path,
    platform: str,
) -> dict[str, object] | None:
    """Load a trusted manifest or return None for ordinary unverified state."""
    if not _printable(platform, 64):
        return None
    payload = _read_state(path)
    if payload is None:
        return None
    try:
        document = _load_json(payload)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not _valid_document(document, platform):
        return None
    return document


def snapshot_unvalidated_state(
    path: Path,
    snapshot_root: Path,
    name: str,
) -> None:
    """Snapshot one bounded owned file without trusting its JSON schema."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise InstallStateError("install state snapshot name is invalid")
    parent = _private_parent(path)
    target = parent / Path(path).name
    snapshot_root = Path(snapshot_root).absolute()
    try:
        root_stat = os.lstat(snapshot_root)
        root_real = snapshot_root.resolve(strict=True)
        root_confirmed = os.lstat(root_real)
    except (OSError, RuntimeError) as error:
        raise InstallStateError(
            "install state snapshot directory is unavailable"
        ) from error
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
        or (root_stat.st_dev, root_stat.st_ino)
        != (root_confirmed.st_dev, root_confirmed.st_ino)
    ):
        raise InstallStateError(
            "install state snapshot directory is unsafe"
        )
    snapshot_root = root_real
    data_path = snapshot_root / f"{name}.data"
    present_path = snapshot_root / f"{name}.present"
    absent_path = snapshot_root / f"{name}.absent"
    if any(
        os.path.lexists(candidate)
        for candidate in (data_path, present_path, absent_path)
    ):
        raise InstallStateError("install state snapshot already exists")
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        descriptor = os.open(
            absent_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fsync(descriptor)
        os.close(descriptor)
        return
    except OSError as error:
        raise InstallStateError(
            "install state is unavailable"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_size > MAX_STATE_BYTES
    ):
        raise InstallStateError(
            "install state cannot be snapshotted safely"
        )
    descriptor = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_size > MAX_STATE_BYTES
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise InstallStateError("install state changed while opening")
        chunks: list[bytes] = []
        size = 0
        while size < opened.st_size:
            chunk = os.read(
                descriptor,
                min(65536, opened.st_size - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size != opened.st_size:
            raise InstallStateError("install state changed while reading")
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise InstallStateError("install state changed while reading")
    finally:
        os.close(descriptor)
    data_descriptor = os.open(
        data_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        stat.S_IMODE(before.st_mode),
    )
    try:
        os.fchmod(data_descriptor, stat.S_IMODE(before.st_mode))
        offset = 0
        while offset < len(payload):
            offset += os.write(data_descriptor, payload[offset:])
        os.fsync(data_descriptor)
    finally:
        os.close(data_descriptor)
    marker_descriptor = os.open(
        present_path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fsync(marker_descriptor)
    os.close(marker_descriptor)


def _existing_target_is_replaceable(path: Path) -> None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InstallStateError(
            "install state is unavailable"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
    ):
        raise InstallStateError("install state is unsafe")


def write_manifest(
    path: Path,
    platform: str,
    components: dict[str, object],
) -> None:
    """Atomically publish a private verified installer manifest."""
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "platform": platform,
        "components": components,
    }
    if not _valid_document(document, platform):
        raise InstallStateError("install state document is invalid")
    parent = _private_parent(path)
    target = parent / Path(path).name
    _existing_target_is_replaceable(target)
    payload = _canonical_bytes(document)
    temporary = parent / (
        f".install-state.{os.getpid()}.{secrets.token_hex(8)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    parent_descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.replace(temporary, target)
        except OSError as error:
            raise InstallStateError(
                "install state could not be replaced"
            ) from error
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        os.fsync(parent_descriptor)
    except InstallStateError:
        raise
    except OSError as error:
        raise InstallStateError(
            "install state could not be written safely"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _read_candidate(path: Path) -> bytes:
    candidate = Path(path).absolute()
    try:
        observed = os.lstat(candidate)
    except OSError as error:
        raise InstallStateError(
            "install state candidate is unavailable"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_size > MAX_STATE_BYTES
    ):
        raise InstallStateError("install state candidate is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise InstallStateError(
            "install state candidate could not be opened"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_size > MAX_STATE_BYTES
            or (opened.st_dev, opened.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise InstallStateError(
                "install state candidate changed while opening"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(65536, MAX_STATE_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_STATE_BYTES:
                raise InstallStateError(
                    "install state candidate is too large"
                )
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise InstallStateError(
                "install state candidate changed while reading"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _fingerprint_file(
    root: Path,
    relative: Path,
    digest,
) -> None:
    candidate = root / relative
    try:
        observed = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InstallStateError("fingerprint input is unavailable") from error
    if (
        resolved != candidate
        or stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
    ):
        raise InstallStateError("fingerprint input is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise InstallStateError(
            "fingerprint input could not be opened"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or (opened.st_dev, opened.st_ino)
            != (observed.st_dev, observed.st_ino)
        ):
            raise InstallStateError(
                "fingerprint input changed while opening"
            )
        name = relative.as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(stat.S_IMODE(opened.st_mode).to_bytes(4, "big"))
        digest.update(opened.st_size.to_bytes(8, "big"))
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise InstallStateError(
                "fingerprint input changed while reading"
            )
    finally:
        os.close(descriptor)


def fingerprint_paths(
    root: Path,
    paths: Sequence[Path],
) -> str:
    """Fingerprint stable owned regular files beneath one physical root."""
    requested_root = Path(root).absolute()
    try:
        observed_root = os.lstat(requested_root)
        physical_root = requested_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise InstallStateError("fingerprint root is unavailable") from error
    if (
        stat.S_ISLNK(observed_root.st_mode)
        or not stat.S_ISDIR(observed_root.st_mode)
        or observed_root.st_uid != os.getuid()
        or physical_root != requested_root
    ):
        raise InstallStateError("fingerprint root is unsafe")
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        relative = Path(value)
        if relative.is_absolute() or relative == Path("."):
            raise InstallStateError("fingerprint path must be relative")
        key = relative.as_posix()
        if key in seen:
            raise InstallStateError("fingerprint path is duplicated")
        seen.add(key)
        normalized.append(relative)
    if not normalized:
        raise InstallStateError("fingerprint requires at least one path")
    digest = hashlib.sha256()
    for relative in sorted(normalized, key=lambda item: item.as_posix()):
        _fingerprint_file(
            physical_root,
            relative,
            digest,
        )
    return digest.hexdigest()


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InstallStateError("invalid install-state arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="install_state")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("path")
    read.add_argument("platform")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("path")
    snapshot.add_argument("snapshot_root")
    snapshot.add_argument("name")
    write = commands.add_parser("write")
    write.add_argument("path")
    write.add_argument("platform")
    write.add_argument("components_json")
    fingerprint = commands.add_parser("fingerprint")
    fingerprint.add_argument("root")
    fingerprint.add_argument("paths", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "read":
            document = load_manifest(
                Path(arguments.path),
                arguments.platform,
            )
            if document is None:
                return 1
            sys.stdout.write(_canonical_bytes(document).decode("utf-8"))
            return 0
        if arguments.command == "snapshot":
            snapshot_unvalidated_state(
                Path(arguments.path),
                Path(arguments.snapshot_root),
                arguments.name,
            )
            return 0
        if arguments.command == "write":
            candidate = _read_candidate(
                Path(arguments.components_json)
            )
            try:
                components = _load_json(candidate)
            except (
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise InstallStateError(
                    "install state candidate is invalid"
                ) from error
            write_manifest(
                Path(arguments.path),
                arguments.platform,
                components,
            )
            return 0
        digest = fingerprint_paths(
            Path(arguments.root),
            [Path(value) for value in arguments.paths],
        )
        print(digest)
        return 0
    except InstallStateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("ERROR: install state operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
