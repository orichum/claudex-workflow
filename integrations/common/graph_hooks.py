#!/usr/bin/env python3
"""Safely manage Orichum-owned repository graph hooks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile


class GraphHookError(RuntimeError):
    """A repository graph hook cannot be managed safely."""


_ORICHUM_START = "# orichum-graph-hook-start"
_ORICHUM_END = "# orichum-graph-hook-end"
_UPSTREAM_MARKERS = {
    "post-commit": ("# graphify-hook-start", "# graphify-hook-end"),
    "post-checkout": (
        "# graphify-checkout-hook-start",
        "# graphify-checkout-hook-end",
    ),
}
_HOOK_NAMES = tuple(_UPSTREAM_MARKERS)
_MAX_HOOK_BYTES = 1024 * 1024
_MAX_LOG_BYTES = 1024 * 1024
_GRAPHIFY_MERGE_NAME = "graphify graph.json union merge"
_GRAPHIFY_DRIVER = re.compile(
    r"(?:graphify|[A-Za-z0-9/_.@:\\-]+ -m graphify) "
    r"merge-driver %O %A %B"
)
_GRAPHIFY_ATTRIBUTE = "graphify-out/graph.json merge=graphify"
_DETACHED_LAUNCHER = (
    "import json, os, subprocess, sys\n"
    "command = json.loads(sys.argv[1])\n"
    "subprocess.Popen(command, cwd=os.getcwd(), stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, "
    "start_new_session=True)\n"
)
_LOG_WORKER = r"""
import fcntl
import json
import os
import stat
import subprocess
import sys

command = json.loads(sys.argv[1])
log = sys.argv[2]
lock = sys.argv[3]
maximum = int(sys.argv[4])
previous = log + ".previous"
nofollow = getattr(os, "O_NOFOLLOW", 0)
cloexec = getattr(os, "O_CLOEXEC", 0)

lock_descriptor = os.open(
    lock, os.O_RDWR | os.O_CREAT | nofollow | cloexec, 0o600
)
lock_status = os.fstat(lock_descriptor)
if (
    not stat.S_ISREG(lock_status.st_mode)
    or lock_status.st_uid != os.getuid()
    or stat.S_IMODE(lock_status.st_mode) != 0o600
):
    raise SystemExit(2)
fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
try:
    if os.path.lexists(log):
        log_status = os.lstat(log)
        if (
            stat.S_ISLNK(log_status.st_mode)
            or not stat.S_ISREG(log_status.st_mode)
            or log_status.st_uid != os.getuid()
            or stat.S_IMODE(log_status.st_mode) != 0o600
        ):
            raise SystemExit(2)
        if log_status.st_size > maximum:
            if os.path.lexists(previous):
                previous_status = os.lstat(previous)
                if (
                    stat.S_ISLNK(previous_status.st_mode)
                    or not stat.S_ISREG(previous_status.st_mode)
                    or previous_status.st_uid != os.getuid()
                    or stat.S_IMODE(previous_status.st_mode) != 0o600
                ):
                    raise SystemExit(2)
            os.replace(log, previous)
    output_descriptor = os.open(
        log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow | cloexec, 0o600
    )
    output_status = os.fstat(output_descriptor)
    if (
        not stat.S_ISREG(output_status.st_mode)
        or output_status.st_uid != os.getuid()
        or stat.S_IMODE(output_status.st_mode) != 0o600
    ):
        raise SystemExit(2)
    try:
        subprocess.run(
            command,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=output_descriptor,
            stderr=subprocess.STDOUT,
            close_fds=True,
            check=False,
        )
    finally:
        os.close(output_descriptor)
finally:
    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    os.close(lock_descriptor)
