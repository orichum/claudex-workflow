#!/usr/bin/env python3
"""Resolve repository-aware locations for centrally managed Graphify data."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Literal
from urllib.parse import quote, unquote, urlsplit
import uuid

from integrations.common.graph_hooks import (
    GraphHookError,
    _launch_detached_update,
    graph_hook_status,
    install_graph_hooks,
    resolve_hook_repository,
)


class GraphManagerError(RuntimeError):
    """A repository cannot be mapped to a safe, unique graph location."""


class GraphError(GraphManagerError):
    """A centrally managed graph operation failed."""


@dataclass(frozen=True)
class RepositoryIdentity:
    key: str
    host: str
    namespace: tuple[str, ...]
    repository: str
    remote: str | None


@dataclass(frozen=True)
class GraphTarget:
    repository: Path
    identity: RepositoryIdentity
    revision: str
    kind: Literal["revision", "working"]
    state_id: str
    output_dir: Path
    graph_file: Path
    metadata_file: Path


@dataclass(frozen=True)
class GraphBinding:
    identity: str
    revision: str
    state_id: str
    graph_file: Path
    sha256: str


@dataclass(frozen=True)
class GraphOperationResult:
    repository: Path
    identity: str
    revision: str
    state_id: str
    output_dir: Path
    graph_file: Path
    action: Literal["created", "updated", "migrated", "not-applicable"]
    node_count: int


@dataclass(frozen=True)
class GraphStatus:
    target: GraphTarget
    status: Literal["current", "stale", "missing", "invalid"]
    node_count: int | None
    hook_status: str


def _path_without_symlink(path: Path, label: str) -> Path:
    if ".." in Path(path).parts:
        raise GraphManagerError(f"{label} path is unsafe")
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            observed = os.lstat(current)
        except FileNotFoundError:
            return path
        except OSError as error:
            raise GraphManagerError(f"{label} path is unavailable") from error
        if stat.S_ISLNK(observed.st_mode):
            raise GraphManagerError(f"{label} path is unsafe")
    return path


def _directory_without_symlink(
    path: Path, label: str, *, require_private: bool = False
) -> Path:
    path = _path_without_symlink(path, label)
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise GraphManagerError(f"{label} directory is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or (require_private and stat.S_IMODE(observed.st_mode) != 0o700)
    ):
        raise GraphManagerError(f"{label} directory is unsafe")
    return path


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise GraphManagerError("Git is unavailable") from error


def _git_text(repository: Path, *arguments: str) -> str:
    result = _git(repository, *arguments)
    if result.returncode != 0:
        raise GraphManagerError("Repository Git command failed")
    return result.stdout.strip()


def _filesystem_segment(value: str) -> str:
    decoded = unquote(value)
    if decoded in {"", ".", ".."}:
        return "".join(f"%{byte:02X}" for byte in decoded.encode("utf-8"))
    return quote(decoded, safe="-_.")


def normalize_remote_url(value: str) -> str:
    """Return a credential-free, filesystem-safe remote identity key."""
    value = value.strip()
    if not value:
        raise GraphManagerError("Repository remote is empty")

    scp_host = None
    scp_path = None
    if "://" not in value and ":" in value:
        prefix, candidate_path = value.split(":", 1)
        if "/" not in prefix and candidate_path:
            scp_host = prefix.rsplit("@", 1)[-1]
            scp_path = candidate_path

    if scp_host is not None:
        host = scp_host
        path = scp_path
    elif "://" not in value and "/" in value and not value.startswith("/"):
        host, path = value.split("/", 1)
    else:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        path = parsed.path
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"

    host = host.lower().strip()
    if not host:
        raise GraphManagerError("Repository remote has no host")
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        raise GraphManagerError("Repository remote has no path")
    if segments[-1].lower().endswith(".git"):
        segments[-1] = segments[-1][:-4]
    if not segments[-1]:
        raise GraphManagerError("Repository remote has no name")
    encoded = [_filesystem_segment(segment) for segment in segments]
    if any(not segment for segment in encoded):
        raise GraphManagerError("Repository remote contains an unsafe path")
    return "/".join((_filesystem_segment(host), *encoded))


def _identity_from_key(key: str, remote: str | None) -> RepositoryIdentity:
    parts = key.split("/")
    if len(parts) < 2 or any(not part for part in parts):
        raise GraphManagerError("Repository identity must include host and name")
    return RepositoryIdentity(
        key=key,
        host=parts[0],
        namespace=tuple(parts[1:-1]),
        repository=parts[-1],
        remote=remote,
    )


def _configured_identity(repository: Path) -> str | None:
    result = _git(repository, "config", "--local", "--get", "orichum.repositoryIdentity")
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    if not value:
        raise GraphManagerError("Configured repository identity is empty")
    return normalize_remote_url(value)


def _remote_urls(repository: Path) -> list[str]:
    remotes = _git_text(repository, "remote").splitlines()
    if "origin" in remotes:
        result = _git(repository, "remote", "get-url", "--all", "origin")
        if result.returncode != 0:
            raise GraphManagerError("Repository remote cannot be read")
        return [line for line in result.stdout.splitlines() if line.strip()]
    urls: list[str] = []
    for remote in remotes:
        result = _git(repository, "remote", "get-url", "--all", remote)
        if result.returncode != 0:
            raise GraphManagerError("Repository remote cannot be read")
        urls.extend(line for line in result.stdout.splitlines() if line.strip())
    return urls


def _resolve_repository_identity(
    repository: Path, *, persist: bool
) -> RepositoryIdentity:
    repository = _directory_without_symlink(repository, "Repository")
    configured = _configured_identity(repository)
    if configured is not None:
        return _identity_from_key(configured, None)

    remotes = _remote_urls(repository)
    normalized = {normalize_remote_url(remote) for remote in remotes}
    if len(normalized) == 1:
        key = normalized.pop()
        return _identity_from_key(key, key)
    if len(normalized) > 1:
        raise GraphManagerError("Repository fetch remotes are ambiguous")

    if not persist:
        raise GraphManagerError("Repository identity is not configured")
    key = f"local/{uuid.uuid4()}"
    result = _git(repository, "config", "--local", "orichum.repositoryIdentity", key)
    if result.returncode != 0:
        raise GraphManagerError("Local repository identity cannot be persisted")
    return _identity_from_key(key, None)


def resolve_repository_identity(repository: Path) -> RepositoryIdentity:
    """Resolve an explicit, remote-derived, or persistent local identity."""
    return _resolve_repository_identity(repository, persist=True)


def _status(
    repository: Path, ignored_top_level: tuple[Path, ...] = ()
) -> bytes:
    arguments = [
        "git",
        "-C",
        str(repository),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
        ":(exclude,top)graphify-out",
        ":(exclude,top)graphify-out/**",
    ]
    for ignored in ignored_top_level:
        try:
            relative = ignored.relative_to(repository)
        except ValueError as error:
            raise GraphManagerError(
                "Ignored repository status path is unsafe"
            ) from error
        if len(relative.parts) != 1:
            raise GraphManagerError("Ignored repository status path is unsafe")
        name = relative.as_posix()
        arguments.extend(
            (
                f":(exclude,top,literal){name}",
                f":(exclude,top,glob){name}/**",
            )
        )
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise GraphManagerError("Git is unavailable") from error
    if result.returncode != 0:
        raise GraphManagerError("Repository status cannot be read")
    return result.stdout


def _status_paths(status: bytes) -> list[bytes]:
    records = status.split(b"\0")
    paths: list[bytes] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise GraphManagerError("Repository status is malformed")
        paths.append(record[3:])
        if record[:1] in {b"R", b"C"} or record[1:2] in {b"R", b"C"}:
            if index >= len(records):
                raise GraphManagerError("Repository status is malformed")
            paths.append(records[index])
            index += 1
    return paths


def _content_digest(repository: Path, relative: bytes) -> bytes | None:
    relative_path = Path(relative.decode("utf-8", "surrogateescape"))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    candidate = repository / relative_path
    try:
        if not candidate.is_file() or candidate.is_symlink():
            return None
        digest = hashlib.sha256()
        with candidate.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.digest()
    except OSError as error:
        raise GraphManagerError("Changed file cannot be fingerprinted") from error


def working_tree_fingerprint(
    repository: Path, ignored_top_level: tuple[Path, ...] = ()
) -> str:
    """Hash dirty status and changed or untracked regular-file contents."""
    repository = _directory_without_symlink(repository, "Repository")
    status = _status(repository, ignored_top_level)
    digest = hashlib.sha256(status)
    for path in sorted(set(_status_paths(status))):
        content = _content_digest(repository, path)
        if content is not None:
            digest.update(path)
            digest.update(b"\0")
            digest.update(content)
    return digest.hexdigest()


@contextmanager
def _checkout_state_lock(git_dir: Path):
    try:
        descriptor = os.open(
            git_dir / "orichum.checkoutIdentity.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        raise GraphManagerError("Checkout identity cannot be synchronized") from error
    try:
        yield
    finally:
        os.close(descriptor)


def _checkout_id(repository: Path) -> str:
    git_dir = Path(_git_text(repository, "rev-parse", "--absolute-git-dir"))
    common_dir = _git_text(
        repository, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    is_main_worktree = git_dir == Path(common_dir)
    state_file = git_dir / "orichum.checkoutIdentity"
    with _checkout_state_lock(git_dir):
        result = _git(
            repository, "config", "--local", "extensions.worktreeConfig", "true"
        )
        if result.returncode != 0:
            raise GraphManagerError("Worktree configuration cannot be enabled")
        prior_worktree = _git(
            repository, "config", "--worktree", "--get", "orichum.checkoutIdentity"
        )
        legacy = _git(
            repository, "config", "--local", "--get", "orichum.checkoutIdentity"
        )
        try:
            persisted = state_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            persisted = None
        except OSError as error:
            raise GraphManagerError("Checkout identity cannot be read") from error
        persisted_id: str | None = None
        if persisted is not None:
            try:
                persisted_id = uuid.UUID(persisted).hex
            except ValueError as error:
                raise GraphManagerError("Persisted checkout identity is invalid") from error
        remove_prior_worktree = prior_worktree.returncode == 0
        remove_legacy = (
            not remove_prior_worktree
            and persisted_id is None
            and legacy.returncode == 0
            and is_main_worktree
        )
        configured = prior_worktree if remove_prior_worktree else legacy
        if remove_prior_worktree or remove_legacy:
            try:
                checkout_id = uuid.UUID(configured.stdout.strip()).hex
            except ValueError as error:
                raise GraphManagerError("Persisted checkout identity is invalid") from error
        elif persisted_id is not None:
            return persisted_id
        else:
            checkout_id = uuid.uuid4().hex
        if checkout_id != persisted_id:
            temporary_path: Path | None = None
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".orichum.checkoutIdentity.",
                    suffix=".tmp",
                    dir=git_dir,
                )
                temporary_path = Path(temporary_name)
                try:
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "w", encoding="ascii") as file:
                        descriptor = -1
                        file.write(checkout_id)
                        file.flush()
                        os.fsync(file.fileno())
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                os.replace(temporary_path, state_file)
                temporary_path = None
                directory = os.open(
                    git_dir,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as error:
                raise GraphManagerError("Checkout identity cannot be persisted") from error
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
        if remove_prior_worktree:
            result = _git(
                repository,
                "config",
                "--worktree",
                "--unset-all",
                "orichum.checkoutIdentity",
            )
            if result.returncode != 0:
                raise GraphManagerError("Prior checkout identity cannot be removed")
        if remove_legacy:
            result = _git(
                repository,
                "config",
                "--local",
                "--unset-all",
                "orichum.checkoutIdentity",
            )
            if result.returncode != 0:
                raise GraphManagerError("Legacy checkout identity cannot be removed")
        return checkout_id


def _existing_checkout_id(repository: Path) -> str:
    git_dir = Path(_git_text(repository, "rev-parse", "--absolute-git-dir"))
    state_file = git_dir / "orichum.checkoutIdentity"
    try:
        persisted = state_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        persisted = ""
    except OSError as error:
        raise GraphManagerError("Checkout identity cannot be read") from error
    if persisted:
        try:
            return uuid.UUID(persisted).hex
        except ValueError as error:
            raise GraphManagerError("Persisted checkout identity is invalid") from error
    for scope in ("--worktree", "--local"):
        result = _git(
            repository,
            "config",
            scope,
            "--get",
            "orichum.checkoutIdentity",
        )
        if result.returncode == 0:
            try:
                return uuid.UUID(result.stdout.strip()).hex
            except ValueError as error:
                raise GraphManagerError(
                    "Persisted checkout identity is invalid"
                ) from error
    raise GraphManagerError("Checkout identity is not configured")


def _resolve_graph_target(
    repository: Path,
    data_root: Path,
    *,
    persist: bool,
    ignored_top_level: tuple[Path, ...] = (),
) -> GraphTarget:
    repository = _directory_without_symlink(repository, "Repository")
    data_root = _directory_without_symlink(
        data_root, "Graph data", require_private=True
    )
    identity = _resolve_repository_identity(repository, persist=persist)
    revision = _git_text(repository, "rev-parse", "HEAD")
    status = _status(repository, ignored_top_level)
    root = data_root / "graphs" / identity.key
    if status:
        fingerprint = working_tree_fingerprint(
            repository, ignored_top_level
        )
        checkout_id = (
            _checkout_id(repository)
            if persist
            else _existing_checkout_id(repository)
        )
        state_id = f"{checkout_id}-{fingerprint}"
        kind: Literal["revision", "working"] = "working"
        output_dir = root / "working" / state_id / "graphify-out"
    else:
        state_id = revision
        kind = "revision"
        output_dir = root / "revisions" / revision / "graphify-out"
    _path_without_symlink(output_dir, "Graph output")
    return GraphTarget(
        repository=repository,
        identity=identity,
        revision=revision,
        kind=kind,
        state_id=state_id,
        output_dir=output_dir,
        graph_file=output_dir / "graph.json",
        metadata_file=output_dir / "metadata.json",
    )


def resolve_graph_target(repository: Path, data_root: Path) -> GraphTarget:
    """Return the central Graphify location for this repository state."""
    return _resolve_graph_target(repository, data_root, persist=True)


def _graph_file_digest(graph_file: Path) -> str:
    graph_file = _path_without_symlink(graph_file, "Graph file")
    descriptor = None
    try:
        descriptor = os.open(
            graph_file,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
        ):
            raise GraphError("Graphify graph is unsafe")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        path_after = os.stat(graph_file, follow_symlinks=False)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field)
            or getattr(after, field) != getattr(path_after, field)
            for field in stable_fields
        ):
            raise GraphError("Graphify graph changed during validation")
        return digest.hexdigest()
    except GraphError:
        raise
    except OSError as error:
        raise GraphError("Graphify graph is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def resolve_available_graph(
    repository: Path, data_root: Path
) -> GraphBinding | None:
    """Return a read-only binding for the graph matching repository state."""
    try:
        target = _resolve_graph_target(repository, data_root, persist=False)
        status = inspect_graph(target)
        if status.status != "current":
            return None
        digest = _graph_file_digest(target.graph_file)
        current = _resolve_graph_target(repository, data_root, persist=False)
        if (
            current.identity.key != target.identity.key
            or current.revision != target.revision
            or current.state_id != target.state_id
            or current.graph_file != target.graph_file
            or inspect_graph(current).status != "current"
        ):
            return None
        if not hmac.compare_digest(
            digest, _graph_file_digest(current.graph_file)
        ):
            return None
        return GraphBinding(
            identity=current.identity.key,
            revision=current.revision,
            state_id=current.state_id,
            graph_file=current.graph_file,
            sha256=digest,
        )
    except GraphManagerError:
        return None


_LEGACY_GRAPHIFY_ENTRIES = frozenset(
    {
        ".graphify_ast.json",
        ".graphify_analysis.json",
        ".graphify_cached.json",
        ".graphify_detect.json",
        ".graphify_extract.json",
        ".graphify_build.json",
        ".graphify_labels.json",
        ".graphify_learning.json",
        ".graphify_obsidian_manifest.json",
        ".graphify_python",
        ".graphify_root",
        ".graphify_semantic.json",
        ".graphify_semantic_marker",
        ".graphify_semantic_new.json",
        ".graphify_uncached.txt",
        ".graphify_version",
        ".graph.tmp.json",
        ".needs_update",
        "GRAPH_REPORT.md",
        "GRAPH_TREE.html",
        "cache",
        "cost.json",
        "cypher.txt",
        "graph.graphml",
        "graph.html",
        "graph.json",
        "graph.svg",
        "index.md",
        "manifest.json",
        "memory",
        "merged-graph.json",
        "needs_update",
        "obsidian",
        "reflections",
        "transcripts",
        "wiki",
    }
)


def _recognized_legacy_entry(name: str) -> bool:
    return (
        name in _LEGACY_GRAPHIFY_ENTRIES
        or re.fullmatch(r"\.graphify_chunk_\d+\.json", name) is not None
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) is not None
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
            r"-callflow\.html",
            name,
        )
        is not None
    )


def _private_directory(path: Path, label: str, *, create: bool = False) -> Path:
    path = _path_without_symlink(path, label)
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise GraphError(f"{label} directory cannot be created") from error
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise GraphError(f"{label} directory is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise GraphError(f"{label} directory is unsafe")
    return path


def _graph_identity_root(
    identity: RepositoryIdentity, data_root: Path
) -> Path:
    data_root = _directory_without_symlink(
        data_root, "Graph data", require_private=True
    )
    graphs = _private_directory(
        data_root / "graphs", "Graph root", create=True
    )
    current = graphs
    for segment in identity.key.split("/"):
        current = _private_directory(
            current / segment, "Repository graph root", create=True
        )
    return current


@contextmanager
def _graph_lock(identity: RepositoryIdentity, data_root: Path):
    root = _graph_identity_root(identity, data_root)
    descriptor = None
    try:
        descriptor = os.open(
            root / ".orichum.lock",
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise GraphError("Repository graph lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except GraphError:
        raise
    except OSError as error:
        raise GraphError("Repository graph lock failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json_file(path: Path, label: str) -> object:
    descriptor = None
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
        ):
            raise GraphError(f"{label} is unsafe")
        with os.fdopen(descriptor, encoding="utf-8") as source:
            descriptor = None
            return json.load(source)
    except GraphError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GraphError(f"{label} is invalid") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_graph_file(graph_file: Path, revision: str) -> int:
    parsed = _read_json_file(graph_file, "Graphify graph")
    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise GraphError("Graphify graph is invalid")
    built_at_commit = parsed.get("built_at_commit")
    if (
        not isinstance(built_at_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", built_at_commit) is None
        or built_at_commit != revision
    ):
        raise GraphError("Graphify graph provenance is invalid")
    links = parsed.get("links", [])
    if not isinstance(links, list):
        raise GraphError("Graphify graph is invalid")
    for entry in (*nodes, *links):
        if not isinstance(entry, dict):
            raise GraphError("Graphify graph is invalid")
        source_file = entry.get("source_file")
        if source_file is None or source_file == "":
            continue
        if not isinstance(source_file, str):
            raise GraphError("Graphify graph source path is invalid")
        relative = Path(source_file)
        if relative.is_absolute() or ".." in relative.parts:
            raise GraphError("Graphify graph source path is invalid")
    return len(nodes)


def _metadata(target: GraphTarget) -> dict[str, object]:
    metadata: dict[str, object] = {
        "schema_version": 1,
        "repository_identity": target.identity.key,
        "revision": target.revision,
        "state_id": target.state_id,
        "kind": target.kind,
        "built_at_commit": target.revision,
    }
    if target.kind == "working":
        metadata["checkout_path"] = str(target.repository)
    return metadata


def _valid_working_metadata(
    metadata: object,
    identity: RepositoryIdentity,
    state_id: str,
) -> str | None:
    if not isinstance(metadata, dict):
        return None
    revision = metadata.get("revision")
    checkout = metadata.get("checkout_path")
    if (
        metadata.get("schema_version") != 1
        or metadata.get("repository_identity") != identity.key
        or metadata.get("kind") != "working"
        or metadata.get("state_id") != state_id
        or not isinstance(revision, str)
        or not revision
        or metadata.get("built_at_commit") != revision
        or not isinstance(checkout, str)
        or not Path(checkout).is_absolute()
    ):
        return None
    return checkout


def _write_metadata(target: GraphTarget, output_dir: Path) -> None:
    metadata_file = output_dir / "metadata.json"
    temporary = output_dir / f".metadata.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(_metadata(target), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, metadata_file)
    except OSError as error:
        raise GraphError("Graph metadata cannot be written") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _validate_output(target: GraphTarget, output_dir: Path) -> int:
    output_dir = _private_directory(output_dir, "Graph output")
    node_count = _validate_graph_file(
        output_dir / "graph.json", target.revision
    )
    metadata = _read_json_file(output_dir / "metadata.json", "Graph metadata")
    if not isinstance(metadata, dict):
        raise GraphError("Graph metadata is invalid")
    expected = _metadata(target)
    for key in (
        "schema_version",
        "repository_identity",
        "revision",
        "state_id",
        "kind",
    ):
        if metadata.get(key) != expected[key]:
            raise GraphError("Graph metadata does not match its target")
    if (
        target.kind == "working"
        and metadata.get("checkout_path") != expected["checkout_path"]
    ):
        raise GraphError("Graph metadata does not match its target")
    return node_count


def inspect_graph(target: GraphTarget) -> GraphStatus:
    """Inspect an existing central graph without modifying it."""
    if not os.path.lexists(target.output_dir):
        return GraphStatus(target, "missing", None, "unknown")
    try:
        node_count = _validate_output(target, target.output_dir)
        metadata = _read_json_file(target.metadata_file, "Graph metadata")
    except GraphError:
        return GraphStatus(target, "invalid", None, "unknown")
    if (
        not isinstance(metadata, dict)
        or metadata.get("built_at_commit") != target.revision
    ):
        return GraphStatus(target, "stale", node_count, "unknown")
    return GraphStatus(target, "current", node_count, "unknown")


def discover_graph_targets(path: Path) -> tuple[Path, ...]:
    """Discover canonical Git repositories below a project path."""
    from integrations.common.context_population import discover_git_worktrees

    return discover_git_worktrees(path)


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            candidate = current_path / name
            observed = os.lstat(candidate)
            if stat.S_ISLNK(observed.st_mode) or observed.st_uid != os.getuid():
                raise GraphError("Legacy Graphify output is unsafe")
        for filename in files:
            candidate = current_path / filename
            if not stat.S_ISREG(os.lstat(candidate).st_mode):
                raise GraphError("Legacy Graphify output is unsafe")
            digest = hashlib.sha256()
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[str(candidate.relative_to(root))] = digest.hexdigest()
    return hashes


def _copy_legacy_tree(source: Path, destination: Path) -> dict[str, str]:
    """Copy an owned tree through no-follow descriptors and hash its files."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(source, flags)
    except OSError as error:
        raise GraphError("Legacy Graphify output is unsafe") from error
    hashes: dict[str, str] = {}

    def copy_directory(
        source_descriptor: int,
        target: Path,
        relative: Path,
    ) -> None:
        for name in sorted(os.listdir(source_descriptor)):
            observed = os.stat(
                name, dir_fd=source_descriptor, follow_symlinks=False
            )
            child_relative = relative / name
            child_target = target / name
            if observed.st_uid != os.getuid() or stat.S_ISLNK(observed.st_mode):
                raise GraphError("Legacy Graphify output is unsafe")
            if stat.S_ISDIR(observed.st_mode):
                child_target.mkdir(mode=0o700)
                child_descriptor = os.open(
                    name, flags, dir_fd=source_descriptor
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or opened.st_uid != os.getuid()
                        or (opened.st_dev, opened.st_ino)
                        != (observed.st_dev, observed.st_ino)
                    ):
                        raise GraphError("Legacy Graphify output changed")
                    copy_directory(
                        child_descriptor, child_target, child_relative
                    )
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise GraphError("Legacy Graphify output is unsafe")
            file_descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=source_descriptor,
            )
            try:
                opened = os.fstat(file_descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or (opened.st_dev, opened.st_ino)
                    != (observed.st_dev, observed.st_ino)
                ):
                    raise GraphError("Legacy Graphify output changed")
                digest = hashlib.sha256()
                with os.fdopen(file_descriptor, "rb") as source_stream:
                    file_descriptor = -1
                    with child_target.open("xb") as target_stream:
                        os.chmod(child_target, 0o600)
                        for chunk in iter(
                            lambda: source_stream.read(1024 * 1024), b""
                        ):
                            digest.update(chunk)
                            target_stream.write(chunk)
                hashes[str(child_relative)] = digest.hexdigest()
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)

    try:
        root_observed = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_observed.st_mode)
            or root_observed.st_uid != os.getuid()
        ):
            raise GraphError("Legacy Graphify output is unsafe")
        entries = set(os.listdir(root_descriptor))
        if any(not _recognized_legacy_entry(name) for name in entries):
            raise GraphError(
                "Legacy Graphify output contains unknown entries"
            )
        destination.mkdir(mode=0o700)
        copy_directory(root_descriptor, destination, Path())
    except GraphError:
        raise
    except OSError as error:
        raise GraphError("Legacy Graphify migration failed") from error
    finally:
        os.close(root_descriptor)
    return hashes


