#!/usr/bin/env python3
"""Private, serialized persistence for portable plugin declarations."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable, Iterator, Mapping


MAX_PLUGIN_BYTES = 2 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PLUGIN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
)


class PluginRegistryError(RuntimeError):
    """Plugin declarations failed safe validation or persistence."""


def _private_parent(path: Path) -> Path:
    parent = Path(path).parent
    try:
        observed = os.lstat(parent)
        resolved = parent.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise PluginRegistryError(
            "plugin registry parent is unavailable"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or (observed.st_dev, observed.st_ino)
        != (confirmed.st_dev, confirmed.st_ino)
        or resolved != parent
    ):
        raise PluginRegistryError("plugin registry parent is unsafe")
    return parent


def _read(path: Path) -> bytes:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise PluginRegistryError("plugin registry is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise PluginRegistryError("plugin registry is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (details.st_dev, details.st_ino)
        ):
            raise PluginRegistryError(
                "plugin registry changed while opening"
            )
        content = bytearray()
        while len(content) <= MAX_PLUGIN_BYTES:
            chunk = os.read(
                descriptor,
                min(65536, MAX_PLUGIN_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_PLUGIN_BYTES:
            raise PluginRegistryError("plugin registry is too large")
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
            raise PluginRegistryError(
                "plugin registry changed while reading"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _validate(document: object) -> dict[str, object]:
    if (
        not isinstance(document, dict)
        or set(document)
        != {"schemaVersion", "marketplaces", "plugins"}
        or document["schemaVersion"] != 1
        or not isinstance(document["marketplaces"], list)
        or not isinstance(document["plugins"], list)
    ):
        raise PluginRegistryError("plugin registry is invalid")
    marketplaces = document["marketplaces"]
    plugins = document["plugins"]
    names: list[str] = []
    for entry in marketplaces:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"name", "source"}
            or not isinstance(entry["name"], str)
            or _NAME.fullmatch(entry["name"]) is None
            or not isinstance(entry["source"], str)
            or not entry["source"]
            or entry["source"].startswith("-")
        ):
            raise PluginRegistryError("plugin registry is invalid")
        names.append(entry["name"])
    if len(names) != len(set(names)):
        raise PluginRegistryError("plugin registry is invalid")
    if (
        any(
            not isinstance(plugin, str)
            or _PLUGIN.fullmatch(plugin) is None
            for plugin in plugins
        )
        or len(plugins) != len(set(plugins))
        or any(plugin.rsplit("@", 1)[1] not in names for plugin in plugins)
    ):
        raise PluginRegistryError("plugin registry is invalid")
    return {
        "schemaVersion": 1,
        "marketplaces": [
            {"name": entry["name"], "source": entry["source"]}
            for entry in marketplaces
        ],
        "plugins": list(plugins),
    }


def _load(path: Path) -> dict[str, object]:
    try:
        document = json.loads(_read(path).decode("utf-8"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise PluginRegistryError("plugin registry is invalid") from error
    return _validate(document)


def _write(path: Path, document: Mapping[str, object]) -> None:
    payload = (
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    if len(payload) > MAX_PLUGIN_BYTES:
        raise PluginRegistryError("plugin registry is too large")
    descriptor, name = tempfile.mkstemp(
        prefix=".plugins.json.", dir=path.parent
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("plugin registry write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        temporary = None
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def plugin_registry_lock(path: Path) -> Iterator[None]:
    """Serialize an exact plugin-registry state check and mutation."""
    parent = _private_parent(Path(path))
    lock_path = parent / ".plugins.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise PluginRegistryError(
            "plugin registry lock is unavailable"
        ) from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise PluginRegistryError("plugin registry lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def update_plugins(
    path: Path,
    transform: Callable[[dict[str, object]], Mapping[str, object]],
) -> dict[str, object]:
    """Update the plugin declaration under the shared registry lock."""
    path = Path(path).resolve(strict=True)
    with plugin_registry_lock(path):
        updated = _validate(transform(_load(path)))
        _write(path, updated)
        return updated
