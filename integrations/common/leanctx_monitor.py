#!/usr/bin/env python3
"""Project-aware monitoring for Orichum-managed LeanCTX sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
from typing import Mapping, Sequence

from .session_config import (
    SessionError,
    require_owned_component,
    require_private_direct_child,
    verify_context_binding,
)


_RUN_ID = re.compile(r"^run\.[A-Za-z0-9_-]+$")
_MAX_MANIFEST_BYTES = 64 * 1024


class LeanctxMonitorError(RuntimeError):
    """LeanCTX monitoring state cannot be resolved safely."""


@dataclass(frozen=True)
class LeanctxRun:
    run_id: str
    run_dir: Path
    project_root: Path
    created_at: str
    has_activity: bool


@dataclass(frozen=True)
class LeanctxStats:
    total_commands: int
    input_tokens: int
    output_tokens: int
    saved_tokens: int
    savings_percent: float


def _private_json(path: Path) -> tuple[dict[str, object], os.stat_result]:
    observed = os.lstat(path)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise LeanctxMonitorError("session manifest is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (
            observed.st_dev,
            observed.st_ino,
        ):
            raise LeanctxMonitorError("session manifest changed")
        payload = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise LeanctxMonitorError("session manifest is too large")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeanctxMonitorError("session manifest is invalid") from error
    if not isinstance(document, dict):
        raise LeanctxMonitorError("session manifest is invalid")
    return document, observed


def _manifest_digests(document: dict[str, object]) -> tuple[str, str]:
    if set(document) != {
        "schemaVersion",
        "contextSha256",
        "effectiveModelsSha256",
    } or document.get("schemaVersion") != 1:
        raise LeanctxMonitorError("session manifest is invalid")
    context = document.get("contextSha256")
    effective = document.get("effectiveModelsSha256")
    for digest in (context, effective):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise LeanctxMonitorError("session manifest is invalid")
    return context, effective


def _activity_exists(directory: Path) -> bool:
    path = directory / "events.jsonl"
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
    ):
        raise LeanctxMonitorError("LeanCTX activity file is unsafe")
    return observed.st_size > 0


def _verified_run(
    workflow_root: Path,
    data_root: Path,
    sessions: Path,
    candidate: Path,
) -> LeanctxRun | None:
    run_dir = require_private_direct_child(sessions, candidate)
    try:
        os.lstat(run_dir / "leanctx")
    except FileNotFoundError:
        return None
    leanctx = require_owned_component(
        run_dir,
        "leanctx",
        private=True,
    )
    manifest_path = run_dir / ".complete"
    try:
        os.lstat(manifest_path)
    except FileNotFoundError:
        return None
    document, manifest_stat = _private_json(manifest_path)
    context_digest, _ = _manifest_digests(document)
    binding = verify_context_binding(
        workflow_root,
        run_dir,
        run_dir / "context.json",
        context_digest,
        run_dir.name,
        data_root,
    )
    context = binding.context
    route = context.get("route") if isinstance(context, dict) else None
    project = route.get("contextRootReal") if isinstance(route, dict) else None
    if not isinstance(project, str) or not project:
        raise LeanctxMonitorError("session project context is invalid")
    project_root = Path(project)
    if not project_root.is_absolute():
        raise LeanctxMonitorError("session project context is invalid")
    created_at = datetime.fromtimestamp(
        manifest_stat.st_mtime,
        tz=timezone.utc,
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return LeanctxRun(
        run_id=binding.run_id,
        run_dir=binding.run_dir,
        project_root=project_root.resolve(strict=False),
        created_at=created_at,
        has_activity=_activity_exists(leanctx),
    )


def discover_runs(
    workflow_root: Path,
    data_root: Path,
) -> tuple[LeanctxRun, ...]:
    """Discover complete, verified LeanCTX runs newest first."""
    state_path = data_root / "state"
    sessions_path = state_path / "sessions"
    if not os.path.lexists(state_path) or not os.path.lexists(sessions_path):
        return ()
    try:
        state = require_owned_component(data_root, "state", private=True)
        sessions = require_owned_component(state, "sessions", private=True)
        runs = []
        for candidate in sorted(sessions.iterdir(), key=lambda path: path.name):
            if not candidate.name.startswith("run."):
                continue
            run = _verified_run(
                workflow_root,
                data_root,
                sessions,
                candidate,
            )
            if run is not None:
                runs.append(run)
    except (LeanctxMonitorError, SessionError, OSError) as error:
        raise LeanctxMonitorError(
            "completed LeanCTX run is invalid"
        ) from error
    return tuple(
        sorted(
            runs,
            key=lambda run: (run.created_at, run.run_id),
            reverse=True,
        )
    )


def select_run(
    runs: Sequence[LeanctxRun],
    project_root: Path | None,
    run_id: str | None,
    current_run_id: str | None = None,
) -> LeanctxRun:
    """Select an explicit, current, or newest active run for a project."""
    if run_id is not None:
        if not _RUN_ID.fullmatch(run_id):
            raise LeanctxMonitorError("run identifier is invalid")
        for run in runs:
            if run.run_id == run_id:
                return run
        raise LeanctxMonitorError(f"LeanCTX run {run_id} was not found")
    if project_root is None:
        raise LeanctxMonitorError(
            "current directory is not mapped to an Orichum project"
        )
    expected = Path(project_root).resolve(strict=False)
    matches = tuple(run for run in runs if run.project_root == expected)
    if matches:
        if current_run_id is not None:
            for run in matches:
                if run.run_id == current_run_id:
                    return run
        active = tuple(run for run in matches if run.has_activity)
        candidates = active or matches
        return max(candidates, key=lambda run: (run.created_at, run.run_id))
    raise LeanctxMonitorError(
        "current project has no LeanCTX activity; "
        "run 'orichum leanctx list' to inspect available runs"
    )


def leanctx_environment(
    run: LeanctxRun,
    base: Mapping[str, str] | None = None,
    config_dir: Path | None = None,
) -> dict[str, str]:
    """Build the fixed LeanCTX store environment for one run."""
    environment = dict(os.environ if base is None else base)
    directory = run.run_dir / "leanctx"
    environment.update(
        {
            "LEAN_CTX_CACHE_DIR": str(directory),
            "LEAN_CTX_CONFIG_DIR": str(config_dir or directory),
            "LEAN_CTX_DATA_DIR": str(directory),
            "LEAN_CTX_PROJECT_ROOT": str(run.project_root),
            "LEAN_CTX_STATE_DIR": str(directory),
        }
    )
    return environment


def _native_failure(stderr: str) -> str:
    detail = stderr.strip()[:1000]
    return detail or "LeanCTX command failed"


def read_stats(
    binary: Path,
    run: LeanctxRun,
    base: Mapping[str, str] | None = None,
) -> LeanctxStats:
    """Read and validate LeanCTX's machine-readable statistics."""
    try:
        completed = subprocess.run(
            [str(binary), "stats", "json"],
            env=leanctx_environment(run, base),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise LeanctxMonitorError(
            "managed LeanCTX is unavailable; run 'orichum doctor'"
        ) from error
    if completed.returncode != 0:
        raise LeanctxMonitorError(_native_failure(completed.stderr))
    try:
        document = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise LeanctxMonitorError(
            "LeanCTX statistics are invalid; run 'orichum doctor'"
        ) from error
    fields = (
        document.get("total_commands"),
        document.get("total_input_tokens"),
        document.get("total_output_tokens"),
    ) if isinstance(document, dict) else ()
    if (
        len(fields) != 3
        or any(
            type(value) is not int or value < 0
            for value in fields
        )
    ):
        raise LeanctxMonitorError(
            "LeanCTX statistics are invalid; run 'orichum doctor'"
        )
    commands, input_tokens, output_tokens = fields
    saved = max(input_tokens - output_tokens, 0)
    percent = saved / input_tokens * 100.0 if input_tokens else 0.0
    return LeanctxStats(
        total_commands=commands,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        saved_tokens=saved,
        savings_percent=percent,
    )


def managed_binary(data_root: Path) -> Path:
    """Resolve the fixed, current-user-owned Orichum LeanCTX executable."""
    try:
        binary_dir = require_owned_component(
            data_root,
            "bin",
            private=True,
        )
        binary = binary_dir / "lean-ctx"
        observed = os.lstat(binary)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o755
            or not os.access(binary, os.X_OK)
        ):
            raise LeanctxMonitorError("managed LeanCTX is unavailable")
        resolved = binary.resolve(strict=True)
        if resolved != binary or resolved.parent != binary_dir:
            raise LeanctxMonitorError("managed LeanCTX is unavailable")
        return resolved
    except (LeanctxMonitorError, SessionError, OSError) as error:
        raise LeanctxMonitorError(
            "managed LeanCTX is unavailable; run 'orichum doctor'"
        ) from error


