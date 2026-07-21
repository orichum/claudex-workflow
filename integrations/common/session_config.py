#!/usr/bin/env python3
"""Create and verify workflow-owned, digest-bound session state."""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from integrations.common.project_context import ContextError, load_config, resolve_context


class SessionError(RuntimeError):
    """Raised when session state does not satisfy its ownership boundary."""


@dataclass(frozen=True)
class SessionPaths:
    run_id: str
    run_dir: Path
    context_file: Path
    context_sha256: str
    mcp_file: Path


@dataclass(frozen=True)
class ContextBinding:
    """Descriptor-verified authority for one immutable session context."""

    workflow_root: Path
    run_id: str
    run_dir: Path
    context_file: Path
    context_sha256: str
    context: dict[str, object]


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _absolute_lexical(path: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path.cwd() / path


def _stable_lstat(path: Path) -> os.stat_result:
    try:
        first = os.lstat(path)
        second = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise SessionError("session component is unavailable") from error
    if not _same_object(first, second):
        raise SessionError("session component changed during validation")
    return second


def _require_directory(
    path: Path,
    *,
    parent: Optional[Path] = None,
    expected_mode: Optional[int] = None,
) -> Path:
    path = _absolute_lexical(path)
    observed = _stable_lstat(path)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise SessionError("session component must be a real directory")
    if observed.st_uid != os.getuid():
        raise SessionError("session component has an unexpected owner")
    if expected_mode is not None and stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session component has unsafe permissions")

    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SessionError("session component cannot be canonicalized") from error
    if real != path:
        raise SessionError("session component is not a canonical path")
    if parent is not None:
        parent_real = Path(parent).resolve(strict=True)
        if path.parent != parent_real or real.parent != parent_real:
            raise SessionError("session component is not a direct child")

    final = _stable_lstat(path)
    if not _same_object(observed, final):
        raise SessionError("session component changed during validation")
    return real


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_owned_component(
    parent: Path,
    name: str,
    *,
    private: bool,
    create: bool = False,
) -> Path:
    """Validate one fixed, current-UID-owned direct child directory."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise SessionError("invalid session component name")
    parent = _require_directory(parent)
    child = parent / name
    try:
        os.lstat(child)
    except FileNotFoundError:
        if not create:
            raise SessionError("required session component is missing")
        try:
            os.mkdir(child, 0o700)
            _fsync_directory(parent)
        except OSError as error:
            raise SessionError("session component could not be created") from error
    except OSError as error:
        raise SessionError("session component is unavailable") from error
    return _require_directory(
        child,
        parent=parent,
        expected_mode=0o700 if private else None,
    )


def require_private_direct_child(
    parent: Path, child: Path, *, expected_mode: int = 0o700
) -> Path:
    """Validate a private canonical directory directly below its parent."""
    parent = _require_directory(parent, expected_mode=0o700)
    child = _absolute_lexical(child)
    if child.parent != parent:
        raise SessionError("session directory is not a direct child")
    return _require_directory(child, parent=parent, expected_mode=expected_mode)


def _require_owned_file(parent: Path, path: Path, expected_mode: int) -> Path:
    parent = _require_directory(parent, expected_mode=0o700)
    path = _absolute_lexical(path)
    if path.parent != parent:
        raise SessionError("session file is not a direct child")
    observed = _stable_lstat(path)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise SessionError("session file must be a real regular file")
    if observed.st_uid != os.getuid():
        raise SessionError("session file has an unexpected owner")
    if stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session file has unsafe permissions")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SessionError("session file cannot be canonicalized") from error
    if real != path or real.parent != parent:
        raise SessionError("session file is not canonical")
    final = _stable_lstat(path)
    if not _same_object(observed, final):
        raise SessionError("session file changed during validation")
    return real


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _session_mcp_payload(context: dict[str, object]) -> dict[str, object]:
    """Expose only installed, project-relevant MCP servers for this session."""
    servers: dict[str, object] = {}
    route = context.get("route")

    if isinstance(route, dict):
        docker = shutil.which("docker")
        profile = route.get("dockerProfile")
        if docker and isinstance(profile, str) and profile:
            servers["docker"] = {
                "command": docker,
                "args": ["mcp", "gateway", "run", "--profile", profile],
            }

        mempalace = shutil.which("mempalace-mcp")
        palace = route.get("palacePathReal")
        if (
            mempalace
            and route.get("memoryAvailable") is True
            and isinstance(palace, str)
            and palace
        ):
            servers["mempalace"] = {
                "command": mempalace,
                "args": ["--palace", palace],
            }

    repo_root = context.get("repoRootReal")
    graphify = shutil.which("graphify-mcp")
    if graphify and isinstance(repo_root, str) and repo_root:
        repo = Path(repo_root)
        graph = repo / "graphify-out" / "graph.json"
        try:
            graph = graph.resolve(strict=True)
            graph.relative_to(repo)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            pass
        else:
            if graph.is_file():
                servers["graphify"] = {
                    "command": graphify,
                    "args": ["--graph", str(graph)],
                }

    return {"mcpServers": servers}


def atomic_json(path: Path, payload: dict, mode: int = 0o600) -> bytes:
    """Write canonical JSON through an exclusive no-follow same-directory file."""
    if mode != 0o600:
        raise SessionError("session files must use mode 0600")
    path = _absolute_lexical(path)
    parent = require_private_direct_child(
        path.parent.parent, path.parent, expected_mode=0o700
    )
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise SessionError("invalid session file path")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SessionError("session file path is unavailable") from error
    else:
        raise SessionError("session file already exists")

    data = _canonical_json_bytes(payload)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    temporary_name = f".{path.name}.{secrets.token_hex(12)}"
    file_fd: Optional[int] = None
    replaced = False
    try:
        parent_stat = os.fstat(directory_fd)
        if not _same_object(parent_stat, _stable_lstat(parent)):
            raise SessionError("session directory changed during file creation")
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        file_fd = os.open(temporary_name, open_flags, mode, dir_fd=directory_fd)
        os.fchmod(file_fd, mode)
        written = 0
        while written < len(data):
            written += os.write(file_fd, data[written:])
        os.fsync(file_fd)
        temporary_stat = os.fstat(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        os.fsync(directory_fd)
        final = _stable_lstat(path)
        if not _same_object(temporary_stat, final):
            raise SessionError("session file changed during installation")
        _require_owned_file(parent, path, mode)
    except OSError as error:
        raise SessionError("session file could not be written") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    return data


def _validate_file_stat(observed: os.stat_result, expected_mode: int) -> None:
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise SessionError("session file must be a real regular file")
    if observed.st_uid != os.getuid():
        raise SessionError("session file has an unexpected owner")
    if stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session file has unsafe permissions")


def _read_owned_file(
    parent: Path, file_name: str, expected_mode: int = 0o600
) -> bytes:
    """Read one fixed child once through a no-follow, parent-anchored descriptor."""
    if Path(file_name).name != file_name or file_name in {"", ".", ".."}:
        raise SessionError("invalid session file name")
    parent = require_private_direct_child(
        Path(parent).parent, parent, expected_mode=0o700
    )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise SessionError("no-follow file access is unavailable")
    directory_fd = os.open(parent, directory_flags | no_follow)
    file_fd: Optional[int] = None
    try:
        parent_before = os.fstat(directory_fd)
        if not _same_object(parent_before, _stable_lstat(parent)):
            raise SessionError("session directory changed before file read")
        try:
            path_before = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
            file_fd = os.open(
                file_name, os.O_RDONLY | no_follow, dir_fd=directory_fd
            )
        except OSError as error:
            raise SessionError("session file could not be opened safely") from error
        descriptor_before = os.fstat(file_fd)
        _validate_file_stat(path_before, expected_mode)
        _validate_file_stat(descriptor_before, expected_mode)
        if not _same_object(path_before, descriptor_before):
            raise SessionError("session file changed before reading")

        blocks = []
        while True:
            block = os.read(file_fd, 65536)
            if not block:
                break
            blocks.append(block)

        descriptor_after = os.fstat(file_fd)
        try:
            path_after = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except OSError as error:
            raise SessionError("session file changed during reading") from error
        _validate_file_stat(descriptor_after, expected_mode)
        _validate_file_stat(path_after, expected_mode)
        if not _same_object(descriptor_before, descriptor_after):
            raise SessionError("session file descriptor changed during reading")
        if not _same_object(descriptor_after, path_after):
            raise SessionError("session file path changed during reading")
        parent_after = os.fstat(directory_fd)
        if not _same_object(parent_before, parent_after) or not _same_object(
            parent_after, _stable_lstat(parent)
        ):
            raise SessionError("session directory changed during file read")
        return b"".join(blocks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_workflow_root(workflow_root: Path) -> Path:
    return _require_directory(_absolute_lexical(workflow_root))


def _validated_session_ancestors(workflow_root: Path) -> tuple[Path, Path, Path, Path]:
    workflow_root = _validated_workflow_root(workflow_root)
    runtime = require_owned_component(workflow_root, "runtime", private=False)
    state = require_owned_component(runtime, "state", private=True)
    sessions = require_owned_component(state, "sessions", private=True)
    return workflow_root, runtime, state, sessions


def create_session(
    workflow_root: Path, launch_dir: Path, config_path: Path
) -> SessionPaths:
    """Create one session with project-relevant MCP servers only."""
    workflow_root = _validated_workflow_root(workflow_root)
    runtime = require_owned_component(workflow_root, "runtime", private=False)
    state = require_owned_component(runtime, "state", private=True, create=True)
    sessions = require_owned_component(state, "sessions", private=True, create=True)
    try:
        run_dir = Path(tempfile.mkdtemp(prefix="run.", dir=sessions))
        _fsync_directory(sessions)
    except OSError as error:
        raise SessionError("session directory could not be created") from error
    run_dir = require_private_direct_child(sessions, run_dir, expected_mode=0o700)

    context_file = run_dir / "context.json"
    context = resolve_context(load_config(config_path), launch_dir)
    context_bytes = atomic_json(context_file, context, 0o600)
    context_sha256 = hashlib.sha256(context_bytes).hexdigest()
    mcp_file = run_dir / "mcp.json"
    atomic_json(mcp_file, _session_mcp_payload(context), 0o600)
    return verify_session(workflow_root, run_dir, context_sha256)


def verify_context_binding(
    workflow_root: Path,
    run_dir: Path,
    context_file: Path,
    context_sha256: str,
    run_id: str,
) -> ContextBinding:
    """Bind fixed authority fields to the exact verified context bytes."""
    workflow_root, _, _, sessions = _validated_session_ancestors(workflow_root)
    if (
        not isinstance(run_id, str)
        or not run_id.startswith("run.")
        or Path(run_id).name != run_id
        or run_id in {"", ".", ".."}
    ):
        raise SessionError("run identifier is invalid")
    run_dir = _absolute_lexical(run_dir)
    expected_run_dir = sessions / run_id
    if (
        run_dir != expected_run_dir
        or run_dir.parent != sessions
        or run_dir.name != run_id
    ):
        raise SessionError("run directory is not a managed direct child")
    run_dir = require_private_direct_child(sessions, run_dir, expected_mode=0o700)
    context_file = _absolute_lexical(context_file)
    if context_file != run_dir / "context.json":
        raise SessionError("context file is not the fixed session child")
    if (
        not isinstance(context_sha256, str)
        or len(context_sha256) != 64
        or any(character not in "0123456789abcdef" for character in context_sha256)
    ):
        raise SessionError("context digest is invalid")

    context_bytes = _read_owned_file(run_dir, "context.json", 0o600)
    observed_digest = hashlib.sha256(context_bytes).hexdigest()
    if not hmac.compare_digest(observed_digest, context_sha256):
        raise SessionError("context digest mismatch")
    try:
        context = json.loads(context_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SessionError("session context is invalid") from error
    if not isinstance(context, dict):
        raise SessionError("session context is invalid")
    return ContextBinding(
        workflow_root=workflow_root,
        run_id=run_id,
        run_dir=run_dir,
        context_file=context_file,
        context_sha256=context_sha256,
        context=context,
    )


def verify_session(
    workflow_root: Path, run_dir: Path, context_sha256: str
) -> SessionPaths:
    """Revalidate session context and its exact project MCP configuration."""
    run_dir = _absolute_lexical(run_dir)
    binding = verify_context_binding(
        workflow_root,
        run_dir,
        run_dir / "context.json",
        context_sha256,
        run_dir.name,
    )
    mcp_file = binding.run_dir / "mcp.json"
    mcp_bytes = _read_owned_file(binding.run_dir, "mcp.json", 0o600)
    if mcp_bytes != _canonical_json_bytes(_session_mcp_payload(binding.context)):
        raise SessionError("session MCP configuration does not match its context")
    return SessionPaths(
        binding.run_id,
        binding.run_dir,
        binding.context_file,
        binding.context_sha256,
        mcp_file,
    )


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--workflow-root", required=True, type=Path)
    create.add_argument("--launch-dir", required=True, type=Path)
    create.add_argument("--config", type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--workflow-root", required=True, type=Path)
    verify.add_argument("--run-dir", required=True, type=Path)
    verify.add_argument("--context-sha256", required=True)
    return parser


def main() -> int:
    arguments = _create_parser().parse_args()
    try:
        if arguments.command == "create":
            config_path = arguments.config or (
                arguments.workflow_root / "controller" / "project-context.json"
            )
            session = create_session(
                arguments.workflow_root, arguments.launch_dir, config_path
            )
            print(
                json.dumps(
                    {
                        "runId": session.run_id,
                        "runDir": str(session.run_dir),
                        "contextFile": str(session.context_file),
                        "contextSha256": session.context_sha256,
                        "mcpFile": str(session.mcp_file),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            verify_session(
                arguments.workflow_root,
                arguments.run_dir,
                arguments.context_sha256,
            )
    except (SessionError, ContextError, json.JSONDecodeError, OSError, ValueError):
        print("ERROR: owned session state rejected", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
