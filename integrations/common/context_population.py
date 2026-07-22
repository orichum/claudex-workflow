"""Discover Git worktrees eligible for bounded context population."""

import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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
    heartbeat: Callable[[float], None] | None = None,
    heartbeat_interval: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    if heartbeat is not None and heartbeat_interval <= 0:
        raise ValueError("heartbeat_interval must be positive")
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
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
        if heartbeat is None:
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
    heartbeat: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = _run(command, cwd=cwd, heartbeat=heartbeat)
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


def _not_applicable(
    completed: subprocess.CompletedProcess[str], graph: Path
) -> bool:
    return (
        completed.returncode != 0
        and not os.path.lexists(graph)
        and "found 0 code" in completed.stdout.lower()
        and "graph is empty" in completed.stderr.lower()
    )


def _resolve_contained_graph(repository: Path, graph: Path) -> Path:
    try:
        resolved_repository = repository.resolve(strict=True)
        resolved_graph = graph.resolve(strict=True)
        resolved_graph.relative_to(resolved_repository)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        raise PopulationError(
            "Graphify graph validation failed for repository "
            f"{_safe_field(repository)}"
        ) from error
    return resolved_graph


def _require_regular_graph(repository: Path, graph: Path) -> Path:
    resolved_graph = _resolve_contained_graph(repository, graph)
    try:
        graph_status = resolved_graph.stat()
    except OSError as error:
        raise PopulationError(
            "Graphify graph validation failed for repository "
            f"{_safe_field(repository)}"
        ) from error
    if not stat.S_ISREG(graph_status.st_mode):
        raise PopulationError(
            "Graphify graph validation failed for repository "
            f"{_safe_field(repository)}"
        )
    return resolved_graph


def _validate_graph(repository: Path, graph: Path) -> None:
    resolved_graph = _require_regular_graph(repository, graph)
    descriptor = None
    try:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(resolved_graph, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PopulationError(
                "Graphify graph validation failed for repository "
                f"{_safe_field(repository)}"
            )
        source = os.fdopen(descriptor, encoding="utf-8")
        descriptor = None
        with source:
            parsed = json.load(source)
    except PopulationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PopulationError(
            "Graphify graph validation failed for repository "
            f"{_safe_field(repository)}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise PopulationError(
            f"Graphify graph validation failed for repository {_safe_field(repository)}"
        )


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


def _populate_repository(
    repository: Path,
    graphify: str,
    *,
    ordinal: int = 1,
    total: int = 1,
    progress: Callable[[str], None] | None = None,
) -> RepositoryResult:
    graph = repository / "graphify-out" / "graph.json"
    prefix = f"[graphify {ordinal}/{total}]"
    repository_name = _safe_field(repository.name)
    if graph.exists():
        action = "updated"
        operation = "update"
        _emit(
            progress,
            f"{prefix} validating existing graph for {repository_name}",
        )
        _require_regular_graph(repository, graph)
        _emit(progress, f"{prefix} existing graph validated")
        _emit(progress, f"{prefix} updating {repository_name}")
        operation_started = time.monotonic()
        _require_success(
            [graphify, "update", str(repository)],
            label="Graphify update",
            repository=repository,
            heartbeat=_heartbeat(progress, f"{prefix} update"),
        )
    else:
        action = "created"
        operation = "create"
        _emit(progress, f"{prefix} creating {repository_name}")
        operation_started = time.monotonic()
        try:
            completed = _run(
                [graphify, "extract", str(repository), "--code-only"],
                heartbeat=_heartbeat(progress, f"{prefix} create"),
            )
        except PopulationError as error:
            raise PopulationError(
                "Graphify extract failed for repository "
                f"{_safe_field(repository)}: {error}"
            ) from error
        if _not_applicable(completed, graph):
            _emit(
                progress,
                f"{prefix} no supported code; skipped — "
                f"{_format_elapsed(time.monotonic() - operation_started)}",
            )
            return RepositoryResult(repository, "not applicable", "not applicable")
        if completed.returncode != 0:
            _raise_for_failure(
                completed, label="Graphify extract", repository=repository
            )
    _emit(
        progress,
        f"{prefix} {operation} complete — "
        f"{_format_elapsed(time.monotonic() - operation_started)}",
    )
    _emit(progress, f"{prefix} validating graph")
    _validate_graph(repository, graph)
    _emit(progress, f"{prefix} graph validated")
    _emit(progress, f"{prefix} installing Git hooks")
    _require_success(
        [graphify, "hook", "install"],
        label="Graphify hook install",
        cwd=repository,
        repository=repository,
        heartbeat=_heartbeat(progress, f"{prefix} installing Git hooks"),
    )
    _emit(progress, f"{prefix} verifying Git hooks")
    status = _require_success(
        [graphify, "hook", "status"],
        label="Graphify hook status",
        cwd=repository,
        repository=repository,
        heartbeat=_heartbeat(progress, f"{prefix} verifying Git hooks"),
    )
    if "not installed" in f"{status.stdout}\n{status.stderr}".lower():
        raise PopulationError(
            "Graphify hook status failed for repository "
            f"{_safe_field(repository)}: not installed"
        )
    _emit(progress, f"{prefix} hooks installed and verified")
    return RepositoryResult(repository, action, "installed")


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
        graph_exists = (
            repository / "graphify-out" / "graph.json"
        ).exists()
        planned_action = (
            "update" if graph_exists else "applicability check pending"
        )
        _emit(
            progress,
            f"[discover] {index}/{len(repositories)} "
            f"{_safe_field(repository.name)} "
            f"— Graphify {planned_action}",
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
            heartbeat=_heartbeat(progress, f"{prefix} mining"),
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
    results = []
    for index, repository in enumerate(repositories, start=1):
        result = _populate_repository(
            repository,
            graphify,
            ordinal=index,
            total=len(repositories),
            progress=progress,
        )
        results.append(result)
    return PopulationResult(Path(palace), wing, tuple(results))


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