def _port_is_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def first_free_loopback_port(start: int = 3333) -> int:
    """Find the first available loopback TCP port at or after start."""
    if type(start) is not int or not 1 <= start <= 65535:
        raise LeanctxMonitorError("dashboard port must be between 1 and 65535")
    for port in range(start, 65536):
        if _port_is_free(port):
            return port
    raise LeanctxMonitorError("no local dashboard port is available")


def run_watch(binary: Path, run: LeanctxRun) -> int:
    """Run LeanCTX's live terminal monitor in the foreground."""
    try:
        completed = subprocess.run(
            [str(binary), "watch"],
            env=leanctx_environment(run),
            check=False,
        )
    except OSError as error:
        raise LeanctxMonitorError(
            "managed LeanCTX is unavailable; run 'orichum doctor'"
        ) from error
    return completed.returncode


def _dashboard_config(run: LeanctxRun) -> bytes:
    path = run.run_dir / "leanctx" / "config.toml"
    try:
        observed = os.lstat(path)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LeanctxMonitorError("LeanCTX configuration is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (
                observed.st_dev,
                observed.st_ino,
            ):
                raise LeanctxMonitorError("LeanCTX configuration changed")
            return os.read(descriptor, 1024 * 1024)
        finally:
            os.close(descriptor)
    except (LeanctxMonitorError, OSError) as error:
        raise LeanctxMonitorError(
            "LeanCTX configuration is unavailable; run 'orichum doctor'"
        ) from error


