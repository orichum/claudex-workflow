#!/usr/bin/env python3
"""Private, serialized persistence for portable plugin declarations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, Iterator, Mapping


MAX_PLUGIN_BYTES = 2 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PLUGIN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
)


class PluginRegistryError(RuntimeError):
    """Plugin declarations failed safe validation or persistence."""


@dataclass(frozen=True)
class _RegistryDomain:
    descriptor: int
    manifest_name: str


@contextmanager
def _registry_domain(path: Path) -> Iterator[_RegistryDomain]:
    path = Path(path)
    try:
        manifest = os.lstat(path)
        resolved_path = path.resolve(strict=True)
        resolved_manifest = os.lstat(resolved_path)
        parent = resolved_path.parent
        observed = os.lstat(parent)
        resolved = parent.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise PluginRegistryError(
            "plugin registry is unavailable"
        ) from error
    if (
        stat.S_ISLNK(manifest.st_mode)
        or not stat.S_ISREG(manifest.st_mode)
        or manifest.st_uid != os.getuid()
        or stat.S_IMODE(manifest.st_mode) != 0o600
        or (manifest.st_dev, manifest.st_ino)
        != (resolved_manifest.st_dev, resolved_manifest.st_ino)
    ):
        raise PluginRegistryError("plugin registry is unsafe")
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
    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise PluginRegistryError(
            "plugin registry parent is unavailable"
        ) from error
    try:
        opened = os.fstat(descriptor)
        current = os.stat(
            resolved_path.name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
            or (opened.st_dev, opened.st_ino)
            != (observed.st_dev, observed.st_ino)
            or (current.st_dev, current.st_ino)
            != (manifest.st_dev, manifest.st_ino)
        ):
            raise PluginRegistryError(
                "plugin registry changed while opening"
            )
        yield _RegistryDomain(descriptor, resolved_path.name)
    except OSError as error:
        raise PluginRegistryError(
            "plugin registry changed while opening"
        ) from error
    finally:
        os.close(descriptor)


def _read(domain: _RegistryDomain) -> bytes:
    try:
        details = os.stat(
            domain.manifest_name,
            dir_fd=domain.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise PluginRegistryError("plugin registry is unavailable") from error
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            domain.manifest_name,
            flags,
            dir_fd=domain.descriptor,
        )
    except OSError as error:
        raise PluginRegistryError(
            "plugin registry could not be opened safely"
        ) from error
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


def _load(domain: _RegistryDomain) -> dict[str, object]:
    try:
        document = json.loads(_read(domain).decode("utf-8"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise PluginRegistryError("plugin registry is invalid") from error
    return _validate(document)


def _write(
    domain: _RegistryDomain, document: Mapping[str, object]
) -> None:
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
    descriptor = -1
    temporary: str | None = None
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for _ in range(64):
            candidate = f".plugins.json.{secrets.token_hex(8)}"
            try:
                descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=domain.descriptor,
                )
                temporary = candidate
                break
            except FileExistsError:
                continue
        if descriptor < 0 or temporary is None:
            raise PluginRegistryError(
                "plugin registry staging name is unavailable"
            )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("plugin registry write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            domain.manifest_name,
            src_dir_fd=domain.descriptor,
            dst_dir_fd=domain.descriptor,
        )
        temporary = None
        os.fsync(domain.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=domain.descriptor)
            except FileNotFoundError:
                pass


@contextmanager
def plugin_registry_lock(path: Path) -> Iterator[_RegistryDomain]:
    """Serialize an exact plugin-registry state check and mutation."""
    with _registry_domain(Path(path)) as domain:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                ".plugins.lock",
                flags,
                0o600,
                dir_fd=domain.descriptor,
            )
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
                raise PluginRegistryError(
                    "plugin registry lock is unsafe"
                )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield domain
        finally:
            os.close(descriptor)


def update_plugins(
    path: Path,
    transform: Callable[[dict[str, object]], Mapping[str, object]],
) -> dict[str, object]:
    """Update the plugin declaration under the shared registry lock."""
    with plugin_registry_lock(Path(path)) as domain:
        updated = _validate(transform(_load(domain)))
        _write(domain, updated)
        return updated