def _prepare_target_parent(target: GraphTarget) -> None:
    identity_root = _graph_identity_root(target.identity, _target_data_root(target))
    relative_parent = target.output_dir.parent.relative_to(identity_root)
    current = identity_root
    for segment in relative_parent.parts:
        current = _private_directory(
            current / segment, "Graph state directory", create=True
        )


def _target_data_root(target: GraphTarget) -> Path:
    marker = target.identity.key.split("/")
    candidate = target.output_dir
    for _ in range(4 + len(marker)):
        candidate = candidate.parent
    return candidate


def _require_current_binding(
    target: GraphTarget,
    data_root: Path,
    ignored_top_level: tuple[Path, ...] = (),
) -> None:
    current = _resolve_graph_target(
        target.repository,
        data_root,
        persist=True,
        ignored_top_level=ignored_top_level,
    )
    expected = (
        target.repository,
        target.identity.key,
        target.revision,
        target.kind,
        target.state_id,
        target.output_dir,
    )
    observed = (
        current.repository,
        current.identity.key,
        current.revision,
        current.kind,
        current.state_id,
        current.output_dir,
    )
    if observed != expected:
        raise GraphError(
            "Repository state changed during graph synchronization; retry "
            "the command"
        )


def _activate_staged_output(staged: Path, active: Path) -> None:
    if not os.path.lexists(active):
        try:
            os.replace(staged, active)
        except OSError as error:
            raise GraphError("Graph activation failed") from error
        return
    _atomic_exchange_directories(staged, active)
    try:
        shutil.rmtree(staged)
    except OSError:
        pass


