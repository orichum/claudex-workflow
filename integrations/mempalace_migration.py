#!/usr/bin/env python3
"""Migrate the two approved project memories through MemPalace's MCP surface."""

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROUTES = {
    "xebia": "xebia",
    "wing_xebia": "xebia",
    "complion": "complion",
    "wing_complion": "complion",
}
EXPECTED_RECORD_COUNTS = {"xebia": 63, "complion": 27}


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PathIdentity:
    canonical_path: Path
    existing_path: Path
    existing_device: int
    existing_inode: int
    missing_suffix: tuple[str, ...]
    case_sensitive: bool


def route_drawers(drawers: Iterable[dict]) -> dict[str, list[dict]]:
    routed = {"xebia": [], "complion": []}
    for original in drawers:
        target = ROUTES.get(original.get("wing"))
        if target is None:
            continue
        item = dict(original)
        item["wing"] = target
        routed[target].append(item)
    return routed


def fingerprint_drawers(drawers: Iterable[dict]) -> str:
    records = sorted(
        (str(item.get("wing", "")), str(item.get("room", "")),
         str(item.get("content", "")))
        for item in drawers
    )
    encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def drawer_fingerprint(drawer: dict) -> str:
    encoded = json.dumps(
        [str(drawer.get("wing", "")), str(drawer.get("room", "")),
         str(drawer.get("content", ""))],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _case_variant(name: str) -> str | None:
    for index, character in enumerate(name):
        if character.isalpha():
            replacement = character.upper() if character.islower() \
                else character.lower()
            if replacement != character:
                return name[:index] + replacement + name[index + 1:]
    return None


def _filesystem_is_case_sensitive(existing_path: Path) -> bool:
    cursor = existing_path.resolve(strict=True)
    while True:
        variant_name = _case_variant(cursor.name)
        if variant_name is not None:
            variant = cursor.with_name(variant_name)
            try:
                variant_stat = variant.stat()
            except FileNotFoundError:
                return True
            except OSError:
                pass
            else:
                return not os.path.samestat(cursor.stat(), variant_stat)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    try:
        entries = list(existing_path.iterdir()) if existing_path.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        variant_name = _case_variant(entry.name)
        if variant_name is None:
            continue
        try:
            variant_stat = entry.with_name(variant_name).stat()
        except FileNotFoundError:
            return True
        except OSError:
            continue
        return not os.path.samestat(entry.stat(), variant_stat)
    # Unknown must not make case-variant paths appear distinct during a
    # destructive-boundary preflight. Normalizing is the fail-closed choice.
    return False


def _normalized_component(component: str, case_sensitive: bool) -> str:
    if case_sensitive:
        return component
    return unicodedata.normalize("NFC", component).casefold()


def _path_identity(
    target: Path, label: str, *, must_exist: bool = False
) -> PathIdentity:
    if not target.is_absolute():
        raise MigrationError(f"{label} path must be absolute")
    normalized = Path(os.path.normpath(str(target)))
    components = normalized.parts[1:]
    existing = Path(normalized.anchor)
    missing_start = len(components)
    for index, component in enumerate(components):
        candidate = existing / component
        try:
            value = candidate.lstat()
        except FileNotFoundError:
            missing_start = index
            break
        except OSError as error:
            raise MigrationError(f"{label} path ancestor is inaccessible") from error
        if stat.S_ISLNK(value.st_mode):
            raise MigrationError(f"{label} path ancestors must not be symlinks")
        if index < len(components) - 1 and not stat.S_ISDIR(value.st_mode):
            raise MigrationError(f"{label} path ancestor is not a directory")
        existing = candidate
    if must_exist and missing_start != len(components):
        raise MigrationError(f"{label} must resolve to an existing directory")

    canonical_existing = existing.resolve(strict=True)
    case_sensitive = _filesystem_is_case_sensitive(canonical_existing)
    raw_suffix = components[missing_start:]
    missing_suffix = tuple(
        _normalized_component(component, case_sensitive)
        for component in raw_suffix
    )
    value = canonical_existing.stat()
    return PathIdentity(
        canonical_path=canonical_existing.joinpath(*raw_suffix),
        existing_path=canonical_existing,
        existing_device=value.st_dev,
        existing_inode=value.st_ino,
        missing_suffix=missing_suffix,
        case_sensitive=case_sensitive,
    )


def _existing_relative_parts(
    root: PathIdentity, candidate: PathIdentity
) -> tuple[str, ...] | None:
    cursor = candidate.existing_path
    parts: list[str] = []
    while True:
        value = cursor.stat()
        if value.st_dev == root.existing_device and \
           value.st_ino == root.existing_inode:
            return tuple(reversed(parts))
        if cursor.parent == cursor:
            return None
        parts.append(cursor.name)
        cursor = cursor.parent


def _identity_contains(root: PathIdentity, candidate: PathIdentity) -> bool:
    relative_existing = _existing_relative_parts(root, candidate)
    if relative_existing is None:
        return False
    candidate_suffix = tuple(
        _normalized_component(component, root.case_sensitive)
        for component in relative_existing
    ) + candidate.missing_suffix
    root_suffix = root.missing_suffix
    return candidate_suffix[:len(root_suffix)] == root_suffix


def _identities_equal(first: PathIdentity, second: PathIdentity) -> bool:
    relative_existing = _existing_relative_parts(first, second)
    if relative_existing is not None:
        second_suffix = tuple(
            _normalized_component(component, first.case_sensitive)
            for component in relative_existing
        ) + second.missing_suffix
        if first.missing_suffix == second_suffix:
            return True
    relative_existing = _existing_relative_parts(second, first)
    if relative_existing is None:
        return False
    first_suffix = tuple(
        _normalized_component(component, second.case_sensitive)
        for component in relative_existing
    ) + first.missing_suffix
    return second.missing_suffix == first_suffix


def _canonical_target_identity(target: Path, label: str) -> PathIdentity:
    identity = _path_identity(target, f"{label} target")
    root_identity = _path_identity(
        Path(identity.canonical_path.anchor), "filesystem root", must_exist=True
    )
    home_identity = _path_identity(
        Path.home().resolve(), "home", must_exist=True
    )
    if _identities_equal(identity, root_identity) or \
       _identities_equal(identity, home_identity):
        raise MigrationError(f"{label} target is unsafe")
    return identity


def validate_migration_paths(
    source: Path, xebia_target: Path, complion_target: Path
) -> tuple[Path, Path, Path]:
    try:
        source_canonical = source.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise MigrationError("source must resolve to an existing directory") from error
    if not source_canonical.is_dir():
        raise MigrationError("source must resolve to an existing directory")
    source_identity = _path_identity(
        source_canonical, "source", must_exist=True
    )
    xebia_identity = _canonical_target_identity(xebia_target, "xebia")
    complion_identity = _canonical_target_identity(complion_target, "complion")
    identities = (source_identity, xebia_identity, complion_identity)
    for index, first in enumerate(identities):
        for second in identities[index + 1:]:
            if _identity_contains(first, second) or \
               _identity_contains(second, first):
                raise MigrationError("source and targets must be distinct and non-overlapping")
    return tuple(identity.canonical_path for identity in identities)


class PalaceClient:
    def __init__(self, executable: str, palace: Path):
        self.process = subprocess.Popen(
            [executable, "--palace", str(palace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self.request_id = 0
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claudex-migration", "version": "1"},
        })
        self._notify("notifications/initialized", {})

    def _notify(self, method: str, params: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        }, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        request_id = self.request_id
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params,
        }, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise MigrationError("MemPalace MCP exited before responding")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise MigrationError(str(response["error"].get("message", "tool failed")))
            return response["result"]

    def call(self, name: str, arguments: dict) -> dict:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        if not isinstance(content, list) or not content or "text" not in content[0]:
            raise MigrationError("MemPalace returned an invalid tool result")
        value = json.loads(content[0]["text"])
        if isinstance(value, dict) and value.get("error"):
            raise MigrationError(str(value["error"]))
        return value

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        self.close()


def read_wing(client: PalaceClient, wing: str) -> list[dict]:
    summaries = []
    offset = 0
    while True:
        page = client.call("mempalace_list_drawers", {
            "wing": wing, "limit": 100, "offset": offset,
        })
        batch = page.get("drawers", [])
        if not isinstance(batch, list):
            raise MigrationError("invalid drawer listing")
        summaries.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(page.get("total", offset)):
            break
    drawers = []
    for summary in summaries:
        drawer_id = summary.get("drawer_id")
        if not isinstance(drawer_id, str) or not drawer_id:
            raise MigrationError("drawer listing omitted an identifier")
        drawers.append(client.call("mempalace_get_drawer", {"drawer_id": drawer_id}))
    return drawers


def read_selected(executable: str, palace: Path) -> dict[str, list[dict]]:
    source = []
    with PalaceClient(executable, palace) as client:
        for wing in ROUTES:
            source.extend(read_wing(client, wing))
    return route_drawers(source)


def read_record_counts(executable: str, palace: Path) -> dict[str, int]:
    with PalaceClient(executable, palace) as client:
        taxonomy = client.call("mempalace_get_taxonomy", {}).get("taxonomy", {})
    counts = {"xebia": 0, "complion": 0}
    for source_wing, target in ROUTES.items():
        rooms = taxonomy.get(source_wing, {}) if isinstance(taxonomy, dict) else {}
        if isinstance(rooms, dict):
            counts[target] += sum(int(value) for value in rooms.values())
    return counts


def write_target(executable: str, palace: Path, drawers: list[dict]) -> None:
    with PalaceClient(executable, palace) as client:
        for item in drawers:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            arguments = {
                "wing": item["wing"], "room": item["room"],
                "content": item["content"], "added_by": "claudex-migration",
            }
            source_file = metadata.get("source_file")
            if isinstance(source_file, str) and source_file:
                arguments["source_file"] = source_file
            client.call("mempalace_add_drawer", arguments)


def preflight_target(
    executable: str,
    target: Path,
    name: str,
    expected_drawers: list[dict],
    expected_record_count: int,
) -> list[dict]:
    if not target.exists():
        return list(expected_drawers)
    if not target.is_dir():
        raise MigrationError(f"{name} target is not a directory")
    routed = read_selected(executable, target)
    record_counts = read_record_counts(executable, target)
    other = "complion" if name == "xebia" else "xebia"
    if routed[other] or record_counts[other] != 0:
        raise MigrationError(f"{name} target contains data for another project")

    expected = Counter(drawer_fingerprint(item) for item in expected_drawers)
    observed = Counter(drawer_fingerprint(item) for item in routed[name])
    if observed - expected:
        raise MigrationError(f"{name} target contains unexpected or excess drawers")

    observed_records = record_counts[name]
    if observed == expected:
        if observed_records != expected_record_count:
            raise MigrationError(f"{name} target record count does not match the source")
        return []
    if not observed:
        if observed_records != 0:
            raise MigrationError(f"{name} empty target has a nonzero record count")
    elif observed_records <= 0 or observed_records >= expected_record_count:
        raise MigrationError(f"{name} partial target record count is inconsistent")

    remaining = expected - observed
    missing = []
    for item in expected_drawers:
        fingerprint = drawer_fingerprint(item)
        if remaining[fingerprint]:
            missing.append(item)
            remaining[fingerprint] -= 1
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--source", type=Path, default=Path.home() / ".mempalace/palace")
    parser.add_argument("--xebia-target", type=Path,
                        default=Path.home() / ".mempalace/palaces/xebia")
    parser.add_argument("--complion-target", type=Path,
                        default=Path.home() / ".mempalace/palaces/complion")
    arguments = parser.parse_args()
    executable = shutil.which("mempalace-mcp")
    if executable is None:
        raise MigrationError("mempalace-mcp is unavailable")
    arguments.source, arguments.xebia_target, arguments.complion_target = \
        validate_migration_paths(
            arguments.source, arguments.xebia_target, arguments.complion_target
        )

    source_record_counts = read_record_counts(executable, arguments.source)
    if source_record_counts != EXPECTED_RECORD_COUNTS:
        raise MigrationError(
            f"selected source record counts changed: expected {EXPECTED_RECORD_COUNTS}, "
            f"observed {source_record_counts}"
        )
    routed = read_selected(executable, arguments.source)
    observed_counts = {name: len(values) for name, values in routed.items()}
    expected_hashes = {name: fingerprint_drawers(values)
                       for name, values in routed.items()}

    if arguments.execute:
        targets = (("xebia", arguments.xebia_target),
                   ("complion", arguments.complion_target))
        missing_drawers = {
            name: preflight_target(
                executable, target, name, routed[name], source_record_counts[name]
            )
            for name, target in targets
        }
        for name, target in targets:
            if not missing_drawers[name]:
                continue
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target, 0o700)
            write_target(executable, target, missing_drawers[name])
        for name, target in targets:
            if preflight_target(
                executable, target, name, routed[name], source_record_counts[name]
            ):
                raise MigrationError(f"{name} target verification failed")

    print(json.dumps({
        "mode": "execute" if arguments.execute else "dry-run",
        "logicalDrawers": observed_counts, "storageRecords": source_record_counts,
        "fingerprints": expected_hashes,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MigrationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
