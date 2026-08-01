#!/usr/bin/env python3
"""Build and activate the small, immutable Orichum runtime payload."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
from typing import Iterable


class RuntimeBundleError(RuntimeError):
    """The runtime payload could not be built or activated safely."""


RUNTIME_FILES = (
    "VERSION",
    "controller/settings.json",
    "discover-models.sh",
    "doctor.sh",
    "install.sh",
)
RUNTIME_TREES = (
    "bin",
    "config",
    "controller/plugin",
    "integrations",
    "lib",
)
MANIFEST_NAME = "runtime-manifest.json"
BUILD_IDENTITY_NAME = "build-identity.json"
BUILD_IDENTITY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 1


def _canonical(document: object) -> bytes:
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_file(source_root: Path, relative: Path) -> Path:
    candidate = source_root / relative
    try:
        value = candidate.lstat()
    except OSError as error:
        raise RuntimeBundleError(
            f"runtime source is unavailable: {relative}"
        ) from error
    if stat.S_ISLNK(value.st_mode):
        raise RuntimeBundleError(
            f"runtime source must not be a symlink: {relative}"
        )
    if not stat.S_ISREG(value.st_mode):
        raise RuntimeBundleError(
            f"runtime source is not a regular file: {relative}"
        )
    return candidate


def _payload_paths(source_root: Path) -> tuple[Path, ...]:
    paths: list[Path] = [Path(name) for name in RUNTIME_FILES]
    for tree_name in RUNTIME_TREES:
        tree = source_root / tree_name
        try:
            tree_value = tree.lstat()
        except OSError as error:
            raise RuntimeBundleError(
                f"runtime source tree is unavailable: {tree_name}"
            ) from error
        if stat.S_ISLNK(tree_value.st_mode) or not stat.S_ISDIR(
            tree_value.st_mode
        ):
            raise RuntimeBundleError(
                f"runtime source tree is unsafe: {tree_name}"
            )
        for directory, names, filenames in os.walk(
            tree, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            safe_names: list[str] = []
            for name in sorted(names):
                child = directory_path / name
                value = child.lstat()
                if stat.S_ISLNK(value.st_mode):
                    raise RuntimeBundleError(
                        "runtime source must not contain symlinked "
                        f"directories: {child.relative_to(source_root)}"
                    )
                if name == "__pycache__":
                    continue
                if not stat.S_ISDIR(value.st_mode):
                    raise RuntimeBundleError(
                        "runtime source contains an invalid directory entry: "
                        f"{child.relative_to(source_root)}"
                    )
                safe_names.append(name)
            names[:] = safe_names
            for name in sorted(filenames):
                if name.endswith(".pyc"):
                    continue
                relative = (directory_path / name).relative_to(source_root)
                _safe_source_file(source_root, relative)
                paths.append(relative)
    return tuple(sorted(set(paths), key=lambda item: item.as_posix()))


def _entries(root: Path, relative_paths: Iterable[Path]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for relative in relative_paths:
        path = root / relative
        value = path.lstat()
        entries.append(
            {
                "mode": stat.S_IMODE(value.st_mode),
                "path": relative.as_posix(),
                "sha256": _sha256(path),
                "size": value.st_size,
            }
        )
    return entries


def _set_private_directory_modes(root: Path) -> None:
    for directory, names, _ in os.walk(root):
        Path(directory).chmod(0o700)
        for name in names:
            child = Path(directory) / name
            if child.is_symlink():
                raise RuntimeBundleError(
                    f"runtime payload contains a symlink: {child}"
                )


def _git_output(source_root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(source_root), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def _source_identity(
    source_root: Path,
    relative_paths: tuple[Path, ...],
) -> dict[str, object]:
    try:
        version = _safe_source_file(
            source_root, Path("VERSION")
        ).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeBundleError("runtime version is unavailable") from error
    identity: dict[str, object] = {
        "schemaVersion": BUILD_IDENTITY_SCHEMA_VERSION,
        "version": version,
        "sourceKind": "source",
        "sourceCommit": None,
        "dirty": False,
        "exactTag": False,
    }
    try:
        top_level = Path(
            _git_output(source_root, "rev-parse", "--show-toplevel")
        ).resolve(strict=True)
        if top_level != source_root:
            return identity
        commit = _git_output(source_root, "rev-parse", "HEAD")
        tracked_changes = _git_output(
            source_root,
            "diff",
            "--name-only",
            "HEAD",
            "--",
            *RUNTIME_FILES,
            *RUNTIME_TREES,
        )
        status = _git_output(
            source_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *(path.as_posix() for path in relative_paths),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return identity
    expected_tag = f"v{version}"
    try:
        tag = _git_output(
            source_root,
            "describe",
            "--tags",
            "--exact-match",
            "--match",
            expected_tag,
            "HEAD",
        )
    except (OSError, subprocess.SubprocessError):
        tag = ""
    identity.update({
        "sourceKind": "git",
        "sourceCommit": commit,
        "dirty": bool(tracked_changes or status),
        "exactTag": tag == expected_tag,
    })
    return identity


def _validated_embedded_identity(
    source_root: Path,
) -> dict[str, object] | None:
    try:
        validate(source_root)
        document = json.loads(
            _safe_source_file(
                source_root, Path(BUILD_IDENTITY_NAME)
            ).read_text(encoding="utf-8")
        )
        version = _safe_source_file(
            source_root, Path("VERSION")
        ).read_text(encoding="ascii").strip()
    except (
        RuntimeBundleError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return None
    if (
        type(document) is not dict
        or set(document) != {
            "schemaVersion",
            "version",
            "sourceKind",
            "sourceCommit",
            "dirty",
            "exactTag",
        }
        or type(document["schemaVersion"]) is not int
        or document["schemaVersion"] != BUILD_IDENTITY_SCHEMA_VERSION
        or document["version"] != version
        or type(document["dirty"]) is not bool
        or type(document["exactTag"]) is not bool
    ):
        return None
    source_kind = document["sourceKind"]
    commit = document["sourceCommit"]
    if source_kind == "source":
        return (
            document
            if commit is None
            and not document["dirty"]
            and not document["exactTag"]
            else None
        )
    if (
        source_kind != "git"
        or type(commit) is not str
        or len(commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        return None
    return document


def build(source_root: Path, staging_root: Path) -> Path:
    """Copy the declared runtime payload into a content-addressed staging tree."""
    source_root = source_root.resolve(strict=True)
    relative_paths = _payload_paths(source_root)
    staging_root = staging_root.absolute()
    if staging_root == Path(staging_root.anchor):
        raise RuntimeBundleError("runtime staging root is unsafe")
    staging_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise RuntimeBundleError("runtime staging root is unsafe")
    staging_root.chmod(0o700)

    candidate = staging_root / f".candidate.{secrets.token_hex(8)}"
    candidate.mkdir(mode=0o700)
    try:
        for relative in relative_paths:
            source = _safe_source_file(source_root, relative)
            destination = candidate / relative
            destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            source_mode = stat.S_IMODE(source.lstat().st_mode)
            mode = 0o755 if source_mode & 0o111 else 0o644
            with source.open("rb") as reader, destination.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            destination.chmod(mode)
        identity_path = candidate / BUILD_IDENTITY_NAME
        identity_path.write_bytes(
            _canonical(
                _validated_embedded_identity(source_root)
                or _source_identity(source_root, relative_paths)
            )
        )
        identity_path.chmod(0o644)
        _set_private_directory_modes(candidate)
        entries = _entries(
            candidate,
            (*relative_paths, Path(BUILD_IDENTITY_NAME)),
        )
        digest = hashlib.sha256(_canonical(entries)).hexdigest()
        manifest = {
            "schemaVersion": SCHEMA_VERSION,
            "digest": digest,
            "files": entries,
        }
        manifest_path = candidate / MANIFEST_NAME
        manifest_path.write_bytes(_canonical(manifest))
        manifest_path.chmod(0o600)
        release = staging_root / digest
        if release.exists():
            validate(release)
            shutil.rmtree(candidate)
        else:
            os.replace(candidate, release)
        validate(release)
        return release
    except Exception:
        shutil.rmtree(candidate, ignore_errors=True)
        raise


def _read_manifest(release: Path) -> dict[str, object]:
    manifest_path = release / MANIFEST_NAME
    try:
        value = manifest_path.lstat()
        payload = manifest_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeBundleError("runtime manifest is unavailable") from error
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or not isinstance(document, dict)
        or document.get("schemaVersion") != SCHEMA_VERSION
        or not isinstance(document.get("digest"), str)
        or not isinstance(document.get("files"), list)
    ):
        raise RuntimeBundleError("runtime manifest is invalid")
    return document


def validate(release: Path) -> str:
    """Validate an installed or staged release and return its digest."""
    release = release.absolute()
    try:
        release_value = release.lstat()
    except OSError as error:
        raise RuntimeBundleError("runtime release is unavailable") from error
    if (
        stat.S_ISLNK(release_value.st_mode)
        or not stat.S_ISDIR(release_value.st_mode)
        or release_value.st_uid != os.getuid()
    ):
        raise RuntimeBundleError("runtime release is unsafe")
    manifest = _read_manifest(release)
    digest = manifest["digest"]
    if release.name != digest:
        raise RuntimeBundleError("runtime release name does not match its digest")

    expected_paths: set[str] = {MANIFEST_NAME}
    validated_entries: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for raw_entry in manifest["files"]:
        if (
            not isinstance(raw_entry, dict)
            or set(raw_entry) != {"mode", "path", "sha256", "size"}
            or not isinstance(raw_entry["path"], str)
            or not isinstance(raw_entry["mode"], int)
            or not isinstance(raw_entry["size"], int)
            or not isinstance(raw_entry["sha256"], str)
            or raw_entry["mode"] not in {0o644, 0o755}
            or raw_entry["size"] < 0
            or len(raw_entry["sha256"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in raw_entry["sha256"]
            )
        ):
            raise RuntimeBundleError("runtime manifest entry is invalid")
        relative = Path(raw_entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeBundleError("runtime manifest path is invalid")
        if relative.as_posix() in seen_paths:
            raise RuntimeBundleError("runtime manifest contains duplicate paths")
        seen_paths.add(relative.as_posix())
        path = release / relative
        try:
            value = path.lstat()
        except OSError as error:
            raise RuntimeBundleError(
                f"runtime file is unavailable: {relative}"
            ) from error
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
        ):
            raise RuntimeBundleError(f"runtime file is unsafe: {relative}")
        if stat.S_IMODE(value.st_mode) != raw_entry["mode"]:
            raise RuntimeBundleError(f"runtime file mode mismatch: {relative}")
        if value.st_size != raw_entry["size"]:
            raise RuntimeBundleError(f"runtime file size mismatch: {relative}")
        if _sha256(path) != raw_entry["sha256"]:
            raise RuntimeBundleError(f"runtime file digest mismatch: {relative}")
        expected_paths.add(relative.as_posix())
        validated_entries.append(raw_entry)

    actual_paths = {
        path.relative_to(release).as_posix()
        for path in release.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_paths != expected_paths:
        raise RuntimeBundleError("runtime release contains unknown entries")
    if hashlib.sha256(_canonical(validated_entries)).hexdigest() != digest:
        raise RuntimeBundleError("runtime manifest digest mismatch")
    return digest


def _prepare_runtime_root(home: Path) -> tuple[Path, Path]:
    home = home.absolute()
    if home == Path(home.anchor):
        raise RuntimeBundleError("Orichum home is unsafe")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    if home.is_symlink() or not home.is_dir():
        raise RuntimeBundleError("Orichum home is unsafe")
    home.chmod(0o700)
    runtime = home / "runtime"
    releases = runtime / "releases"
    releases.mkdir(parents=True, mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    releases.chmod(0o700)
    return runtime, releases


def activate(staged_release: Path, home: Path) -> tuple[Path, Path | None]:
    """Install a validated release and atomically switch ``runtime/current``."""
    digest = validate(staged_release)
    runtime, releases = _prepare_runtime_root(home)
    release = releases / digest
    if release.exists():
        validate(release)
    else:
        candidate = releases / f".{digest}.candidate.{secrets.token_hex(8)}"
        try:
            shutil.copytree(staged_release, candidate, symlinks=False)
            _set_private_directory_modes(candidate)
            (candidate / MANIFEST_NAME).chmod(0o600)
            validate_candidate = candidate.with_name(digest)
            os.replace(candidate, validate_candidate)
        except OSError as error:
            shutil.rmtree(candidate, ignore_errors=True)
            if error.errno == errno.EXDEV:
                raise RuntimeBundleError(
                    "runtime staging and Orichum home use incompatible filesystems"
                ) from error
            raise
        release = releases / digest
        validate(release)

    current = runtime / "current"
    previous: Path | None = None
    if current.is_symlink():
        previous = current.resolve(strict=True)
        try:
            previous.relative_to(releases)
        except ValueError as error:
            raise RuntimeBundleError(
                "current runtime pointer escapes the release directory"
            ) from error
        validate(previous)
    elif current.exists():
        raise RuntimeBundleError("current runtime pointer is not a symlink")

    pointer = runtime / f".current.{secrets.token_hex(8)}"
    pointer.symlink_to(Path("releases") / digest)
    os.replace(pointer, current)
    return release, previous


def current_release(home: Path) -> Path | None:
    """Return the validated active release, if one is selected."""
    runtime, releases = _prepare_runtime_root(home)
    current = runtime / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeBundleError("current runtime pointer is not a symlink")
    release = current.resolve(strict=True)
    try:
        release.relative_to(releases)
    except ValueError as error:
        raise RuntimeBundleError(
            "current runtime pointer escapes the release directory"
        ) from error
    validate(release)
    return release


def restore(home: Path, previous: Path | None) -> None:
    """Restore the prior current pointer after a failed activation."""
    runtime, releases = _prepare_runtime_root(home)
    current = runtime / "current"
    if previous is None:
        current.unlink(missing_ok=True)
        return
    previous = previous.resolve(strict=True)
    try:
        relative = previous.relative_to(releases)
    except ValueError as error:
        raise RuntimeBundleError("previous runtime is outside Orichum home") from error
    validate(previous)
    pointer = runtime / f".current.{secrets.token_hex(8)}"
    pointer.symlink_to(Path("releases") / relative)
    os.replace(pointer, current)


def rollback_activation(
    home: Path,
    activated: Path,
    previous: Path | None,
) -> None:
    """Restore the pointer and remove only the uncommitted activated release."""
    activated = activated.resolve(strict=True)
    restore(home, previous)
    if previous is None or activated != previous.resolve(strict=True):
        validate(activated)
        shutil.rmtree(activated)
    runtime = home.absolute() / "runtime"
    releases = runtime / "releases"
    if releases.exists() and not any(releases.iterdir()):
        releases.rmdir()
    if runtime.exists() and not any(runtime.iterdir()):
        runtime.rmdir()


def rollback_attempt(
    home: Path,
    activated: Path,
    previous: Path | None,
) -> None:
    """Rollback an activation even if it failed before returning its result."""
    home = home.absolute()
    activated = activated.absolute()
    active = current_release(home)
    if previous is not None:
        previous = previous.resolve(strict=True)
        validate(previous)
    if active == activated.resolve(strict=False):
        restore(home, previous)
        active = previous
    elif active != previous:
        raise RuntimeBundleError(
            "runtime pointer changed during activation rollback"
        )
    if activated.exists():
        activated_resolved = activated.resolve(strict=True)
        if previous is None or activated_resolved != previous:
            validate(activated_resolved)
            shutil.rmtree(activated_resolved)
    runtime = home / "runtime"
    releases = runtime / "releases"
    if releases.exists() and not any(releases.iterdir()):
        releases.rmdir()
    if runtime.exists() and not any(runtime.iterdir()):
        runtime.rmdir()


def prune(home: Path, keep: Iterable[Path]) -> None:
    """Remove releases that are not part of the committed installation."""
    _, releases = _prepare_runtime_root(home)
    retained = {path.resolve(strict=True) for path in keep}
    for candidate in releases.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            raise RuntimeBundleError(
                f"unknown runtime release entry: {candidate}"
            )
        if candidate.resolve(strict=True) not in retained:
            shutil.rmtree(candidate)