def _atomic_exchange_directories(first: Path, second: Path) -> None:
    """Atomically exchange two directory entries on supported platforms."""
    libc = ctypes.CDLL(None, use_errno=True)
    at_fdcwd = -2
    first_bytes = os.fsencode(first)
    second_bytes = os.fsencode(second)
    if sys.platform == "darwin":
        exchange = getattr(libc, "renameatx_np", None)
        if exchange is None:
            raise GraphError("Atomic graph activation is unsupported")
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(
            at_fdcwd, first_bytes, at_fdcwd, second_bytes, 0x00000002
        )
    elif sys.platform.startswith("linux"):
        exchange = getattr(libc, "renameat2", None)
        if exchange is None:
            raise GraphError("Atomic graph activation is unsupported")
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(
            at_fdcwd, first_bytes, at_fdcwd, second_bytes, 0x00000002
        )
    else:
        raise GraphError("Atomic graph activation is unsupported")
    if result != 0:
        error_number = ctypes.get_errno()
        raise GraphError(
            f"Atomic graph activation failed: {os.strerror(error_number)}"
        )


def migrate_legacy_graph(target: GraphTarget) -> bool:
    """Move a recognized repository-local Graphify directory into storage."""
    legacy = target.repository / "graphify-out"
    if not os.path.lexists(legacy):
        return False
    if os.path.lexists(target.output_dir):
        raise GraphError(
            "Legacy Graphify output cannot replace an active graph"
        )
    try:
        repository = _directory_without_symlink(
            target.repository, "Repository"
        )
        legacy = _directory_without_symlink(
            legacy, "Legacy Graphify output"
        )
    except GraphManagerError as error:
        raise GraphError(str(error)) from error
    try:
        legacy.relative_to(repository)
    except ValueError as error:
        raise GraphError("Legacy Graphify output escapes the repository") from error
    _prepare_target_parent(target)
    staged = target.output_dir.parent / (
        f".graphify-out.migration-{uuid.uuid4().hex}"
    )
    quarantined = repository / (
        f".orichum-legacy-graphify-{uuid.uuid4().hex}"
    )
    source_quarantined = False
    activated = False
    try:
        os.replace(legacy, quarantined)
        source_quarantined = True
        source_hashes = _copy_legacy_tree(quarantined, staged)
        if source_hashes != _tree_hashes(staged):
            raise GraphError("Legacy Graphify output copy verification failed")
        data_root = _target_data_root(target)
        _require_current_binding(target, data_root, (quarantined,))
        _validate_graph_file(staged / "graph.json", target.revision)
        _write_metadata(target, staged)
        _validate_output(target, staged)
        _require_current_binding(target, data_root, (quarantined,))
        _activate_staged_output(staged, target.output_dir)
        activated = True
        try:
            shutil.rmtree(quarantined)
        except OSError:
            try:
                os.replace(target.output_dir, staged)
            except OSError as rollback_error:
                raise GraphError(
                    "Legacy cleanup failed and activation could not roll back"
                ) from rollback_error
            activated = False
            raise
        source_quarantined = False
    except GraphError:
        raise
    except OSError as error:
        raise GraphError("Legacy Graphify migration failed") from error
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        if (
            source_quarantined
            and not activated
            and not os.path.lexists(legacy)
        ):
            try:
                os.replace(quarantined, legacy)
                source_quarantined = False
            except OSError:
                pass
    return True


