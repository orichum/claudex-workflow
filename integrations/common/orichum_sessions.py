#!/usr/bin/env python3
"""Immutable, private logical-session bindings for Orichum."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Collection, Mapping, Sequence
from types import MappingProxyType
import uuid

from .account_registry import Account
from .model_routing import (
    EffectiveStack,
    ROLES,
    RoutingError,
    validate_model_id,
    validate_stack_name,
)
from .route_selection import Route, RouteError, route_chain
from .stack_bindings import StackBindings
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    StackDefinitionError,
    normalize_model_stacks,
)


MAX_BINDING_BYTES = 1024 * 1024
_SESSION_ID = re.compile(r"oc-s-[a-f0-9]{16}")
_ACCOUNT_ID = re.compile(r"oc-a-[a-f0-9]{16}")
_PROFILE = re.compile(r"ocp-[a-f0-9]{16}")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_UPSTREAM = re.compile(
    r"oc-r-[a-f0-9]{16}/[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}"
)


class LogicalSessionError(RuntimeError):
    """Logical session state failed closed validation."""


@dataclass(frozen=True)
class RouteBinding:
    primary: Route
    fallbacks: tuple[Route, ...]


@dataclass(frozen=True)
class LogicalSession:
    id: str
    claude_session_id: str
    parent_id: str | None
    project_root: Path
    stack: str
    controller: RouteBinding
    agents: Mapping[str, RouteBinding]
    created_at: str


@dataclass(frozen=True)
class ResolvedSessionPlan:
    stack: str
    controller: RouteBinding
    agents: Mapping[str, RouteBinding]
    effective: EffectiveStack


def _require_private_directory(path: Path, label: str) -> Path:
    try:
        observed = os.lstat(path)
        resolved = path.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as failure:
        raise LogicalSessionError(f"{label} is unavailable") from failure
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or observed.st_dev != confirmed.st_dev
        or observed.st_ino != confirmed.st_ino
    ):
        raise LogicalSessionError(f"{label} is unsafe")
    return resolved


def _session_root(state_home: Path) -> Path:
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    root = state / "logical-sessions"
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as failure:
        raise LogicalSessionError(
            "logical session directory could not be created"
        ) from failure
    return _require_private_directory(root, "logical session directory")


def _staging_root(state_home: Path) -> Path:
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    root = state / "logical-session-staging"
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as failure:
        raise LogicalSessionError(
            "logical session staging directory could not be created"
        ) from failure
    return _require_private_directory(root, "logical session staging directory")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LogicalSessionError(f"{label} is invalid")
    return value


def _route_json(route: Route) -> dict[str, object]:
    return {
        "accountId": route.account_id,
        "provider": route.provider,
        "family": route.family,
        "logicalModel": route.logical_model,
        "upstreamModel": route.upstream_model,
        "profile": route.claudex_profile,
        "priority": route.priority,
        "pool": route.pool,
    }


def _parse_route(value: object) -> Route:
    keys = {
        "accountId",
        "provider",
        "family",
        "logicalModel",
        "upstreamModel",
        "profile",
        "priority",
        "pool",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise LogicalSessionError("session route has invalid fields")
    account_id = value["accountId"]
    profile = value["profile"]
    upstream = value["upstreamModel"]
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise LogicalSessionError("session route account ID is invalid")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise LogicalSessionError("session route profile is invalid")
    if (
        not isinstance(upstream, str)
        or not _UPSTREAM.fullmatch(upstream)
        or "://" in upstream
    ):
        raise LogicalSessionError("session route upstream model is invalid")
    if (
        type(value["priority"]) is not int
        or value["priority"] < 0
        or value["priority"] > 1000
    ):
        raise LogicalSessionError("session route priority is invalid")
    try:
        logical_model = validate_model_id(
            value["logicalModel"], "logical session model"
        )
    except RoutingError as failure:
        raise LogicalSessionError("session route model is invalid") from failure
    return Route(
        account_id=account_id,
        provider=_identifier(value["provider"], "session route provider"),
        family=_identifier(value["family"], "session route family"),
        logical_model=logical_model,
        upstream_model=upstream,
        claudex_profile=profile,
        priority=value["priority"],
        pool=_identifier(value["pool"], "session route pool"),
    )


def _binding_json(binding: RouteBinding) -> dict[str, object]:
    return {
        "primary": _route_json(binding.primary),
        "fallbacks": [_route_json(route) for route in binding.fallbacks],
    }


def _parse_binding(value: object) -> RouteBinding:
    if not isinstance(value, dict) or set(value) != {"primary", "fallbacks"}:
        raise LogicalSessionError("route binding has invalid fields")
    raw_fallbacks = value["fallbacks"]
    if not isinstance(raw_fallbacks, list):
        raise LogicalSessionError("route fallbacks must be an array")
    primary = _parse_route(value["primary"])
    fallbacks = tuple(_parse_route(route) for route in raw_fallbacks)
    if len(fallbacks) > 1:
        raise LogicalSessionError("route binding permits at most one fallback")
    routes = (primary, *fallbacks)
    if any(
        route.family != primary.family
        or route.logical_model != primary.logical_model
        for route in fallbacks
    ):
        raise LogicalSessionError("route fallbacks must remain in family and model")
    if len({route.account_id for route in routes}) != len(routes):
        raise LogicalSessionError("route binding account IDs must be unique")
    if len({route.upstream_model for route in routes}) != len(routes):
        raise LogicalSessionError("route binding upstream models must be unique")
    return RouteBinding(primary=primary, fallbacks=fallbacks)


def _session_json(session: LogicalSession) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "id": session.id,
        "claudeSessionId": session.claude_session_id,
        "parentId": session.parent_id,
        "projectRoot": str(session.project_root),
        "stack": session.stack,
        "controller": _binding_json(session.controller),
        "agents": {
            role: _binding_json(session.agents[role]) for role in ROLES
        },
        "createdAt": session.created_at,
    }


def _parse_session(value: object) -> LogicalSession:
    keys = {
        "schemaVersion",
        "id",
        "claudeSessionId",
        "parentId",
        "projectRoot",
        "stack",
        "controller",
        "agents",
        "createdAt",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise LogicalSessionError("logical session has invalid fields")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise LogicalSessionError("logical session schemaVersion must be exactly 1")
    identifier = value["id"]
    parent = value["parentId"]
    if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
        raise LogicalSessionError("logical session ID is invalid")
    if parent is not None and (
        not isinstance(parent, str) or not _SESSION_ID.fullmatch(parent)
    ):
        raise LogicalSessionError("logical session parent is invalid")
    try:
        parsed_uuid = uuid.UUID(value["claudeSessionId"])
    except (AttributeError, TypeError, ValueError) as failure:
        raise LogicalSessionError("Claude session ID is invalid") from failure
    if parsed_uuid.version != 4 or str(parsed_uuid) != value["claudeSessionId"]:
        raise LogicalSessionError("Claude session ID must be canonical UUID v4")
    claude_id = str(parsed_uuid)
    raw_root = value["projectRoot"]
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise LogicalSessionError("logical session project root is invalid")
    project_root = Path(raw_root).resolve(strict=False)
    if str(project_root) != raw_root:
        raise LogicalSessionError("logical session project root is not canonical")
    try:
        stack = validate_stack_name(value["stack"], "logical session stack")
    except RoutingError as failure:
        raise LogicalSessionError("logical session stack is invalid") from failure
    agents = value["agents"]
    if not isinstance(agents, dict) or set(agents) != set(ROLES):
        raise LogicalSessionError("logical session agents are invalid")
    created_at = value["createdAt"]
    try:
        parsed_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as failure:
        raise LogicalSessionError("logical session timestamp is invalid") from failure
    if parsed_time.tzinfo != timezone.utc:
        raise LogicalSessionError("logical session timestamp must be UTC")
    return LogicalSession(
        id=identifier,
        claude_session_id=claude_id,
        parent_id=parent,
        project_root=project_root,
        stack=stack,
        controller=_parse_binding(value["controller"]),
        agents=MappingProxyType(
            {role: _parse_binding(agents[role]) for role in ROLES}
        ),
        created_at=created_at,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LogicalSessionError(f"duplicate session field {key!r}")
        result[key] = value
    return result


def _read_binding(directory: Path) -> bytes:
    path = directory / "binding.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as failure:
        raise LogicalSessionError("logical session binding is unavailable") from failure
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LogicalSessionError("logical session binding is unsafe")
        content = os.read(descriptor, MAX_BINDING_BYTES + 1)
        if len(content) > MAX_BINDING_BYTES or os.read(descriptor, 1):
            raise LogicalSessionError("logical session binding is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
        ):
            raise LogicalSessionError("logical session binding changed while reading")
        return content
    finally:
        os.close(descriptor)


def _decode_binding(content: bytes) -> LogicalSession:
    def reject_constant(value: str) -> object:
        raise LogicalSessionError(f"non-finite session value {value}")

    try:
        raw = json.loads(
            content.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as failure:
        raise LogicalSessionError("logical session binding is invalid JSON") from failure
    return _parse_session(raw)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("logical session write made no progress")
        offset += written


def resolve_session_plan(
    config: Mapping[str, object],
    accounts: Sequence[Account],
    *,
    pools: Sequence[str],
    requested_stack: str | None,
    health: Mapping[str, str],
    selection_ordinal: int,
    bindings: StackBindings | None = None,
    available_models: Collection[str] | None = None,
) -> ResolvedSessionPlan:
    """Resolve and pin every controller/agent route for a new session."""
    try:
        raw_stacks = config["model-stacks"]
        stacks = (
            raw_stacks
            if isinstance(raw_stacks, NormalizedStacks)
            else normalize_model_stacks(raw_stacks)
        )
        provider_document = config["providers"]
        stack_name = requested_stack or stacks.default_stack
        stack = stacks.stacks[stack_name]
    except (KeyError, TypeError, StackDefinitionError) as failure:
        raise LogicalSessionError("session model stack is incomplete") from failure
    try:
        stack_name = validate_stack_name(stack_name, "logical session stack")
    except RoutingError as failure:
        raise LogicalSessionError("session model stack is invalid") from failure
    route_config = {
        "models": stacks.models,
        "providers": provider_document,
    }
    bindings = StackBindings({}) if bindings is None else bindings

    def bind_candidate(
        candidate: StackCandidate, ordinal: int
    ) -> RouteBinding:
        try:
            model = stacks.models[candidate.model]
            locked = bindings.candidate_accounts.get(candidate.id)
            chain = route_chain(
                accounts,
                pools=pools,
                family=model.family,
                logical_model=candidate.model,
                allowed_providers=candidate.providers,
                locked_account_id=locked,
                upstream_by_provider=model.routes,
                config=route_config,
                health=health,
                selection_ordinal=ordinal,
                available_models=available_models,
            )
        except (KeyError, TypeError, RouteError) as failure:
            raise LogicalSessionError(
                f"no safe account route is available for {candidate.model}"
            ) from failure
        return _parse_binding(
            {
                "primary": _route_json(chain[0]),
                "fallbacks": [_route_json(route) for route in chain[1:]],
            }
        )

    controller = None
    controller_failures = []
    for candidate in stack.controller:
        try:
            controller = bind_candidate(candidate, selection_ordinal)
            break
        except LogicalSessionError as failure:
            controller_failures.append(failure)
    if controller is None:
        raise LogicalSessionError(
            "no safe account route is available for controller"
        ) from controller_failures[-1]
    agent_bindings: dict[str, RouteBinding] = {}
    for index, role in enumerate(ROLES, start=1):
        selected = None
        failures = []
        for candidate in stack.agents[role]:
            try:
                selected = bind_candidate(
                    candidate, selection_ordinal + index
                )
                break
            except LogicalSessionError as failure:
                failures.append(failure)
        if selected is None:
            raise LogicalSessionError(
                f"no safe account route is available for role {role}"
            ) from failures[-1]
        agent_bindings[role] = selected
    frozen_agents = MappingProxyType(agent_bindings)
    effective_agents = {
        role: frozen_agents[role].primary.upstream_model for role in ROLES
    }
    effective = EffectiveStack(
        stack_name=stack_name,
        controller=controller.primary.upstream_model,
        candidates={
            role: (effective_agents[role],) for role in ROLES
        },
        agents=effective_agents,
    )
    return ResolvedSessionPlan(
        stack=stack_name,
        controller=controller,
        agents=frozen_agents,
        effective=effective,
    )


def create_logical_session(
    state_home: Path,
    *,
    project_root: Path,
    stack: str,
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    parent_id: str | None = None,
) -> LogicalSession:
    root = _session_root(state_home)
    staging = _staging_root(state_home)
    canonical_project = Path(project_root).resolve(strict=False)
    if parent_id is not None:
        parent = load_logical_session(state_home, parent_id)
        if parent.project_root != canonical_project:
            raise LogicalSessionError(
                "forked logical session must stay in its parent project"
            )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    for _ in range(32):
        identifier = f"oc-s-{secrets.token_hex(8)}"
        session = _parse_session(
            {
                "schemaVersion": 1,
                "id": identifier,
                "claudeSessionId": str(uuid.uuid4()),
                "parentId": parent_id,
                "projectRoot": str(canonical_project),
                "stack": stack,
                "controller": _binding_json(controller),
                "agents": {
                    role: _binding_json(agents[role]) for role in agents
                },
                "createdAt": now,
            }
        )
        directory = staging / identifier
        published = root / identifier
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        except OSError as failure:
            raise LogicalSessionError(
                "logical session could not be allocated"
            ) from failure
        descriptor = -1
        try:
            payload = (
                json.dumps(
                    _session_json(session),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if len(payload) > MAX_BINDING_BYTES:
                raise LogicalSessionError("logical session binding is too large")
            descriptor = os.open(
                directory / "binding.json",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            root_fd = os.open(
                staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            os.rename(directory, published)
            root_fd = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            return load_logical_session(state_home, identifier)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                (directory / "binding.json").unlink()
            except FileNotFoundError:
                pass
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            try:
                (published / "binding.json").unlink()
            except FileNotFoundError:
                pass
            try:
                published.rmdir()
            except FileNotFoundError:
                pass
            raise
    raise LogicalSessionError("could not allocate a logical session ID")


def load_logical_session(state_home: Path, identifier: str) -> LogicalSession:
    if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
        raise LogicalSessionError("logical session selector is invalid")
    root = _session_root(state_home)
    directory = _require_private_directory(
        root / identifier, "logical session"
    )
    if directory.parent != root:
        raise LogicalSessionError("logical session escaped its state directory")
    session = _decode_binding(_read_binding(directory))
    if session.id != identifier:
        raise LogicalSessionError("logical session ID does not match its path")
    return session


def list_logical_sessions(state_home: Path) -> tuple[LogicalSession, ...]:
    root = _session_root(state_home)
    try:
        entries = tuple(os.scandir(root))
    except OSError as failure:
        raise LogicalSessionError("logical sessions could not be listed") from failure
    names = []
    for entry in entries:
        if not _SESSION_ID.fullmatch(entry.name) or not entry.is_dir(
            follow_symlinks=False
        ):
            raise LogicalSessionError(
                "logical session directory contains an unexpected entry"
            )
        names.append(entry.name)
    names.sort()
    sessions = tuple(load_logical_session(state_home, name) for name in names)
    return tuple(sorted(sessions, key=lambda item: (item.created_at, item.id)))