"""


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GraphHookError("Repository Git command is unavailable") from error


def _git_text(repository: Path, *arguments: str) -> str:
    completed = _git(repository, *arguments)
    if completed.returncode != 0:
        raise GraphHookError("Repository Git command failed")
    value = completed.stdout.strip()
    if not value or any(character in value for character in "\r\n\0"):
        raise GraphHookError("Repository Git path is unsafe")
    return value


def _safe_path(path: Path, label: str) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise GraphHookError(f"{label} is unsafe")
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            return absolute
        except OSError as error:
            raise GraphHookError(f"{label} is unavailable") from error
        if stat.S_ISLNK(observed.st_mode):
            raise GraphHookError(f"{label} is unsafe")
    return absolute


def _safe_directory(
    path: Path,
    label: str,
    *,
    private: bool = False,
    create: bool = False,
) -> Path:
    path = _safe_path(path, label)
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise GraphHookError(f"{label} cannot be created") from error
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise GraphHookError(f"{label} is unavailable") from error
    mode = stat.S_IMODE(observed.st_mode)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or mode & 0o022
        or (private and mode != 0o700)
    ):
        raise GraphHookError(f"{label} is unsafe")
    return path


def resolve_hook_repository(path: Path) -> Path:
    """Resolve the active worktree root with a bounded Git operation."""
    candidate = _safe_directory(Path(path), "Repository")
    root = Path(_git_text(candidate, "rev-parse", "--show-toplevel"))
    root = _safe_directory(root, "Repository")
    inside = _git(candidate, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise GraphHookError("Repository is not a working tree")
    return root


def _hooks_directory(repository: Path) -> Path:
    raw = _git_text(repository, "rev-parse", "--git-path", "hooks")
    path = Path(raw)
    if not path.is_absolute():
        path = repository / path
    return _safe_directory(path, "Git hooks directory")


def _read_hook(path: Path) -> tuple[str | None, int]:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None, 0o755
    except OSError as error:
        raise GraphHookError("Git hook is unavailable") from error
    mode = stat.S_IMODE(observed.st_mode)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or mode & 0o022
        or observed.st_size > _MAX_HOOK_BYTES
    ):
        raise GraphHookError("Git hook is unsafe")
    try:
        return path.read_text(encoding="utf-8"), mode
    except (OSError, UnicodeError) as error:
        raise GraphHookError("Git hook is unavailable") from error


def _remove_marked_section(content: str, start: str, end: str) -> str:
    lines = content.splitlines(keepends=True)
    kept: list[str] = []
    inside = False
    for line in lines:
        marker = line.rstrip("\r\n")
        if marker == start:
            if inside:
                raise GraphHookError("Git hook marker section is malformed")
            inside = True
            if kept and not kept[-1].strip():
                kept.pop()
            continue
        if marker == end:
            if not inside:
                raise GraphHookError("Git hook marker section is malformed")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise GraphHookError("Git hook marker section is malformed")
    return "".join(kept)


def _hook_block(launcher: Path) -> str:
    command = f'{shlex.quote(str(launcher))} graph hook-update "$PWD"'
    return f"{_ORICHUM_START}\n{command}\n{_ORICHUM_END}\n"


def _valid_launcher(launcher: Path) -> bool:
    if not launcher.is_absolute():
        return False
    try:
        observed = os.stat(launcher)
    except OSError:
        return False
    return (
        stat.S_ISREG(observed.st_mode)
        and observed.st_uid == os.getuid()
        and bool(observed.st_mode & stat.S_IXUSR)
    )


def _managed_launcher(content: str) -> Path | None:
    lines = content.splitlines()
    starts = [index for index, line in enumerate(lines) if line == _ORICHUM_START]
    ends = [index for index, line in enumerate(lines) if line == _ORICHUM_END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] != starts[0] + 2:
        raise GraphHookError("Orichum graph hook block is malformed")
    try:
        command = shlex.split(lines[starts[0] + 1])
    except ValueError as error:
        raise GraphHookError("Orichum graph hook command is malformed") from error
    if (
        len(command) != 4
        or command[1:] != ["graph", "hook-update", "$PWD"]
    ):
        raise GraphHookError("Orichum graph hook command is foreign")
    launcher = Path(command[0])
    if not _valid_launcher(launcher):
        raise GraphHookError("Orichum graph hook launcher is unsafe")
    canonical = _hook_block(launcher).splitlines()[1]
    if lines[starts[0] + 1] != canonical:
        raise GraphHookError("Orichum graph hook command is not canonical")
    return launcher


def _updated_hook(content: str | None, name: str, launcher: Path) -> str:
    if content is None:
        content = "#!/bin/sh\n"
    content = _remove_marked_section(content, _ORICHUM_START, _ORICHUM_END)
    upstream_start, upstream_end = _UPSTREAM_MARKERS[name]
    content = _remove_marked_section(content, upstream_start, upstream_end)
    separator = "\n" if content.endswith("\n") else "\n\n"
    return f"{content}{separator}{_hook_block(launcher)}"


def _write_hook(path: Path, content: str, mode: int) -> None:
    descriptor = None
    temporary_path = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.orichum-",
            dir=path.parent,
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise GraphHookError("Git hook cannot be written") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def install_graph_hooks(repository: Path, orichum_launcher: Path) -> None:
    """Install marked post-commit and post-checkout hook sections."""
    repository = resolve_hook_repository(repository)
    launcher = Path(orichum_launcher)
    if not launcher.is_absolute():
        raise GraphHookError("Orichum launcher must be absolute")
    try:
        launcher_status = os.stat(launcher)
    except OSError as error:
        raise GraphHookError("Orichum launcher is unavailable") from error
    if (
        not stat.S_ISREG(launcher_status.st_mode)
        or launcher_status.st_uid != os.getuid()
        or not launcher_status.st_mode & stat.S_IXUSR
    ):
        raise GraphHookError("Orichum launcher is unsafe")
    hooks = _hooks_directory(repository)
    updates = []
    for name in _HOOK_NAMES:
        path = hooks / name
        content, mode = _read_hook(path)
        updates.append(
            (path, _updated_hook(content, name, launcher), mode | stat.S_IXUSR)
        )
    for path, content, mode in updates:
        _write_hook(path, content, mode)
    remove_upstream_graphify_hooks(repository)


def graph_hook_status(repository: Path) -> str:
    """Return installed, missing, or unsafe for the Orichum hook contract."""
    try:
        repository = resolve_hook_repository(repository)
        hooks = _hooks_directory(repository)
        missing = False
        launchers: set[Path] = set()
        for name in _HOOK_NAMES:
            content, mode = _read_hook(hooks / name)
            if content is None:
                missing = True
                continue
            launcher = _managed_launcher(content)
            if not mode & stat.S_IXUSR or launcher is None:
                missing = True
            else:
                launchers.add(launcher)
        if len(launchers) > 1:
            raise GraphHookError("Orichum graph hook launchers do not match")
        return "missing" if missing else "installed"
    except GraphHookError:
        return "unsafe"


def _config_value(repository: Path, key: str) -> str | None:
    completed = _git(repository, "config", "--local", "--get", key)
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        raise GraphHookError("Graphify merge configuration cannot be read")
    return completed.stdout.strip()


def _safe_attributes(repository: Path) -> tuple[Path, bytes, int] | None:
    path = repository / ".gitattributes"
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise GraphHookError("Git attributes are unavailable") from error
    mode = stat.S_IMODE(observed.st_mode)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or mode & 0o022
        or observed.st_size > _MAX_HOOK_BYTES
    ):
        raise GraphHookError("Git attributes are unsafe")
    try:
        return path, path.read_bytes(), mode
    except OSError as error:
        raise GraphHookError("Git attributes are unavailable") from error


def _write_bytes(path: Path, content: bytes, mode: int) -> None:
    descriptor = None
    temporary_path = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.orichum-",
            dir=path.parent,
        )
        temporary_path = Path(raw_path)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise GraphHookError("Git attributes cannot be written") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def remove_upstream_graphify_hooks(repository: Path) -> None:
    """Remove only recognized upstream Graphify hook and merge registrations."""
    repository = resolve_hook_repository(repository)
    if graph_hook_status(repository) != "installed":
        raise GraphHookError("Repository is not managed by Orichum graph hooks")
    hooks = _hooks_directory(repository)
    for name in _HOOK_NAMES:
        path = hooks / name
        content, mode = _read_hook(path)
        if content is None:
            continue
        start, end = _UPSTREAM_MARKERS[name]
        updated = _remove_marked_section(content, start, end)
        if updated != content:
            _write_hook(path, updated, mode)

    name = _config_value(repository, "merge.graphify.name")
    driver = _config_value(repository, "merge.graphify.driver")
    attributes = _safe_attributes(repository)
    if (
        name != _GRAPHIFY_MERGE_NAME
        or driver is None
        or _GRAPHIFY_DRIVER.fullmatch(driver) is None
        or attributes is None
    ):
        return
    attributes_path, content, mode = attributes
    attribute = _GRAPHIFY_ATTRIBUTE.encode("ascii")
    lines = content.splitlines(keepends=True)
    if not any(line.rstrip(b"\r\n") == attribute for line in lines):
        return
    kept = b"".join(
        line for line in lines if line.rstrip(b"\r\n") != attribute
    )
    if kept:
        _write_bytes(attributes_path, kept, mode)
    else:
        try:
            attributes_path.unlink()
        except OSError as error:
            raise GraphHookError("Graphify attributes cannot be removed") from error
    for key in ("merge.graphify.name", "merge.graphify.driver"):
        completed = _git(repository, "config", "--local", "--unset-all", key)
        if completed.returncode not in (0, 5):
            raise GraphHookError("Graphify merge configuration cannot be removed")


def _graph_log_path(repository: Path, data_root: Path) -> Path:
    repository = resolve_hook_repository(repository)
    common_raw = _git_text(repository, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = repository / common
    common = _safe_directory(common, "Git common directory")
    repository_hash = hashlib.sha256(
        str(common).encode("utf-8")
    ).hexdigest()
    return Path(data_root) / "graphs" / "logs" / f"{repository_hash}.log"


def _launch_detached_update(
    repository: Path,
    data_root: Path,
    command: list[str],
) -> Path:
    """Launch a graph refresh detached from Git and return its bounded log."""
    repository = resolve_hook_repository(repository)
    data_root = _safe_directory(data_root, "Graph data", private=True)
    graphs = _safe_directory(
        data_root / "graphs", "Graph root", private=True, create=True
    )
    logs = _safe_directory(
        graphs / "logs", "Graph log directory", private=True, create=True
    )
    log = _graph_log_path(repository, data_root)
    previous = log.with_name(f"{log.name}.previous")
    lock = log.with_name(f"{log.name}.lock")
    for path, label in (
        (log, "Graph update log"),
        (previous, "Previous graph update log"),
    ):
        if not os.path.lexists(path):
            continue
        observed = os.lstat(path)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise GraphHookError(f"{label} is unsafe")
    lock_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(lock, lock_flags, 0o600)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise GraphHookError("Graph update log lock is unsafe")
        os.close(descriptor)
        descriptor = None
        worker = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _LOG_WORKER,
            json.dumps(command),
            str(log),
            str(lock),
            str(_MAX_LOG_BYTES),
        ]
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _DETACHED_LAUNCHER,
                json.dumps(worker),
            ],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        try:
            return_code = launcher.wait(timeout=2)
        except subprocess.TimeoutExpired as error:
            launcher.kill()
            launcher.wait()
            raise GraphHookError("Graph update launcher timed out") from error
        if return_code != 0:
            raise GraphHookError("Graph update cannot be launched")
    except OSError as error:
        raise GraphHookError("Graph update cannot be launched") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return log
