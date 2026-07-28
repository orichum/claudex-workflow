#!/usr/bin/env python3
"""Transactional migration from the early XDG layout into ORICHUM_HOME."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
from typing import Iterable


class HomeLayoutError(RuntimeError):
    """The Orichum home layout could not be migrated safely."""


SCHEMA_VERSION = 1
JOURNAL_NAME = "home-migration.json"


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


def _safe_directory(path: Path, *, allow_missing: bool) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise HomeLayoutError(f"required directory is missing: {path}")
    except OSError as error:
        raise HomeLayoutError(f"directory is unavailable: {path}") from error
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
    ):
        raise HomeLayoutError(f"directory is unsafe: {path}")
    return True


def _validate_target_home(home: Path) -> None:
    if not home.is_absolute() or home == Path(home.anchor):
        raise HomeLayoutError("Orichum home must be an absolute private path")
    cursor = Path(home.anchor)
    for component in home.parts[1:]:
        cursor /= component
        try:
            value = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise HomeLayoutError(
                "Orichum home ancestor is unavailable"
            ) from error
        if stat.S_ISLNK(value.st_mode):
            raise HomeLayoutError("Orichum home ancestors must not be symlinks")
        if cursor != home and not stat.S_ISDIR(value.st_mode):
            raise HomeLayoutError(
                "Orichum home ancestor is not a directory"
            )
    if _safe_directory(home, allow_missing=True):
        home.chmod(0o700)


def _write_journal(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    candidate = path.with_name(
        f".{path.name}.candidate.{secrets.token_hex(8)}"
    )
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        payload = _canonical(document)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(candidate, path)


def _read_journal(path: Path) -> dict[str, object]:
    try:
        value = path.lstat()
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HomeLayoutError("home migration journal is unavailable") from error
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
        or not isinstance(document, dict)
        or document.get("schemaVersion") != SCHEMA_VERSION
        or document.get("phase") not in {"prepared", "committing"}
        or not isinstance(document.get("moves"), list)
        or not isinstance(document.get("createdHome"), bool)
    ):
        raise HomeLayoutError("home migration journal is invalid")
    for move in document["moves"]:
        if (
            not isinstance(move, dict)
            or set(move) != {"source", "target", "moved"}
            or not isinstance(move["source"], str)
            or not isinstance(move["target"], str)
            or not isinstance(move["moved"], bool)
            or not Path(move["source"]).is_absolute()
            or not Path(move["target"]).is_absolute()
        ):
            raise HomeLayoutError("home migration journal move is invalid")
    return document


def _same_filesystem(source: Path, target_parent: Path) -> None:
    source_device = source.lstat().st_dev
    cursor = target_parent
    while not cursor.exists():
        if cursor == cursor.parent:
            raise HomeLayoutError("migration target parent is unavailable")
        cursor = cursor.parent
    if cursor.lstat().st_dev != source_device:
        raise HomeLayoutError(
            "existing Orichum data cannot be moved atomically to ORICHUM_HOME; "
            "choose a home on the same filesystem"
        )


def _compatibility_link(source: Path, target: Path) -> None:
    source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if source.exists() or source.is_symlink():
        raise HomeLayoutError(f"migration source was recreated: {source}")
    source.symlink_to(target, target_is_directory=True)


def prepare(
    home: Path,
    legacy_data: Path,
    legacy_config: Path,
    legacy_cache: Path,
    journal_root: Path,
) -> Path | None:
    """Move legacy roots and leave reversible compatibility links in place."""
    home = home.absolute()
    legacy_paths = tuple(
        path.absolute()
        for path in (legacy_data, legacy_config, legacy_cache)
    )
    journal = journal_root.absolute() / JOURNAL_NAME
    _validate_target_home(home)
    if journal.exists() or journal.is_symlink():
        existing = _read_journal(journal)
        if existing["phase"] == "committing":
            commit(journal)
        else:
            rollback(journal)

    plan: list[tuple[Path, Path]] = []
    targets = (home, home / "config", home / "cache")
    for source, target in zip(legacy_paths, targets, strict=True):
        if source == target:
            continue
        source_exists = _safe_directory(source, allow_missing=True)
        target_exists = _safe_directory(target, allow_missing=True)
        if source_exists and target_exists:
            raise HomeLayoutError(
                f"both legacy and consolidated Orichum roots exist: "
                f"{source} and {target}"
            )
        if source_exists:
            _same_filesystem(source, target.parent)
            plan.append((source, target))

    if not plan:
        home.mkdir(parents=True, mode=0o700, exist_ok=True)
        home.chmod(0o700)
        return None

    created_home = not home.exists() and plan[0][1] != home
    document: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "phase": "prepared",
        "createdHome": created_home,
        "moves": [
            {
                "source": str(source),
                "target": str(target),
                "moved": False,
            }
            for source, target in plan
        ],
    }
    _write_journal(journal, document)
    try:
        for index, (source, target) in enumerate(plan):
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if target.parent == home:
                target.parent.chmod(0o700)
            os.replace(source, target)
            target.chmod(0o700)
            document["moves"][index]["moved"] = True
            _write_journal(journal, document)
            _compatibility_link(source, target)
        home.chmod(0o700)
    except Exception:
        rollback(journal)
        raise
    return journal


def rollback(journal: Path) -> None:
    """Restore the original XDG roots after an interrupted/failed install."""
    journal = journal.absolute()
    document = _read_journal(journal)
    if document["phase"] == "committing":
        commit(journal)
        return
    moves = document["moves"]
    for move in reversed(moves):
        source = Path(move["source"])
        target = Path(move["target"])
        if source.is_symlink():
            if source.resolve(strict=False) != target.resolve(strict=False):
                raise HomeLayoutError(
                    f"migration compatibility link changed: {source}"
                )
            source.unlink()
        elif source.exists():
            _safe_directory(source, allow_missing=False)
            if target.exists() or target.is_symlink():
                raise HomeLayoutError(
                    f"both migration source and target exist: "
                    f"{source} and {target}"
                )
            move["moved"] = False
            _write_journal(journal, document)
            continue
        if target.exists() or target.is_symlink():
            _safe_directory(target, allow_missing=False)
            source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.replace(target, source)
            move["moved"] = False
            _write_journal(journal, document)
        elif move["moved"]:
            raise HomeLayoutError(
                f"migration source and target are both missing: {source}"
            )
    home = Path(moves[0]["target"])
    if home.name in {"config", "cache"}:
        home = home.parent
    if document["createdHome"] and home.exists() and not any(home.iterdir()):
        home.rmdir()
    journal.unlink()


def commit(journal: Path) -> None:
    """Remove compatibility links after the new runtime is healthy."""
    journal = journal.absolute()
    document = _read_journal(journal)
    for move in document["moves"]:
        source = Path(move["source"])
        target = Path(move["target"])
        _safe_directory(target, allow_missing=False)
        if source.exists() and not source.is_symlink():
            raise HomeLayoutError(
                f"migration source was recreated: {source}"
            )
        if source.is_symlink() and \
           source.resolve(strict=True) != target.resolve(strict=True):
            raise HomeLayoutError(
                f"migration compatibility link changed: {source}"
            )
        if not source.is_symlink() and document["phase"] == "prepared":
            raise HomeLayoutError(
                f"migration compatibility link is missing: {source}"
            )
    if document["phase"] == "prepared":
        document["phase"] = "committing"
        _write_journal(journal, document)
    for move in document["moves"]:
        source = Path(move["source"])
        if source.is_symlink():
            source.unlink()
    journal.unlink()