def _graphify_failure(completed: subprocess.CompletedProcess[str]) -> GraphError:
    diagnostics = "\n".join(
        line
        for output in (completed.stdout, completed.stderr)
        for line in output.splitlines()[-20:]
        if line.strip()
    )
    prefix = f"Graphify failed with exit code {completed.returncode}"
    return GraphError(f"{prefix}: {diagnostics[-3500:]}" if diagnostics else prefix)


def _not_applicable(completed: subprocess.CompletedProcess[str]) -> bool:
    return (
        completed.returncode != 0
        and "found 0 code" in completed.stdout.lower()
        and "graph is empty" in completed.stderr.lower()
    )


def _run_graphify(
    graphify: str,
    arguments: list[str],
    output_dir: Path,
    repository: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GRAPHIFY_OUT"] = str(output_dir.absolute())
    try:
        return subprocess.run(
            [graphify, *arguments],
            check=False,
            capture_output=True,
            text=True,
            cwd=repository,
            env=environment,
        )
    except OSError as error:
        raise GraphError("Graphify executable is unavailable") from error


def _result(
    target: GraphTarget,
    action: Literal["created", "updated", "migrated", "not-applicable"],
    node_count: int,
) -> GraphOperationResult:
    return GraphOperationResult(
        repository=target.repository,
        identity=target.identity.key,
        revision=target.revision,
        state_id=target.state_id,
        output_dir=target.output_dir,
        graph_file=target.graph_file,
        action=action,
        node_count=node_count,
    )


def prune_orphaned_working_graphs(
    identity: RepositoryIdentity, data_root: Path
) -> tuple[Path, ...]:
    """Remove only validated working targets whose checkout no longer exists."""
    identity_root = _graph_identity_root(identity, data_root)
    working = identity_root / "working"
    if not working.exists():
        return ()
    working = _private_directory(working, "Working graph root")
    removed = []
    for state_root in sorted(working.iterdir(), key=lambda path: str(path)):
        try:
            state_root = _private_directory(
                state_root, "Working graph state"
            )
            output = _private_directory(
                state_root / "graphify-out", "Working graph output"
            )
            metadata = _read_json_file(
                output / "metadata.json", "Graph metadata"
            )
            checkout = _valid_working_metadata(
                metadata, identity, state_root.name
            )
            if checkout is None:
                continue
            if Path(checkout).exists():
                continue
            shutil.rmtree(state_root)
            removed.append(state_root)
        except (GraphError, OSError):
            continue
    return tuple(removed)


def sync_graph(
    repository: Path,
    data_root: Path,
    *,
    graphify: str,
    progress: Callable[[str], None] | None = None,
) -> GraphOperationResult:
    """Create or transactionally update one central repository graph."""
    initial = resolve_graph_target(repository, data_root)
    with _graph_lock(initial.identity, data_root):
        target = resolve_graph_target(repository, data_root)
        if target.identity.key != initial.identity.key:
            raise GraphError("Repository identity changed during graph sync")
        if migrate_legacy_graph(target):
            node_count = inspect_graph(target).node_count
            if node_count is None:
                raise GraphError("Migrated graph is invalid")
            prune_orphaned_working_graphs(target.identity, data_root)
            _install_graph_hooks(target.repository, progress)
            return _result(target, "migrated", node_count)

        status = inspect_graph(target)
        _prepare_target_parent(target)
        if status.status in {"missing", "stale", "invalid"}:
            action: Literal["created", "updated"] = (
                "created" if status.status == "missing" else "updated"
            )
            output_dir = target.output_dir.parent / (
                f".graphify-out.staging-{uuid.uuid4().hex}"
            )
            _private_directory(output_dir, "Graph output", create=True)
            arguments = ["extract", str(target.repository), "--code-only"]
        else:
            action = "updated"
            output_dir = target.output_dir.parent / (
                f".graphify-out.staging-{uuid.uuid4().hex}"
            )
            shutil.copytree(target.output_dir, output_dir)
            os.chmod(output_dir, 0o700)
            arguments = ["update", str(target.repository)]
        if progress is not None:
            progress(action)
        try:
            completed = _run_graphify(
                graphify, arguments, output_dir, target.repository
            )
            if _not_applicable(completed) and action == "created":
                shutil.rmtree(output_dir, ignore_errors=True)
                prune_orphaned_working_graphs(target.identity, data_root)
                if progress is not None:
                    progress("not-applicable")
                return _result(target, "not-applicable", 0)
            if completed.returncode != 0:
                raise _graphify_failure(completed)
            _require_current_binding(target, data_root)
            _validate_graph_file(output_dir / "graph.json", target.revision)
            _write_metadata(target, output_dir)
            node_count = _validate_output(target, output_dir)
            _require_current_binding(target, data_root)
            _activate_staged_output(output_dir, target.output_dir)
            prune_orphaned_working_graphs(target.identity, data_root)
            _install_graph_hooks(target.repository, progress)
            return _result(target, action, node_count)
        except BaseException:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise


def sync_graphs(
    path: Path,
    data_root: Path,
    *,
    graphify: str,
    progress: Callable[[str], None] | None = None,
) -> tuple[GraphOperationResult, ...]:
    """Synchronize every canonical Git repository discovered below ``path``."""
    repositories = discover_graph_targets(path)
    results = []
    total = len(repositories)
    for index, repository in enumerate(repositories, start=1):
        operation_progress = None
        if progress is not None:
            operation_progress = lambda action, i=index, item=repository: progress(
                f"[graphify {i}/{total}] {action} {item.name}"
            )
        results.append(
            sync_graph(
                repository,
                data_root,
                graphify=graphify,
                progress=operation_progress,
            )
        )
    return tuple(results)


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
        raise GraphManagerError("ORICHUM_DATA_HOME must be an absolute path")
    return path.resolve(strict=False)


def _command_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(os.path.abspath(path))
    if not path.is_dir():
        raise GraphManagerError(f"path is not a directory: {value}")
    return _directory_without_symlink(path, "Graph scope")


def _resolve_graphify() -> str:
    executable = shutil.which("graphify")
    if executable is None:
        raise GraphManagerError("Graphify executable is unavailable")
    return executable


def _orichum_launcher() -> Path:
    return Path(__file__).resolve().parents[2] / "bin" / "orichum"


def _bounded_hook_diagnostic(error: GraphHookError) -> str:
    escaped = []
    length = 0
    for character in str(error):
        if character.isprintable() and character not in "\r\n":
            rendered = character
        else:
            codepoint = ord(character)
            rendered = (
                f"\\u{codepoint:04x}"
                if codepoint <= 0xFFFF
                else f"\\U{codepoint:08x}"
            )
        escaped.append(rendered)
        length += len(rendered)
        if length >= 256:
            break
    return "".join(escaped)[:256]


def _install_graph_hooks(
    repository: Path,
    progress: Callable[[str], None] | None = None,
) -> None:
    try:
        install_graph_hooks(repository, _orichum_launcher())
    except GraphHookError as error:
        if progress is not None:
            progress(f"hook not managed: {_bounded_hook_diagnostic(error)}")


_HOOK_SYNC_LAUNCHER = (
    "import sys\n"
    "root = sys.argv.pop(1)\n"
    "sys.path.insert(0, root)\n"
    "from integrations.common.graph_manager import graph_main\n"
    "raise SystemExit(graph_main(sys.argv[1:]))\n"
)


def _graph_hook_update(value: str) -> int:
    repository = resolve_hook_repository(Path(value))
    workflow_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _HOOK_SYNC_LAUNCHER,
        str(workflow_root),
        "__hook-sync",
        str(repository),
    ]
    _launch_detached_update(repository, _graph_data_root(), command)
    return 0


