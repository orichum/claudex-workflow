#!/usr/bin/env python3
"""Resolve repository-aware locations for centrally managed Graphify data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
from typing import Literal
from urllib.parse import quote, unquote, urlsplit
import uuid


class GraphManagerError(RuntimeError):
    """A repository cannot be mapped to a safe, unique graph location."""


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
        return [_git_text(repository, "remote", "get-url", "origin")]
    urls: list[str] = []
    for remote in remotes:
        result = _git(repository, "remote", "get-url", "--all", remote)
        if result.returncode != 0:
            raise GraphManagerError("Repository remote cannot be read")
        urls.extend(line for line in result.stdout.splitlines() if line.strip())
    return urls


def resolve_repository_identity(repository: Path) -> RepositoryIdentity:
    """Resolve an explicit, remote-derived, or persistent local identity."""
    repository = Path(repository).resolve()
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

    key = f"local/{uuid.uuid4()}"
    result = _git(repository, "config", "--local", "orichum.repositoryIdentity", key)
    if result.returncode != 0:
        raise GraphManagerError("Local repository identity cannot be persisted")
    return _identity_from_key(key, None)


def _status(repository: Path) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
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


def working_tree_fingerprint(repository: Path) -> str:
    """Hash dirty status and changed or untracked regular-file contents."""
    repository = Path(repository).resolve()
    status = _status(repository)
    digest = hashlib.sha256(status)
    for path in sorted(set(_status_paths(status))):
        content = _content_digest(repository, path)
        if content is not None:
            digest.update(path)
            digest.update(b"\0")
            digest.update(content)
    return digest.hexdigest()


def resolve_graph_target(repository: Path, data_root: Path) -> GraphTarget:
    """Return the central Graphify location for this repository state."""
    repository = Path(repository).resolve()
    identity = resolve_repository_identity(repository)
    revision = _git_text(repository, "rev-parse", "HEAD")
    status = _status(repository)
    root = Path(data_root) / "graphs" / identity.key
    if status:
        fingerprint = working_tree_fingerprint(repository)
        checkout_id = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()[:16]
        state_id = f"{checkout_id}-{fingerprint}"
        kind: Literal["revision", "working"] = "working"
        output_dir = root / "working" / state_id / "graphify-out"
    else:
        state_id = revision
        kind = "revision"
        output_dir = root / "revisions" / revision / "graphify-out"
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
