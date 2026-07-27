"""Discover Git worktrees eligible for bounded context population."""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from integrations.common.graph_manager import (
    GraphManagerError,
    sync_graphs,
)
from integrations.common.graph_hooks import (
    GraphHookError,
    graph_hook_status,
)


PRUNED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules",
    "graphify-out", "__pycache__", ".mypy_cache", ".pytest_cache",
})


class PopulationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositoryResult:
    repository: Path
    action: str
    hook_status: str


@dataclass(frozen=True)
class PopulationResult:
    palace: Path
    wing: str
    repositories: tuple[RepositoryResult, ...]


@dataclass(frozen=True)
class SkippedWorktree:
    worktree: Path
    primary: Path


@dataclass(frozen=True)
class RepositoryDiscovery:
    repositories: tuple[Path, ...]
    skipped_worktrees: tuple[SkippedWorktree, ...]


def _run(
    command: list[str], *, cwd: Path | None = None,
    env: dict[str, str] | None = None,
    heartbeat: Callable[[float], None] | None = None,
    heartbeat_interval: float = 10.0,
    line_observer: Callable[[str, str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    if heartbeat is not None and heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        raise PopulationError("required command could not be run") from error

    started = time.monotonic()
    next_heartbeat = started + heartbeat_interval
    try:
        if line_observer is not None:
            events: queue.Queue[
                tuple[str, str | BaseException | None]
            ] = queue.Queue()

            def read_stream(name: str, stream) -> None:
                try:
                    for line in stream:
                        events.put((name, line))
                except (OSError, UnicodeError) as error:
                    events.put(("error", error))
                finally:
                    events.put((name, None))

            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ):
                threading.Thread(
                    target=read_stream,
                    args=(name, stream),
                    daemon=True,
                ).start()

            captured = {"stdout": [], "stderr": []}
            closed = set()
            while len(closed) < 2:
                timeout = None
                if heartbeat is not None:
                    timeout = max(0.0, next_heartbeat - time.monotonic())
                try:
                    name, line = events.get(timeout=timeout)
                except queue.Empty:
                    heartbeat(time.monotonic() - started)
                    current = time.monotonic()
                    while next_heartbeat <= current:
                        next_heartbeat += heartbeat_interval
                    continue
                if line is None:
                    closed.add(name)
                    continue
                if name == "error":
                    raise line
                captured[name].append(line)
                line_observer(name, line.rstrip("\r\n"))
                if heartbeat is not None and time.monotonic() >= next_heartbeat:
                    heartbeat(time.monotonic() - started)
                    current = time.monotonic()
                    while next_heartbeat <= current:
                        next_heartbeat += heartbeat_interval
            process.wait()
            stdout = "".join(captured["stdout"])
            stderr = "".join(captured["stderr"])
            process.stdout.close()
            process.stderr.close()
        elif heartbeat is None:
            stdout, stderr = process.communicate()
        else:
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=max(0.0, next_heartbeat - time.monotonic())
                    )
                    break
                except subprocess.TimeoutExpired:
                    heartbeat(time.monotonic() - started)
                    current = time.monotonic()
                    while next_heartbeat <= current:
                        next_heartbeat += heartbeat_interval
    except (OSError, UnicodeError) as error:
        _terminate_process(process)
        raise PopulationError("required command could not be run") from error
    except BaseException:
        _terminate_process(process)
        raise

    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait()
        except ProcessLookupError:
            pass


def _resolve_git_path(value: str, repository: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = repository / path
    return path.resolve(strict=False)


def _discover_git_layout(root: Path) -> RepositoryDiscovery:
    """Return canonical repositories and redundant linked worktrees."""
    try:
        boundary = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PopulationError("context root must resolve to a directory") from error
    if not boundary.is_dir():
        raise PopulationError("context root must resolve to a directory")

    def walk_error(error: OSError) -> None:
        raise PopulationError("context root could not be traversed") from error

    candidates: dict[Path, tuple[Path, Path]] = {}
    for current, directories, files in os.walk(
        boundary, followlinks=False, onerror=walk_error
    ):
        current_path = Path(current)
        directories[:] = sorted(
            name for name in directories
            if name not in PRUNED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )
        if ".git" in files or (current_path / ".git").is_dir():
            completed = _run(
                [
                    "git", "-C", str(current_path), "rev-parse",
                    "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
                ]
            )
            if completed.returncode != 0:
                continue
            try:
                values = completed.stdout.splitlines()
                top = Path(values[0]).resolve(strict=True)
                top.relative_to(boundary)
            except (OSError, RuntimeError, ValueError):
                continue
            if top.is_dir():
                if len(values) >= 3:
                    git_dir = _resolve_git_path(values[1], top)
                    common_dir = _resolve_git_path(values[2], top)
                else:
                    git_dir = common_dir = top / ".git"
                candidates[top] = (git_dir, common_dir)

    groups: dict[Path, list[tuple[Path, Path]]] = {}
    for repository, (git_dir, common_dir) in candidates.items():
        groups.setdefault(common_dir, []).append((repository, git_dir))

    repositories: list[Path] = []
    skipped: list[SkippedWorktree] = []
    for common_dir, checkouts in groups.items():
        primary = next(
            (
                repository for repository, git_dir in checkouts
                if git_dir == common_dir
            ),
            None,
        )
        if primary is None:
            ordered = sorted(checkouts, key=lambda item: str(item[0]))
            primary = ordered[0][0]
            repositories.append(primary)
            skipped.extend(
                SkippedWorktree(repository, primary)
                for repository, _ in ordered[1:]
            )
            continue
        repositories.append(primary)
        skipped.extend(
            SkippedWorktree(repository, primary)
            for repository, git_dir in checkouts
            if git_dir != common_dir
        )

    return RepositoryDiscovery(
        tuple(sorted(repositories, key=lambda path: str(path))),
        tuple(sorted(skipped, key=lambda item: str(item.worktree))),
    )


def discover_git_worktrees(root: Path) -> tuple[Path, ...]:
    """Return sorted logical Git repository roots contained by ``root``."""
    return _discover_git_layout(root).repositories


def _mempalace_sources(
    root: Path, repositories: tuple[Path, ...]
) -> tuple[Path, ...]:
    """Return outermost canonical roots so nested repositories are mined once."""
    if not repositories:
        return (Path(root),)
    return tuple(
        repository
        for repository in repositories
        if not any(
            parent != repository and repository.is_relative_to(parent)
            for parent in repositories
        )
    )


def _reject_nested_skipped_worktrees(
    sources: tuple[Path, ...], skipped: tuple[SkippedWorktree, ...]
) -> None:
    for item in skipped:
        for source in sources:
            if item.worktree != source and item.worktree.is_relative_to(source):
                raise PopulationError(
                    "linked worktree is nested inside a MemPalace source: "
                    f"{_safe_field(item.worktree)}; move it outside "
                    f"{_safe_field(source)} before population"
                )


def _resolve_executable(name: str, label: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise PopulationError(f"{label} executable is unavailable")
    try:
        resolved = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PopulationError(f"{label} executable is unavailable") from error
    if not resolved.is_absolute() or not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PopulationError(f"{label} executable is unavailable")
    return str(resolved)


def _safe_field(value: object) -> str:
    common_escapes = {
        "\b": r"\b",
        "\t": r"\t",
        "\n": r"\n",
        "\f": r"\f",
        "\r": r"\r",
    }
    escaped = []
    for character in str(value):
        if character in common_escapes:
            escaped.append(common_escapes[character])
        elif not character.isprintable():
            codepoint = ord(character)
            escaped.append(
                f"\\u{codepoint:04x}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08x}"
            )
        elif character in ('"', "\\"):
            escaped.append("\\" + character)
        else:
            escaped.append(character)
    return "".join(escaped)


def _bounded_diagnostics(completed: subprocess.CompletedProcess[str]) -> str:
    sections = []
    for label, output in (("stdout", completed.stdout), ("stderr", completed.stderr)):
        lines = [line for line in output.splitlines() if line.strip()][-20:]
        if lines:
            sections.append(f"{label}:\n" + "\n".join(lines))
    return "\n".join(sections)


def _require_success(
    command: list[str], *, label: str, cwd: Path | None = None,
    repository: Path | None = None,
    env: dict[str, str] | None = None,
    heartbeat: Callable[[float], None] | None = None,
    line_observer: Callable[[str, str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = _run(
            command,
            cwd=cwd,
            env=env,
            heartbeat=heartbeat,
            line_observer=line_observer,
        )
    except PopulationError as error:
        subject = (
            f" for repository {_safe_field(repository)}" if repository else ""
        )
        raise PopulationError(f"{label} failed{subject}: {error}") from error
    if completed.returncode == 0:
        return completed

    _raise_for_failure(completed, label=label, repository=repository)


def _raise_for_failure(
    completed: subprocess.CompletedProcess[str], *, label: str,
    repository: Path | None = None,
) -> None:
    subject = f" for repository {_safe_field(repository)}" if repository else ""
    prefix = f"{label} failed{subject} with exit code {completed.returncode}"
    diagnostics = _bounded_diagnostics(completed)
    if diagnostics:
        limit = max(0, 4_000 - len(prefix) - 2)
        diagnostics = diagnostics[-limit:] if limit else ""
    if diagnostics:
        raise PopulationError(f"{prefix}:\n{diagnostics}")
    raise PopulationError(prefix)


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _heartbeat(
    progress: Callable[[str], None] | None, label: str
) -> Callable[[float], None] | None:
    if progress is None:
        return None
    return lambda elapsed: progress(
        f"{label} — {_format_elapsed(elapsed)} elapsed"
    )


_MEMPALACE_FILE_PROGRESS = re.compile(
    r"^\s*\+\s+\[\s*(\d+)/(\d+)\]\s+(.+?)\s+\+(\d+)\s*$"
)


class _MempalaceProgress:
    def __init__(
        self,
        prefix: str,
        progress: Callable[[str], None],
        started: float,
    ) -> None:
        self.prefix = prefix
        self.progress = progress
        self.started = started
        self.next_percent = 5
        self.reported = False

    def __call__(self, stream: str, line: str) -> None:
        if stream != "stdout":
            return
        match = _MEMPALACE_FILE_PROGRESS.match(line)
        if match is None:
            return
        current_text, total_text, filename, drawers = match.groups()
        current = int(current_text)
        total = int(total_text)
        if total <= 0 or current <= 0 or current > total:
            return
        percent = current * 100 // total
        is_first = not self.reported
        if not is_first and current != total and percent < self.next_percent:
            return
        self.reported = True
        while self.next_percent <= percent:
            self.next_percent += 5
        self.progress(
            f"{self.prefix} progress {current}/{total} ({percent}%) — "
            f"{_safe_field(filename.strip())} — +{drawers} drawers — "
            f"{_format_elapsed(time.monotonic() - self.started)} elapsed"
        )


def _mempalace_mine_environment() -> dict[str, str]:
    shim_directory = (
        Path(__file__).resolve().parent.parent / "mempalace_sitecustomize"
    )
    shim = shim_directory / "sitecustomize.py"
    if not shim.is_file() or shim.is_symlink():
        raise PopulationError("MemPalace generated-artifact guard is unavailable")
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(shim_directory)
        if not existing_pythonpath
        else str(shim_directory) + os.pathsep + existing_pythonpath
    )
    environment["CLAUDEX_MEMPALACE_EXCLUDE_GENERATED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _graph_data_root() -> Path:
    raw = os.environ.get("ORICHUM_DATA_HOME")
    if raw is None:
        xdg = os.environ.get("XDG_DATA_HOME")
        raw = (
            str(Path(xdg) / "orichum")
            if xdg
            else str(Path.home() / ".local/share/orichum")
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise PopulationError("ORICHUM_DATA_HOME must be an absolute path")
    return path.resolve(strict=False)


def populate_context(
    root: Path,
    palace: Path,
    wing: str,
    *,
    progress: Callable[[str], None] | None = None,
) -> PopulationResult:
    _emit(progress, f"[discover] scanning {_safe_field(root)}")
    discovery = _discover_git_layout(root)
    repositories = discovery.repositories
    repository_label = "repository" if len(repositories) == 1 else "repositories"
    _emit(
        progress,
        f"[discover] found {len(repositories)} {repository_label}",
    )
    for skipped in discovery.skipped_worktrees:
        _emit(
            progress,
            "[discover] skipped linked worktree "
            f"{_safe_field(skipped.worktree.name)} — same repository as "
            f"{_safe_field(skipped.primary.name)}",
        )
    for index, repository in enumerate(repositories, start=1):
        _emit(
            progress,
            f"[discover] {index}/{len(repositories)} "
            f"{_safe_field(repository.name)} "
            "— Graphify sync pending",
        )

    memory_sources = _mempalace_sources(root, repositories)
    _reject_nested_skipped_worktrees(
        memory_sources, discovery.skipped_worktrees
    )
    mempalace = _resolve_executable("mempalace", "MemPalace")
    for index, source in enumerate(memory_sources, start=1):
        prefix = (
            "[mempalace]" if len(memory_sources) == 1
            else f"[mempalace {index}/{len(memory_sources)}]"
        )
        _emit(
            progress,
            f"{prefix} mining {_safe_field(source)} into "
            f"{_safe_field(palace)}; wing {_safe_field(wing)}",
        )
        mine_started = time.monotonic()
        _require_success(
            [
                mempalace, "--palace", str(palace), "mine", str(source),
                "--mode", "projects", "--wing", wing,
            ],
            label=f"MemPalace mine for {_safe_field(source)}",
            env=_mempalace_mine_environment(),
            heartbeat=_heartbeat(progress, f"{prefix} mining"),
            line_observer=(
                _MempalaceProgress(prefix, progress, mine_started)
                if progress is not None
                else None
            ),
        )
        _emit(
            progress,
            f"{prefix} mine complete — "
            f"{_format_elapsed(time.monotonic() - mine_started)}",
        )
    _emit(progress, "[mempalace] verifying store")
    _require_success(
        [mempalace, "--palace", str(palace), "status"],
        label="MemPalace status",
        heartbeat=_heartbeat(progress, "[mempalace] verification"),
    )
    _emit(progress, "[mempalace] store verified")
    if not repositories:
        return PopulationResult(Path(palace), wing, ())
    graphify = _resolve_executable("graphify", "Graphify")
    try:
        synchronized = sync_graphs(
            root,
            _graph_data_root(),
            graphify=graphify,
            progress=(
                (lambda message: progress(_safe_field(message)))
                if progress is not None
                else None
            ),
        )
    except GraphManagerError as error:
        raise PopulationError(str(error)) from error
    results = tuple(
        RepositoryResult(
            result.repository,
            result.action.replace("-", " "),
            (
                "not applicable"
                if result.action == "not-applicable"
                else _reported_hook_status(result.repository)
            ),
        )
        for result in synchronized
    )
    return PopulationResult(Path(palace), wing, results)


def _reported_hook_status(repository: Path) -> str:
    try:
        return (
            "installed"
            if graph_hook_status(repository) == "installed"
            else "not managed"
        )
    except GraphHookError:
        return "not managed"


def render_population_result(result: PopulationResult) -> str:
    summary = (
        f"MemPalace: populated wing {_safe_field(result.wing)} in "
        f"{_safe_field(result.palace)}"
    )
    if not result.repositories:
        return f"{summary}\n\nGraphify: no applicable Git repositories found."

    headers = ("REPOSITORY", "GRAPHIFY", "GIT HOOK")
    rows = tuple(
        (
            _safe_field(item.repository),
            _safe_field(item.action),
            _safe_field(item.hook_status),
        )
        for item in result.repositories
    )
    widths = tuple(
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    )

    def border() -> str:
        return "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def row(values: tuple[str, str, str]) -> str:
        return "| " + " | ".join(
            value.ljust(width) for value, width in zip(values, widths)
        ) + " |"

    table = "\n".join(
        (border(), row(headers), border(), *(row(values) for values in rows), border())
    )
    return f"{summary}\n\n{table}"
