#!/usr/bin/env python3
"""Resolve immutable workflow context from a physical launch directory."""

import argparse
import contextlib
import fcntl
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


def _structural_existing_ancestor_path(
    path: Path, home: Path, label: str, *, reject_symlinks: bool
) -> Path:
    home_real = home.resolve(strict=False)
    lexical_root = Path(path.anchor)
    if path == lexical_root or path == home:
        raise ContextError(f"{label} is unsafe")
    cursor = lexical_root
    for component in path.parts[1:]:
        cursor /= component
        try:
            value = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise ContextError(f"{label} existing ancestor is inaccessible") from error
        if reject_symlinks and stat.S_ISLNK(value.st_mode):
            raise ContextError(f"{label} existing ancestors must not be symlinks")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ContextError(f"{label} could not be resolved") from error
    if canonical == Path(canonical.anchor) or canonical == home_real:
        raise ContextError(f"{label} is unsafe")
    return canonical


def _validate_config_value(raw: object, home: Path) -> None:
    config = _require_exact_keys(raw, {"contexts"}, "configuration")
    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    lexical_roots = []
    lexical_palaces = set()
    memory_wings = set()
    for index, raw_context in enumerate(raw_contexts):
        context = _require_exact_keys(
            raw_context,
            {"root", "dockerProfile", "memoryPalace", "memoryWing"},
            f"context {index}",
        )
        root = _structural_path(context["root"], home, "root")
        palace = _structural_path(context["memoryPalace"], home, "memoryPalace")
        _require_non_blank(context["dockerProfile"], "dockerProfile")
        memory_wing = _require_non_blank(context["memoryWing"], "memoryWing")
        if any(_contains(existing_root, root) or _contains(root, existing_root)
               for existing_root in lexical_roots):
            raise ContextError("configured roots must not overlap")
        if palace in lexical_palaces:
            raise ContextError("configured palaces must be lexically unique")
        if memory_wing in memory_wings:
            raise ContextError("memoryWing values must be unique")
        lexical_roots.append(root)
        lexical_palaces.add(palace)
        memory_wings.add(memory_wing)


def validate_config_structure(
    config_path: Path, home: Optional[Path] = None
) -> None:
    """Validate routing structure and existing path ancestors without mutation."""
    home = Path.home() if home is None else Path(home)
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("configuration could not be parsed") from error

    _validate_config_value(raw, home)
    for context in raw["contexts"]:
        root = _structural_path(context["root"], home, "root")
        palace = _structural_path(context["memoryPalace"], home, "memoryPalace")
        _structural_existing_ancestor_path(
            root, home, "root", reject_symlinks=False
        )
        _structural_existing_ancestor_path(
            palace, home, "memoryPalace", reject_symlinks=True
        )


