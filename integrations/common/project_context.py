#!/usr/bin/env python3
"""Resolve immutable workflow context from a physical launch directory."""

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


class ContextError(RuntimeError):
    pass


def _expand(value: str, home: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContextError("configured path must be a non-empty string")
    expanded = (
        Path(str(home) + value[1:])
        if value == "~" or value.startswith("~/")
        else Path(value)
    )
    return expanded.expanduser()


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _git_root(launch_real: Path) -> Optional[str]:
    completed = subprocess.run(
        ["git", "-C", str(launch_real), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if completed.returncode != 0:
        return None
    root = Path(completed.stdout.strip()).resolve(strict=True)
    return str(root) if _contains(root, launch_real) else None


def _validate_palace_candidate(
    configured: Path,
) -> tuple[Optional[Path], Optional[str]]:
    if not configured.is_absolute():
        raise ContextError("configured palace must expand to an absolute path")
    cursor = Path(configured.anchor)
    for component in configured.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            return None, "palace_symlink"
    try:
        real = configured.resolve(strict=True)
        palace_stat = real.stat()
    except FileNotFoundError:
        return None, "palace_missing"
    except PermissionError:
        return None, "palace_inaccessible"
    except OSError:
        return None, "palace_inaccessible"
    if not real.is_dir() or not stat.S_ISDIR(palace_stat.st_mode):
        return None, "palace_not_directory"
    if palace_stat.st_uid != os.getuid():
        return None, "palace_owner"
    if stat.S_IMODE(palace_stat.st_mode) & 0o077:
        return None, "palace_permissions"
    return real, None


def _require_exact_keys(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContextError(f"{label} must contain exactly {sorted(expected)}")
    return value


def _require_non_blank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextError(f"{label} must be a non-empty string")
    return value


def _structural_path(value: object, home: Path, label: str) -> Path:
    value = _require_non_blank(value, label)
    if value == "~" or value.startswith("~/"):
        expanded = Path(home) / value[2:] if value != "~" else Path(home)
    elif value.startswith("~"):
        raise ContextError(f"{label} uses unsupported tilde syntax")
    else:
        expanded = Path(value)
    if not expanded.is_absolute():
        raise ContextError(f"{label} must be absolute or use ~/ syntax")
    return Path(os.path.normpath(str(expanded)))


def validate_config_structure(
    config_path: Path, home: Optional[Path] = None
) -> None:
    """Validate closed routing syntax without accessing configured paths."""
    home = Path.home() if home is None else Path(home)
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("configuration could not be parsed") from error

    config = _require_exact_keys(raw, {"palacePath", "contexts"}, "configuration")
    _structural_path(config["palacePath"], home, "palacePath")
    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    lexical_roots = set()
    memory_wings = set()
    for index, raw_context in enumerate(raw_contexts):
        context = _require_exact_keys(
            raw_context,
            {"root", "dockerProfile", "memoryWing"},
            f"context {index}",
        )
        root = _structural_path(context["root"], home, "root")
        _require_non_blank(context["dockerProfile"], "dockerProfile")
        memory_wing = _require_non_blank(context["memoryWing"], "memoryWing")
        if root in lexical_roots:
            raise ContextError("configured roots must be lexically unique")
        if memory_wing in memory_wings:
            raise ContextError("memoryWing values must be unique")
        lexical_roots.add(root)
        memory_wings.add(memory_wing)


def load_config(config_path: Path, home: Optional[Path] = None) -> dict:
    """Load and fully validate immutable routing configuration."""
    home = Path.home() if home is None else Path(home)
    with Path(config_path).open(encoding="utf-8") as handle:
        raw = json.load(handle)

    config = _require_exact_keys(raw, {"palacePath", "contexts"}, "configuration")
    palace_path = _expand(config["palacePath"], home)
    if not palace_path.is_absolute():
        raise ContextError("configured palace must expand to an absolute path")

    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    contexts = []
    canonical_roots = set()
    memory_wings = set()
    for index, raw_context in enumerate(raw_contexts):
        context = _require_exact_keys(
            raw_context,
            {"root", "dockerProfile", "memoryWing"},
            f"context {index}",
        )
        root_path = _expand(context["root"], home)
        if not root_path.is_absolute():
            raise ContextError("configured root must expand to an absolute path")
        try:
            root_real = root_path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ContextError("configured root must resolve to a directory") from error
        if not root_real.is_dir():
            raise ContextError("configured root must resolve to a directory")

        docker_profile = _require_non_blank(
            context["dockerProfile"], "dockerProfile"
        )
        memory_wing = _require_non_blank(context["memoryWing"], "memoryWing")
        if root_real in canonical_roots:
            raise ContextError("configured roots must be canonically unique")
        if memory_wing in memory_wings:
            raise ContextError("memoryWing values must be unique")
        canonical_roots.add(root_real)
        memory_wings.add(memory_wing)
        contexts.append(
            {
                "root": root_real,
                "dockerProfile": docker_profile,
                "memoryWing": memory_wing,
            }
        )

    return {"palacePath": palace_path, "contexts": contexts}


def resolve_context(config: dict, launch_dir: Path) -> dict:
    """Resolve one fixed route and one independent Git root for a launch."""
    launch_real = Path(launch_dir).resolve(strict=True)
    if not launch_real.is_dir():
        raise ContextError("launch directory must resolve to a directory")

    matches = [
        context
        for context in config["contexts"]
        if _contains(context["root"], launch_real)
    ]
    matches.sort(key=lambda context: len(context["root"].parts), reverse=True)

    route = None
    if matches:
        selected = matches[0]
        palace_real, failure_code = _validate_palace_candidate(
            config["palacePath"]
        )
        route = {
            "id": selected["memoryWing"],
            "contextRootReal": str(selected["root"]),
            "dockerProfile": selected["dockerProfile"],
            "memoryWing": selected["memoryWing"],
            "memoryAvailable": failure_code is None,
            "memoryFailureCode": failure_code,
            "palacePathReal": str(palace_real) if palace_real is not None else None,
        }

    return {
        "schemaVersion": 1,
        "launchDirReal": str(launch_real),
        "repoRootReal": _git_root(launch_real),
        "route": route,
    }


def _atomic_json(output: Path, payload: dict) -> None:
    output = Path(output)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        os.close(file_descriptor) if _descriptor_is_open(file_descriptor) else None
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _descriptor_is_open(file_descriptor: int) -> bool:
    try:
        os.fstat(file_descriptor)
        return True
    except OSError:
        return False


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "validate-config":
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=("validate-config",))
        parser.add_argument("--config", required=True, type=Path)
        arguments = parser.parse_args()
        try:
            validate_config_structure(arguments.config)
        except ContextError:
            print("ERROR: project context configuration rejected", file=sys.stderr)
            return 1
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()

    payload = resolve_context(load_config(arguments.config), arguments.launch_dir)
    _atomic_json(arguments.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