def _graph_hook_sync(value: str) -> int:
    repository = resolve_hook_repository(Path(value))
    sync_graph(
        repository,
        _graph_data_root(),
        graphify=_resolve_graphify(),
        progress=print,
    )
    return 0


def _bounded_command(
    arguments: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _package_version(graphify: str | None) -> str:
    if graphify is None:
        return "unavailable"
    completed = _bounded_command([graphify, "--version"])
    if completed is None or completed.returncode != 0:
        return "unknown"
    match = re.search(
        r"\bgraphify(?:y)?\s+([^\s]+)", completed.stdout[:512], re.IGNORECASE
    )
    return match.group(1) if match else "unknown"


def _skill_version() -> str:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    candidates = (
        home / ".agents" / "skills" / "graphify" / ".graphify_version",
        home / ".codex" / "skills" / "graphify" / ".graphify_version",
        home / ".claude" / "skills" / "graphify" / ".graphify_version",
    )
    for candidate in candidates:
        try:
            with candidate.open("r", encoding="ascii") as stream:
                value = stream.read(128).strip()
        except (OSError, UnicodeError):
            continue
        if value:
            return value[:64]
    return "unavailable"


def _render_status_table(rows: list[tuple[str, ...]]) -> str:
    headers = (
        "REPOSITORY",
        "REVISION",
        "STATE",
        "GRAPH",
        "NODES",
        "HOOK",
        "OUTPUT",
    )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(values: tuple[str, ...]) -> str:
        return "  ".join(
            f"{value:<{width}}"
            for value, width in zip(values, widths, strict=True)
        ).rstrip()

    separator = render(tuple("-" * width for width in widths))
    return "\n".join(
        (render(headers), separator, *(render(row) for row in rows))
    )


def _status_rows(
    path: Path, data_root: Path, graphify: str | None
) -> tuple[list[tuple[str, ...]], list[str]]:
    rows: list[tuple[str, ...]] = []
    details: list[str] = []
    for repository in discover_graph_targets(path):
        try:
            identity = _resolve_repository_identity(
                repository, persist=False
            )
            identity_key = identity.key
            revision = _git_text(repository, "rev-parse", "HEAD")
            dirty = bool(_status(repository))
            try:
                target = _resolve_graph_target(
                    repository, data_root, persist=False
                )
                inspected = inspect_graph(target)
                graph_state = inspected.status
                nodes = (
                    str(inspected.node_count)
                    if inspected.node_count is not None
                    else "—"
                )
                output = str(target.output_dir)
            except GraphManagerError as error:
                graph_state = "unknown"
                nodes = "—"
                output = "—"
                details.append(f"  {repository}: {error}")
            rows.append(
                (
                    identity_key,
                    revision[:12],
                    "dirty" if dirty else "clean",
                    graph_state,
                    nodes,
                    graph_hook_status(repository),
                    output,
                )
            )
            if dirty or graph_state == "invalid":
                details.append(f"  checkout: {repository}")
        except GraphManagerError as error:
            rows.append(
                (
                    "(invalid)",
                    "—",
                    "invalid",
                    "unknown",
                    "—",
                    "unknown",
                    "—",
                )
            )
            details.append(f"  {repository}: {error}")
    return rows, details


def _graph_status(path: Path) -> int:
    data_root = _graph_data_root()
    graphify = shutil.which("graphify")
    rows, details = _status_rows(path, data_root, graphify)
    print(_render_status_table(rows))
    for detail in details:
        print(detail)
    package = _package_version(graphify)
    skill = _skill_version()
    drift = " (drift)" if package != skill else ""
    print(f"Graphify package: {package}")
    print(f"Graphify skill: {skill}{drift}")
    return 0


def _graph_identity(arguments: list[str]) -> int:
    if not arguments:
        raise GraphManagerError(
            "graph identity requires PATH and exactly one of --set ID or --clear"
        )
    repository = _command_path(arguments[0])
    remainder = arguments[1:]
    setting: str | None = None
    clear = False
    if len(remainder) == 2 and remainder[0] == "--set":
        setting = normalize_remote_url(remainder[1])
    elif remainder == ["--clear"]:
        clear = True
    else:
        raise GraphManagerError(
            "graph identity requires exactly one of --set ID or --clear"
        )
    result = (
        _git(
            repository,
            "config",
            "--local",
            "orichum.repositoryIdentity",
            setting,
        )
        if setting is not None
        else _git(
            repository,
            "config",
            "--local",
            "--unset-all",
            "orichum.repositoryIdentity",
        )
    )
    if result.returncode != 0 and not clear:
        raise GraphManagerError("Repository identity cannot be set")
    if result.returncode not in (0, 5) and clear:
        raise GraphManagerError("Repository identity cannot be cleared")
    action = f"set to {setting}" if setting is not None else "cleared"
    print(f"Repository identity {action}: {repository}")
    return 0


def graph_main(arguments: list[str] | None = None) -> int:
    """Run the repository-aware Orichum graph command."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    try:
        if arguments and arguments[0] == "hook-update":
            if len(arguments) != 2:
                raise GraphManagerError("graph hook-update requires PATH")
            return _graph_hook_update(arguments[1])
        if arguments and arguments[0] == "__hook-sync":
            if len(arguments) != 2:
                raise GraphManagerError("graph hook sync requires PATH")
            return _graph_hook_sync(arguments[1])
        if arguments and arguments[0] == "status":
            if len(arguments) > 2:
                raise GraphManagerError("graph status accepts at most one PATH")
            path = _command_path(arguments[1] if len(arguments) == 2 else ".")
            return _graph_status(path)
        if arguments and arguments[0] == "identity":
            return _graph_identity(arguments[1:])
        if len(arguments) > 1:
            raise GraphManagerError("graph accepts at most one PATH")
        path = _command_path(arguments[0] if arguments else ".")
        repositories = discover_graph_targets(path)
        print(f"[discover] found {len(repositories)} repositories")
        if not repositories:
            return 0
        sync_graphs(
            path,
            _graph_data_root(),
            graphify=_resolve_graphify(),
            progress=print,
        )
        return 0
    except (GraphHookError, GraphManagerError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(graph_main())