def load_config(config_path: Path, home: Optional[Path] = None) -> dict:
    """Load and fully validate immutable routing configuration."""
    home = Path.home() if home is None else Path(home)
    with Path(config_path).open(encoding="utf-8") as handle:
        raw = json.load(handle)

    config = _require_exact_keys(raw, {"contexts"}, "configuration")

    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    contexts = []
    canonical_roots = set()
    canonical_palaces = set()
    memory_wings = set()
    for index, raw_context in enumerate(raw_contexts):
        context = _require_exact_keys(
            raw_context,
            {"root", "dockerProfile", "memoryPalace", "memoryWing"},
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
        if root_real == Path(root_real.anchor) or root_real == home.resolve(strict=False):
            raise ContextError("configured root is unsafe")

        palace_path = _expand(context["memoryPalace"], home)
        if not palace_path.is_absolute():
            raise ContextError("configured palace must expand to an absolute path")
        try:
            palace_real = palace_path.resolve(strict=True)
        except (OSError, RuntimeError):
            palace_real = None

        docker_profile = _require_non_blank(
            context["dockerProfile"], "dockerProfile"
        )
        memory_wing = _require_non_blank(context["memoryWing"], "memoryWing")
        if any(_contains(existing_root, root_real) or _contains(root_real, existing_root)
               for existing_root in canonical_roots):
            raise ContextError("configured roots must not overlap")
        if palace_real is not None and palace_real in canonical_palaces:
            raise ContextError("configured palaces must be canonically unique")
        if memory_wing in memory_wings:
            raise ContextError("memoryWing values must be unique")
        canonical_roots.add(root_real)
        if palace_real is not None:
            canonical_palaces.add(palace_real)
        memory_wings.add(memory_wing)
        contexts.append(
            {
                "root": root_real,
                "dockerProfile": docker_profile,
                "memoryPalace": palace_path,
                "memoryWing": memory_wing,
            }
        )

    return {"contexts": contexts}


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
            selected["memoryPalace"]
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


def _read_context_document(config_path: Path, home: Path) -> dict:
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("configuration could not be parsed") from error
    _validate_config_value(document, home)
    return document


def _write_context_document(config_path: Path, document: dict) -> None:
    config_path = Path(config_path)
    mode = stat.S_IMODE(config_path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", dir=config_path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    except BaseException:
        os.close(descriptor) if _descriptor_is_open(descriptor) else None
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def _context_lock(config_path: Path):
    descriptor = None
    try:
        descriptor = os.open(Path(config_path).parent, os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ContextError("configuration lock is unavailable") from error
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _context_root(value: str, home: Path, *, must_exist: bool) -> Path:
    root = _structural_path(value, home, "root")
    if root == Path(root.anchor) or root == home:
        raise ContextError("root is unsafe")
    if not must_exist:
        return root
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ContextError("root must resolve to an existing directory") from error
    if not resolved.is_dir():
        raise ContextError("root must resolve to an existing directory")
    if resolved == Path(resolved.anchor) or resolved == home.resolve(strict=False):
        raise ContextError("root is unsafe")
    return resolved


def _context_palace(value: str, home: Path) -> Path:
    palace = _structural_path(value, home, "memoryPalace")
    if palace == Path(palace.anchor) or palace == home:
        raise ContextError("memoryPalace is unsafe")
    return palace


def _ensure_new_palace(palace: Path) -> None:
    if palace.exists():
        return
    palace.mkdir(parents=True, mode=0o700)
    os.chmod(palace, 0o700)


def _prepare_palace(palace: Path) -> None:
    _structural_existing_ancestor_path(
        palace, Path.home(), "memoryPalace", reject_symlinks=True
    )
    try:
        _ensure_new_palace(palace)
    except OSError as error:
        raise ContextError("memoryPalace could not be created") from error
    _, failure_code = _validate_palace_candidate(palace)
    if failure_code is not None:
        raise ContextError("memoryPalace is unsafe")


def _validate_context_candidate(document: dict, home: Path) -> None:
    _validate_config_value(document, home)
    canonical_roots = []
    canonical_palaces = set()
    for context in document["contexts"]:
        root = _context_root(context["root"], home, must_exist=True)
        palace = _context_palace(context["memoryPalace"], home).resolve(strict=False)
        if any(_contains(existing_root, root) or _contains(root, existing_root)
               for existing_root in canonical_roots):
            raise ContextError("configured roots must not overlap")
        if palace in canonical_palaces:
            raise ContextError("configured palaces must be canonically unique")
        canonical_roots.append(root)
        canonical_palaces.add(palace)


def _find_context_index(contexts: list[dict], root: Path, home: Path) -> int:
    for index, context in enumerate(contexts):
        if _context_root(context["root"], home, must_exist=False) == root:
            return index
    raise ContextError("context root is not configured")


def _find_exact_context_index(contexts: list[dict], root: str) -> int:
    for index, context in enumerate(contexts):
        if context["root"] == root:
            return index
    raise ContextError("context root is not configured")


def context_main(arguments: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="claudex-context")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add")
    add.add_argument("root")
    add.add_argument("--docker", required=True)
    add.add_argument("--palace")
    add.add_argument("--wing")
    update = commands.add_parser("update")
    update.add_argument("root")
    update.add_argument("--docker")
    update.add_argument("--palace")
    update.add_argument("--wing")
    remove = commands.add_parser("remove")
    remove.add_argument("root")
    remove.add_argument("--yes", action="store_true")
    commands.add_parser("validate")
    parsed = parser.parse_args(arguments)
    home = Path.home()

    try:
        lock = (
            _context_lock(parsed.config)
            if parsed.command not in ("list", "validate")
            else contextlib.nullcontext()
        )
        with lock:
            document = _read_context_document(parsed.config, home)
            contexts = document["contexts"]
            if parsed.command == "validate":
                _validate_context_candidate(document, home)
                return 0
            if parsed.command == "list":
                print("ROOT\tDOCKER\tPALACE\tWING")
                for context in contexts:
                    print(
                        "{root}\t{dockerProfile}\t{memoryPalace}\t{memoryWing}".format(
                            **context
                        )
                    )
                return 0
            if parsed.command == "add":
                root = _context_root(parsed.root, home, must_exist=True)
                palace_value = parsed.palace or f"~/.mempalace/palaces/{root.name}"
                palace = _context_palace(palace_value, home)
                context = {
                    "root": str(root),
                    "dockerProfile": parsed.docker,
                    "memoryPalace": (
                        palace_value if parsed.palace is None else str(palace)
                    ),
                    "memoryWing": parsed.wing or root.name,
                }
                candidate = {"contexts": [*contexts, context]}
                _validate_context_candidate(candidate, home)
                _prepare_palace(palace)
                _write_context_document(parsed.config, candidate)
                return 0

            try:
                root = _context_root(parsed.root, home, must_exist=False)
                index = _find_context_index(contexts, root, home)
            except ContextError:
                if parsed.command != "remove":
                    raise
                index = _find_exact_context_index(contexts, parsed.root)
            if parsed.command == "update":
                if all(value is None for value in (parsed.docker, parsed.palace, parsed.wing)):
                    raise ContextError("update requires a replacement field")
                replacement = dict(contexts[index])
                if parsed.docker is not None:
                    replacement["dockerProfile"] = parsed.docker
                if parsed.palace is not None:
                    palace = _context_palace(parsed.palace, home)
                    replacement["memoryPalace"] = str(palace)
                else:
                    palace = None
                if parsed.wing is not None:
                    replacement["memoryWing"] = parsed.wing
                candidate = {"contexts": list(contexts)}
                candidate["contexts"][index] = replacement
                _validate_context_candidate(candidate, home)
                if palace is not None:
                    _prepare_palace(palace)
                _write_context_document(parsed.config, candidate)
                return 0

            context = contexts[index]
            print(json.dumps(context, indent=2))
            if not parsed.yes:
                try:
                    confirmation = input("Type REMOVE to confirm: ")
                except EOFError as error:
                    raise ContextError("remove requires confirmation") from error
                if confirmation != "REMOVE":
                    raise ContextError("remove requires confirmation")
            candidate = {"contexts": list(contexts)}
            del candidate["contexts"][index]
            _validate_config_value(candidate, home)
            _write_context_document(parsed.config, candidate)
            return 0
    except ContextError:
        print("ERROR: project context operation rejected", file=sys.stderr)
        return 1


def main(arguments: Optional[list[str]] = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments and arguments[0] == "context":
        return context_main(arguments[1:])
    if arguments and arguments[0] == "validate-config":
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=("validate-config",))
        parser.add_argument("--config", required=True, type=Path)
        parsed = parser.parse_args(arguments)
        try:
            validate_config_structure(parsed.config)
        except ContextError:
            print("ERROR: project context configuration rejected", file=sys.stderr)
            return 1
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parsed = parser.parse_args(arguments)

    payload = resolve_context(load_config(parsed.config), parsed.launch_dir)
    _atomic_json(parsed.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