def _private_state_root(state_root: Path) -> Path:
    try:
        observed = os.lstat(state_root)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise LeanctxMonitorError("Orichum state directory is unsafe")
        resolved = state_root.resolve(strict=True)
        if resolved != state_root:
            raise LeanctxMonitorError("Orichum state directory is unsafe")
        return resolved
    except (LeanctxMonitorError, OSError) as error:
        raise LeanctxMonitorError(
            "Orichum state directory is unavailable"
        ) from error


def run_dashboard(
    binary: Path,
    run: LeanctxRun,
    state_root: Path,
    port: int | None,
    open_mode: str,
) -> int:
    """Run the authenticated native dashboard against one session."""
    if open_mode not in {"browser", "none", "vscode"}:
        raise LeanctxMonitorError("dashboard open mode is invalid")
    if port is None:
        selected_port = first_free_loopback_port()
    else:
        if type(port) is not int or not 1 <= port <= 65535:
            raise LeanctxMonitorError(
                "dashboard port must be between 1 and 65535"
            )
        if not _port_is_free(port):
            raise LeanctxMonitorError(
                f"dashboard port is already occupied: {port}; "
                "omit --port to select one automatically"
            )
        selected_port = port
    state_root = _private_state_root(state_root)
    config = _dashboard_config(run)
    try:
        with tempfile.TemporaryDirectory(
            prefix="leanctx-dashboard.",
            dir=state_root,
        ) as temporary:
            config_dir = Path(temporary)
            config_path = config_dir / "config.toml"
            config_path.write_bytes(config)
            config_path.chmod(0o600)
            environment = leanctx_environment(
                run,
                config_dir=config_dir,
            )
            for name in (
                "LEAN_CTX_HTTP_TOKEN",
                "LEAN_CTX_DASHBOARD_ALLOWED_HOSTS",
                "LEAN_CTX_SCRAPE_TOKEN",
            ):
                environment.pop(name, None)
            environment["LEAN_CTX_DASHBOARD_AUTH"] = "true"
            completed = subprocess.run(
                [
                    str(binary),
                    "dashboard",
                    "--host=127.0.0.1",
                    f"--port={selected_port}",
                    f"--open={open_mode}",
                ],
                env=environment,
                check=False,
            )
            return completed.returncode
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        raise LeanctxMonitorError(
            "LeanCTX dashboard could not be started"
        ) from error
