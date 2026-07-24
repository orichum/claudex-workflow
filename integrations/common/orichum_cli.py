#!/usr/bin/env python3
"""Unified Orichum command dispatcher."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import http.client
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Mapping, Sequence

from .account_registry import (
    Account,
    AccountError,
    account_transaction,
    find_account,
    load_accounts,
    new_account,
    parse_priority,
    update_accounts,
    validate_account_bindings,
)
from .cliproxy_management import (
    ManagementError,
    load_management_endpoint,
    patch_auth_fields,
)
from .github_identity import GithubIdentityError, ensure_github_identity
from .orichum_config import (
    ConfigError,
    ResolvedConfig,
    default_config_paths,
    load_control_plane,
    redact_control_plane,
)
from .orichum_sessions import (
    LogicalSession,
    LogicalSessionError,
    RouteBinding,
    create_logical_session,
    list_logical_sessions,
    load_logical_session,
    resolve_session_plan,
)
from .model_routing import EffectiveStack, ROLES
from .project_context import ContextError, resolve_control_plane_context
from .provider_credentials import (
    CredentialError,
    credential_metadata_transaction,
    resolve_credential_ref,
)
from .route_selection import RouteError, validate_route_credential
from .session_config import (
    SessionError,
    SessionPaths,
    create_resolved_session,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[2]


class CliError(RuntimeError):
    """An Orichum command cannot be completed safely."""


@dataclass(frozen=True)
class PreparedLaunch:
    logical: LogicalSession
    physical: SessionPaths


def _home(
    environment: Mapping[str, str],
    override: str,
    xdg: str,
    fallback: str,
) -> Path:
    raw = environment.get(override)
    if raw is None:
        base = environment.get(xdg)
        raw = str(Path(base) / "orichum") if base else str(Path.home() / fallback)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise CliError(f"{override} must be an absolute path")
    return path.resolve(strict=False)


def _paths(environment: Mapping[str, str] | None = None) -> dict[str, Path]:
    environment = os.environ if environment is None else environment
    data = _home(
        environment, "ORICHUM_DATA_HOME", "XDG_DATA_HOME", ".local/share/orichum"
    )
    return {
        "config": _home(
            environment, "ORICHUM_CONFIG_HOME", "XDG_CONFIG_HOME", ".config/orichum"
        ),
        "data": data,
        "state": data / "state",
        "cache": _home(
            environment, "ORICHUM_CACHE_HOME", "XDG_CACHE_HOME", ".cache/orichum"
        ),
    }


def _load() -> tuple[dict[str, Path], ResolvedConfig]:
    paths = _paths()
    return paths, load_control_plane(default_config_paths(paths["config"]))


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    normalized = [tuple(str(value) for value in row) for row in rows]
    widths = [
        max([len(header), *(len(row[index]) for row in normalized)])
        for index, header in enumerate(headers)
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def row(values: Sequence[str]) -> str:
        return (
            "|"
            + "|".join(
                f" {value:<{width}} "
                for value, width in zip(values, widths, strict=True)
            )
            + "|"
        )

    rendered = [border, row(headers), border]
    rendered.extend(row(values) for values in normalized)
    rendered.append(border)
    return "\n".join(rendered) + "\n"


def _config_show(config: ResolvedConfig) -> dict[str, object]:
    redacted = redact_control_plane(config)
    return {
        name: {"source": config.sources[name], "value": redacted[name]}
        for name in sorted(redacted)
    }


def _context_list(config: ResolvedConfig) -> str:
    contexts = config.documents["projects"]["contexts"]
    rows = [
        (
            context["root"],
            context["dockerProfile"] or "—",
            context.get("githubAccount") or "—",
            context["modelStack"] or "default",
            ", ".join(context["accountPools"]),
            context["memoryPalace"],
            context["memoryWing"],
        )
        for context in contexts
    ]
    return _render_table(
        (
            "ROOT",
            "DOCKER",
            "GITHUB",
            "MODEL STACK",
            "ACCOUNT POOLS",
            "PALACE",
            "WING",
        ),
        rows,
    )


def _model_list(config: ResolvedConfig) -> str:
    models = config.documents["model-stacks"]["models"]
    rows = [
        (
            model,
            metadata["provider"],
            metadata["family"],
            metadata["upstream"],
        )
        for model, metadata in sorted(models.items())
    ]
    return _render_table(("MODEL", "PROVIDER", "FAMILY", "UPSTREAM"), rows)


def _resolve_stack(config: ResolvedConfig, requested: str | None) -> dict[str, object]:
    document = config.documents["model-stacks"]
    stack_name = requested or document["defaultStack"]
    try:
        stack = document["stacks"][stack_name]
    except KeyError as error:
        raise CliError(f"model stack is not configured: {stack_name}") from error
    return {
        "stack": stack_name,
        "controller": stack["controller"],
        "configuredCandidates": stack["agents"],
        "agents": {
            role: candidates[0] for role, candidates in stack["agents"].items()
        },
    }


def _provider_list(config: ResolvedConfig) -> str:
    providers = config.documents["providers"]["providers"]
    rows = [
        (
            provider,
            details["type"],
            details["transport"],
            ", ".join(details["families"]),
        )
        for provider, details in sorted(providers.items())
    ]
    return _render_table(("PROVIDER", "ADAPTER", "TRANSPORT", "FAMILIES"), rows)


def _account_list(accounts: Sequence[Account]) -> str:
    rows = [
        (
            account.id,
            account.name,
            account.provider,
            account.pool,
            str(account.priority),
            account.state.upper(),
        )
        for account in sorted(accounts, key=lambda item: (item.pool, -item.priority, item.name))
    ]
    return _render_table(
        ("ID", "NAME", "PROVIDER", "POOL", "PRIORITY", "STATE"), rows
    )


def _session_list(sessions: Sequence[LogicalSession]) -> str:
    rows = [
        (
            session.id,
            session.created_at,
            str(session.project_root),
            session.stack,
            session.controller.primary.family,
            session.controller.primary.logical_model,
            session.parent_id or "—",
        )
        for session in sessions
    ]
    return _render_table(
        ("ID", "CREATED", "PROJECT", "STACK", "FAMILY", "MODEL", "PARENT"),
        rows,
    )


def _session_routes(
    session: LogicalSession, accounts: Sequence[Account]
) -> str:
    names = {account.id: account.name for account in accounts}
    bindings = (
        ("controller", session.controller),
        *((role, session.agents[role]) for role in ROLES),
    )
    rows = []
    for role, binding in bindings:
        primary = binding.primary
        fallback = (
            f"{names.get(route.account_id, '<unavailable>')} ({route.provider})"
            for route in binding.fallbacks[:1]
        )
        fallback_name = next(fallback, "—")
        rows.append(
            (
                role,
                primary.logical_model,
                primary.provider,
                names.get(primary.account_id, "<unavailable>"),
                fallback_name,
            )
        )
    return _render_table(
        ("ROLE", "MODEL", "PROVIDER", "PRIMARY ACCOUNT", "FALLBACK"),
        rows,
    )


def _effective_for(session: LogicalSession) -> EffectiveStack:
    agents = {
        role: session.agents[role].primary.upstream_model for role in ROLES
    }
    return EffectiveStack(
        stack_name=session.stack,
        controller=session.controller.primary.upstream_model,
        candidates={role: (agents[role],) for role in ROLES},
        agents=agents,
    )


def _validate_session_routes(
    session: LogicalSession,
    accounts: Sequence[Account],
    *,
    auth_dir: Path,
    provider_document: Mapping[str, object],
) -> None:
    _validate_plan_routes(
        session.controller,
        session.agents,
        accounts,
        auth_dir=auth_dir,
        provider_document=provider_document,
    )


def _validate_plan_routes(
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    accounts: Sequence[Account],
    *,
    auth_dir: Path,
    provider_document: Mapping[str, object],
) -> None:
    bindings = (controller, *(agents[role] for role in ROLES))
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        for route in (binding.primary, *binding.fallbacks):
            key = (route.account_id, route.logical_model)
            if key in seen:
                continue
            validate_route_credential(
                route,
                accounts,
                auth_dir=auth_dir,
                provider_document=provider_document,
            )
            seen.add(key)


def _verify_runtime(paths: Mapping[str, Path]) -> None:
    verifier = WORKFLOW_ROOT / "bin" / "orichum-runtime-ready"
    if not verifier.is_file() or verifier.is_symlink():
        raise CliError("Orichum runtime verifier is unavailable")
    completed = subprocess.run(
        [str(verifier), str(paths["data"])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        try:
            accounts = load_accounts(paths["config"] / "accounts.json")
        except AccountError:
            accounts = ()
        if not accounts:
            raise CliError(
                "no provider account is registered; run "
                "orichum provider login <provider>, add the named account, "
                "then re-run install.sh"
            )
        raise CliError("Orichum services are not owned and ready; run install.sh")


def _live_models(paths: Mapping[str, Path]) -> frozenset[str]:
    try:
        ports = json.loads(
            _read_stable_file(
                paths["data"] / "service-ports.json",
                "service port state",
                64 * 1024,
            )
        )
        route_port = ports["routeProxyPort"]
        if (
            type(route_port) is not int
            or route_port < 1024
            or route_port > 65535
        ):
            raise ValueError
        connection = http.client.HTTPConnection(
            "127.0.0.1", route_port, timeout=3
        )
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        payload = response.read(2 * 1024 * 1024 + 1)
        connection.close()
        document = json.loads(payload)
        available = frozenset(
            item["id"]
            for item in document["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
        OSError,
    ) as error:
        raise CliError("live Orichum model catalogue is unavailable") from error
    if response.status != 200 or len(payload) > 2 * 1024 * 1024:
        raise CliError("live Orichum model catalogue is unavailable")
    return available


def _headroom_status(paths: Mapping[str, Path]) -> str:
    try:
        ports = json.loads(
            _read_stable_file(
                paths["data"] / "service-ports.json",
                "service port state",
                64 * 1024,
            )
        )
        port = ports["headroomPort"]
        if type(port) is not int or port < 1024 or port > 65535:
            raise ValueError
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = response.read(256 * 1024 + 1)
        connection.close()
        document = json.loads(payload)
        version = document["version"]
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
        OSError,
    ) as error:
        raise CliError("Headroom health is unavailable") from error
    if (
        response.status != 200
        or len(payload) > 256 * 1024
        or document.get("service") != "headroom-proxy"
        or document.get("status") != "healthy"
        or document.get("ready") is not True
        or not isinstance(version, str)
        or not version
    ):
        raise CliError("Headroom is not healthy and ready")
    return (
        f"Headroom {version}: healthy and ready at "
        f"http://127.0.0.1:{port}\n"
    )


def _validate_live_models(
    paths: Mapping[str, Path],
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    *,
    available: frozenset[str] | None = None,
) -> None:
    if available is None:
        available = _live_models(paths)
    bindings = (controller, *(agents[role] for role in ROLES))
    required = {
        route.upstream_model
        for binding in bindings
        for route in (binding.primary, *binding.fallbacks)
    }
    missing = sorted(required - available)
    if missing:
        raise CliError(
            "bound model routes are not live: " + ", ".join(missing)
        )


def _prepare_new_session(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    launch_dir: Path,
) -> PreparedLaunch:
    _verify_runtime(paths)
    context = resolve_control_plane_context(
        config.documents["projects"], launch_dir
    )
    route = context.get("route")
    if not isinstance(route, dict):
        raise CliError("launch directory is not mapped to an Orichum project")
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    available = _live_models(paths)
    ordinal = int.from_bytes(os.urandom(8), "big")
    plan = resolve_session_plan(
        config.documents,
        accounts,
        pools=tuple(route["accountPools"]),
        requested_stack=route["modelStack"],
        health={},
        selection_ordinal=ordinal,
        available_models=available,
    )
    _validate_plan_routes(
        controller=plan.controller,
        agents=plan.agents,
        accounts=accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(
        paths, plan.controller, plan.agents, available=available
    )
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=plan.effective,
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    logical = create_logical_session(
        paths["state"],
        project_root=Path(route["contextRootReal"]),
        stack=plan.stack,
        controller=plan.controller,
        agents=plan.agents,
    )
    return PreparedLaunch(logical, physical)


def _prepare_resume(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    identifier: str,
    launch_dir: Path,
) -> PreparedLaunch:
    _verify_runtime(paths)
    logical = load_logical_session(paths["state"], identifier)
    context = resolve_control_plane_context(
        config.documents["projects"], launch_dir
    )
    route = context.get("route")
    if (
        not isinstance(route, dict)
        or Path(route["contextRootReal"]) != logical.project_root
    ):
        raise CliError("resume must be launched inside the session project")
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    _validate_session_routes(
        logical,
        accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(paths, logical.controller, logical.agents)
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=_effective_for(logical),
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    return PreparedLaunch(logical, physical)


def _read_handoff(path: Path) -> str:
    path = Path(path)
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise CliError("handoff file is unavailable") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_size < 1
        or observed.st_size > 16 * 1024
    ):
        raise CliError("handoff file is unsafe or exceeds 16 KiB")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
            or opened.st_mtime_ns != observed.st_mtime_ns
        ):
            raise CliError("handoff file changed while opening")
        content = os.read(descriptor, 16 * 1024 + 1)
        if len(content) > 16 * 1024 or os.read(descriptor, 1):
            raise CliError("handoff file exceeds 16 KiB")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise CliError("handoff file changed while reading")
    finally:
        os.close(descriptor)
    try:
        handoff = content.decode("utf-8").strip()
    except UnicodeError as error:
        raise CliError("handoff file must be UTF-8") from error
    if not handoff or "\x00" in handoff:
        raise CliError("handoff file is empty or invalid")
    return handoff


def _prepare_fork(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    identifier: str,
    launch_dir: Path,
    requested_stack: str | None,
    handoff_file: Path | None,
) -> tuple[PreparedLaunch, str]:
    _verify_runtime(paths)
    parent = load_logical_session(paths["state"], identifier)
    context = resolve_control_plane_context(
        config.documents["projects"], launch_dir
    )
    route = context.get("route")
    if (
        not isinstance(route, dict)
        or Path(route["contextRootReal"]) != parent.project_root
    ):
        raise CliError("fork must be launched inside the session project")
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    if requested_stack is None:
        controller = parent.controller
        agents = parent.agents
        stack = parent.stack
        effective = _effective_for(parent)
    else:
        available = _live_models(paths)
        plan = resolve_session_plan(
            config.documents,
            accounts,
            pools=tuple(route["accountPools"]),
            requested_stack=requested_stack,
            health={},
            selection_ordinal=int.from_bytes(os.urandom(8), "big"),
            available_models=available,
        )
        controller = plan.controller
        agents = plan.agents
        stack = plan.stack
        effective = plan.effective
    family_changed = (
        controller.primary.family != parent.controller.primary.family
    )
    if family_changed and handoff_file is None:
        raise CliError("cross-family fork requires --handoff-file")
    handoff = (
        _read_handoff(handoff_file)
        if handoff_file is not None
        else (
            f"Explicit fork of Orichum session {parent.id}. Reconstruct active "
            "work from the repository state and the user's next message; do "
            "not assume access to the parent transcript."
        )
    )
    _validate_plan_routes(
        controller=controller,
        agents=agents,
        accounts=accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(paths, controller, agents)
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=effective,
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    logical = create_logical_session(
        paths["state"],
        project_root=parent.project_root,
        stack=stack,
        controller=controller,
        agents=agents,
        parent_id=parent.id,
    )
    return PreparedLaunch(logical, physical), handoff


def _replace_account(
    accounts: tuple[Account, ...],
    selector: str,
    **changes: object,
) -> tuple[Account, ...]:
    selected = find_account(accounts, selector)
    return tuple(
        replace(account, **changes) if account.id == selected.id else account
        for account in accounts
    )


def _mutate_account(
    parsed: argparse.Namespace,
    paths: Mapping[str, Path],
    config: ResolvedConfig,
) -> None:
    registry = paths["config"] / "accounts.json"
    provider_document = config.documents["providers"]
    auth_dir = paths["data"] / "auth"
    management_endpoint = None

    def management():
        nonlocal management_endpoint
        if management_endpoint is None:
            management_endpoint = load_management_endpoint(paths["data"])
        return management_endpoint

    def validated(
        accounts: Sequence[Account],
    ) -> tuple[Account, ...]:
        result = tuple(accounts)
        validate_account_bindings(result, provider_document)
        return result

    def credential_for(account: Account):
        try:
            provider = provider_document["providers"][account.provider]
            expected_type = provider["authType"]
        except (KeyError, TypeError) as error:
            raise CliError("account provider configuration is incomplete") from error
        return resolve_credential_ref(
            auth_dir,
            account.credential_ref,
            expected_provider=expected_type,
        )

    def publish(account: Account) -> None:
        before = credential_for(account)
        if before.disabled:
            raise CliError("credential is disabled in CLIProxyAPI")
        patch_auth_fields(
            management(),
            account.credential_ref,
            {"prefix": account.routing_prefix, "priority": account.priority},
        )
        after = credential_for(account)
        if (
            after.provider != before.provider
            or after.disabled
            or after.prefix != account.routing_prefix
            or after.priority != account.priority
        ):
            raise CliError("CLIProxyAPI credential publication was not verified")

    def unpublish(account: Account) -> None:
        before = credential_for(account)
        restore_prefix = account.original_prefix or ""
        restore_priority = (
            account.original_priority
            if account.original_priority is not None
            else 0
        )
        patch_auth_fields(
            management(),
            account.credential_ref,
            {"prefix": restore_prefix, "priority": restore_priority},
        )
        if (
            account.original_prefix is None
            or account.original_priority is None
        ):
            patch_auth_fields(
                management(),
                account.credential_ref,
                {
                    "prefix": account.original_prefix,
                    "priority": account.original_priority,
                },
            )
        after = credential_for(account)
        if (
            after.provider != before.provider
            or after.disabled != before.disabled
            or after.prefix != account.original_prefix
            or after.priority != account.original_priority
        ):
            raise CliError("CLIProxyAPI credential restoration was not verified")

    def synchronize(account: Account) -> None:
        if account.state == "pending-add":
            publish(account)
            update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(accounts, account.id, state="active")
                ),
            )
        elif account.state == "pending-remove":
            unpublish(account)
            update_accounts(
                registry,
                lambda accounts: validated(
                    tuple(item for item in accounts if item.id != account.id)
                ),
            )

    action = parsed.account_command
    with account_transaction(registry), credential_metadata_transaction(auth_dir):
        if action == "add":
            providers = provider_document["providers"]
            pools = provider_document["accountPools"]
            provider = providers.get(parsed.provider)
            pool = pools.get(parsed.pool)
            if not isinstance(provider, dict):
                raise CliError(f"provider is not configured: {parsed.provider}")
            if (
                not isinstance(pool, dict)
                or parsed.provider not in pool.get("providers", ())
            ):
                raise CliError(
                    f"provider {parsed.provider} is not authorized by pool {parsed.pool}"
                )
            credential = resolve_credential_ref(
                auth_dir,
                parsed.credential_ref,
                expected_provider=provider["authType"],
            )
            if credential.disabled:
                raise CliError("credential is disabled in CLIProxyAPI")
            priority = parse_priority(parsed.priority)
            created: list[Account] = []

            def add(accounts: tuple[Account, ...]) -> tuple[Account, ...]:
                if any(
                    account.credential_ref == parsed.credential_ref
                    for account in accounts
                ):
                    raise AccountError(
                        "credential reference is already assigned to an account"
                    )
                account = new_account(
                    name=parsed.name,
                    provider=parsed.provider,
                    credential_ref=parsed.credential_ref,
                    pool=parsed.pool,
                    priority=priority,
                    existing=accounts,
                    state="pending-add",
                    original_prefix=credential.prefix,
                    original_priority=credential.priority,
                )
                proposed = validated((*accounts, account))
                created.append(account)
                return proposed

            update_accounts(registry, add)
            account = created[0]
            synchronize(account)
        elif action == "remove":
            current = find_account(load_accounts(registry), parsed.selector)
            if current.state == "pending-remove":
                synchronize(current)
                return
            updated = update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(
                        accounts, parsed.selector, state="pending-remove"
                    )
                ),
            )
            synchronize(find_account(updated, parsed.selector))
        elif action == "sync":
            accounts = load_accounts(registry)
            selected = (
                (find_account(accounts, parsed.selector),)
                if parsed.selector
                else accounts
            )
            for account in selected:
                synchronize(account)
        else:
            changes: dict[str, object]
            if action == "rename":
                changes = {"name": parsed.name}
            elif action == "priority":
                priority = parse_priority(parsed.priority)
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "disabled":
                    update_accounts(
                        registry,
                        lambda accounts: validated(
                            _replace_account(
                                accounts,
                                parsed.selector,
                                priority=priority,
                            )
                        ),
                    )
                    return
                if current.state != "active":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                updated = update_accounts(
                    registry,
                    lambda accounts: validated(
                        _replace_account(
                            accounts,
                            parsed.selector,
                            priority=priority,
                            state="pending-add",
                        )
                    ),
                )
                synchronize(find_account(updated, parsed.selector))
                return
            elif action == "enable":
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "active":
                    return
                if current.state != "disabled":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                updated = update_accounts(
                    registry,
                    lambda accounts: validated(
                        _replace_account(
                            accounts, parsed.selector, state="pending-add"
                        )
                    ),
                )
                synchronize(find_account(updated, parsed.selector))
                return
            elif action == "disable":
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "disabled":
                    return
                if current.state != "active":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                changes = {"state": "disabled"}
            else:
                raise AssertionError("unreachable account action")
            update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(accounts, parsed.selector, **changes)
                ),
            )


def _run_external(name: str, arguments: list[str]) -> int:
    candidate = WORKFLOW_ROOT / "bin" / name
    executable = str(candidate) if candidate.is_file() else shutil.which(name)
    if executable is None:
        raise CliError(f"required command is not installed: {name}")
    completed = subprocess.run(
        [executable, *arguments],
        check=False,
        cwd=WORKFLOW_ROOT,
        env=os.environ.copy(),
    )
    return completed.returncode


_OWNED_CLAUDE_OPTIONS = (
    "--agents",
    "--effort",
    "--model",
    "--fallback-model",
    "--config",
    "--plugin-dir",
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--system-prompt",
    "--system-prompt-file",
    "--agent",
    "--settings",
    "--mcp-config",
    "--strict-mcp-config",
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--session-id",
    "--resume",
    "--continue",
    "--fork-session",
    "--from-pr",
    "--no-session-persistence",
    "--safe-mode",
)
_OWNED_CLAUDE_SHORT_OPTIONS = ("-c", "-r")


def _validate_user_claude_arguments(arguments: Sequence[str]) -> list[str]:
    result = list(arguments)
    if result and result[0] == "--":
        result.pop(0)
    for argument in result:
        if any(
            argument == owned or argument.startswith(f"{owned}=")
            for owned in _OWNED_CLAUDE_OPTIONS
        ) or any(
            argument == owned or argument.startswith(owned)
            for owned in _OWNED_CLAUDE_SHORT_OPTIONS
        ):
            raise CliError(f"Orichum owns Claude option: {argument}")
    return result


def _read_stable_file(path: Path, label: str, maximum: int) -> bytes:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_size > maximum
    ):
        raise CliError(f"{label} is unsafe")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
        ):
            raise CliError(f"{label} changed while opening")
        content = os.read(descriptor, maximum + 1)
        if len(content) > maximum or os.read(descriptor, 1):
            raise CliError(f"{label} is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise CliError(f"{label} changed while reading")
        return content
    finally:
        os.close(descriptor)


def _materialize_session_claudex_config(
    source: Path, prepared: PreparedLaunch
) -> Path:
    content = _read_stable_file(source, "Claudex configuration", 1024 * 1024)
    marker = b'X-Orichum-Session-ID = "unbound"'
    if content.count(marker) != 1:
        raise CliError("Claudex configuration lacks the Orichum session marker")
    content = content.replace(
        marker,
        f'X-Orichum-Session-ID = "{prepared.logical.id}"'.encode("ascii"),
    )
    output = prepared.physical.run_dir / "claudex.toml"
    descriptor = os.open(
        output,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise CliError("session Claudex configuration write stalled")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return output


def _github_config_for_session(
    paths: Mapping[str, Path], physical: SessionPaths
) -> Path | None:
    try:
        physical_context = json.loads(
            _read_stable_file(
                physical.context_file,
                "session project context",
                2 * 1024 * 1024,
            )
        )
        physical_route = physical_context.get("route")
        github_account = (
            physical_route.get("githubAccount")
            if isinstance(physical_route, dict)
            else None
        )
        return (
            ensure_github_identity(paths["data"], github_account)
            if github_account is not None
            else None
        )
    except (
        GithubIdentityError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise CliError("project GitHub identity is unavailable") from error


def _session_environment(
    prepared: PreparedLaunch,
    paths: Mapping[str, Path],
    runtime: Mapping[str, object],
    github_config: Path | None,
    claudex_config: Path,
) -> dict[str, str]:
    physical = prepared.physical
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CLAUDEX_")
    }
    for key in (
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_DISABLE_WORKFLOWS",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        environment.pop(key, None)
    if github_config is not None:
        for key in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST",
        ):
            environment.pop(key, None)
    environment.update(
        {
            "ORICHUM_SESSION_ID": prepared.logical.id,
            "ORICHUM_STATE_HOME": str(paths["state"]),
            "ORICHUM_CONFIG_HOME": str(paths["config"]),
            "ORICHUM_DATA_HOME": str(paths["data"]),
            "CLAUDEX_CONFIG_FILE": str(claudex_config),
            "CLAUDEX_MCP_CONFIG": str(physical.mcp_file),
            "CLAUDEX_RUN_DIR": str(physical.run_dir),
            "CLAUDEX_CONTEXT_FILE": str(physical.context_file),
            "CLAUDEX_CONTEXT_SHA256": physical.context_sha256,
            "CLAUDEX_EFFECTIVE_MODELS_FILE": str(
                physical.effective_models_file
            ),
            "CLAUDEX_RUN_ID": physical.run_id,
            "CLAUDEX_WORKFLOW_ROOT": str(WORKFLOW_ROOT),
            "CLAUDEX_DATA_DIR": str(paths["data"]),
            "CLAUDE_CONFIG_DIR": str(paths["data"] / "claude-config"),
            "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": str(
                runtime["maxToolUseConcurrency"]
            ),
            "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": str(
                runtime["maxSubagentsPerSession"]
            ),
            "CLAUDE_CODE_MAX_RETRIES": "2",
            "ENABLE_TOOL_SEARCH": "true",
            "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
            "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        }
    )
    if github_config is not None:
        environment["GH_CONFIG_DIR"] = str(github_config)
    return environment


def _launch_session(
    prepared: PreparedLaunch,
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    resume: bool,
    arguments: Sequence[str],
    handoff: str | None = None,
) -> None:
    user_arguments = _validate_user_claude_arguments(arguments)
    claudex = paths["data"] / "bin" / "claudex"
    shared_claudex_config = (
        paths["data"] / "model-config" / "current" / "claudex.toml"
    )
    policy = paths["config"] / "controller-policy.md"
    for path, label in (
        (claudex, "Claudex runtime"),
        (shared_claudex_config, "Claudex configuration"),
        (policy, "controller policy"),
    ):
        if not path.is_file() or path.is_symlink():
            raise CliError(f"{label} is unavailable")
    claudex_config = _materialize_session_claudex_config(
        shared_claudex_config, prepared
    )
    runtime = config.documents["runtime"]["controller"]
    physical = prepared.physical
    github_config = _github_config_for_session(paths, physical)
    environment = _session_environment(
        prepared,
        paths,
        runtime,
        github_config,
        claudex_config,
    )
    launch_policy = policy
    if handoff is not None:
        try:
            policy_bytes = _read_stable_file(
                policy, "controller policy", 1024 * 1024
            )
        except OSError as error:
            raise CliError("controller policy could not be read") from error
        launch_policy = physical.run_dir / "launch-policy.md"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(launch_policy, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            payload = (
                policy_bytes
                + b"\n\n## Explicit session handoff\n\n"
                + handoff.encode("utf-8")
                + b"\n"
            )
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    command = [
        str(claudex),
        "--config",
        str(claudex_config),
        "run",
        "gpt",
        "--model",
        physical.controller_model,
        "--mcp-config",
        str(physical.mcp_file),
        "--strict-mcp-config",
        "--effort",
        runtime["effort"],
        "--append-system-prompt-file",
        str(launch_policy),
        "--plugin-dir",
        str(physical.plugin_dir),
    ]
    if resume:
        command.extend(["--resume", prepared.logical.claude_session_id])
    else:
        command.extend(["--session-id", prepared.logical.claude_session_id])
    command.extend(user_arguments)
    os.execvpe(str(claudex), command, environment)


def _deferred(label: str) -> int:
    print(f"ERROR: {label} not yet installed", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orichum")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run")
    run.add_argument("arguments", nargs=argparse.REMAINDER)

    config = commands.add_parser("config")
    config_action = config.add_subparsers(dest="config_command", required=True)
    for name in ("show", "validate", "paths"):
        config_action.add_parser(name)

    context = commands.add_parser("context")
    context_action = context.add_subparsers(dest="context_command", required=True)
    context_action.add_parser("list")
    context_action.add_parser("validate")
    for name in ("add", "populate", "remove", "update"):
        command = context_action.add_parser(name)
        command.add_argument("arguments", nargs=argparse.REMAINDER)

    models = commands.add_parser("models")
    model_action = models.add_subparsers(dest="models_command", required=True)
    model_action.add_parser("list")
    model_action.add_parser("validate")
    resolve = model_action.add_parser("resolve")
    resolve.add_argument("stack", nargs="?")

    provider = commands.add_parser("provider")
    provider_action = provider.add_subparsers(
        dest="provider_command", required=True
    )
    provider_action.add_parser("list")
    login = provider_action.add_parser("login")
    login.add_argument("arguments", nargs=argparse.REMAINDER)
    provider_action.add_parser("accounts")
    account = provider_action.add_parser("account")
    account_action = account.add_subparsers(
        dest="account_command", required=True
    )
    add = account_action.add_parser("add")
    add.add_argument("name")
    add.add_argument("provider")
    add.add_argument("credential_ref")
    add.add_argument("pool")
    add.add_argument("--priority", default="primary")
    rename = account_action.add_parser("rename")
    rename.add_argument("selector")
    rename.add_argument("name")
    priority = account_action.add_parser("priority")
    priority.add_argument("selector")
    priority.add_argument("priority")
    for name in ("enable", "disable", "remove"):
        command = account_action.add_parser(name)
        command.add_argument("selector")
    sync = account_action.add_parser("sync")
    sync.add_argument("selector", nargs="?")

    plugin = commands.add_parser("plugin")
    plugin_action = plugin.add_subparsers(dest="plugin_command", required=True)
    plugin_action.add_parser("list")
    for name in ("add", "remove", "sync", "update"):
        command = plugin_action.add_parser(name)
        command.add_argument("arguments", nargs=argparse.REMAINDER)

    headroom = commands.add_parser("headroom")
    headroom.add_subparsers(dest="headroom_command", required=True).add_parser(
        "status"
    )
    commands.add_parser("doctor")
    sessions = commands.add_parser("sessions")
    sessions_action = sessions.add_subparsers(dest="sessions_command")
    sessions_routes = sessions_action.add_parser("routes")
    sessions_routes.add_argument("session_id")
    session = commands.add_parser("session")
    session_action = session.add_subparsers(
        dest="session_command", required=True
    )
    session_routes = session_action.add_parser("routes")
    session_routes.add_argument("session_id")
    resume = commands.add_parser("resume")
    resume.add_argument("session_id")
    resume.add_argument("arguments", nargs=argparse.REMAINDER)
    fork = commands.add_parser("fork")
    fork.add_argument("session_id")
    fork.add_argument("--stack")
    fork.add_argument("--handoff-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    try:
        if parsed.command is None:
            raise CliError("run Orichum through the installed launcher")
        if parsed.command == "headroom":
            print(_headroom_status(_paths()), end="")
            return 0
        if parsed.command == "doctor":
            return _run_external("orichum-doctor", [])
        if parsed.command == "config" and parsed.config_command == "paths":
            paths = _paths()
            print(
                json.dumps(
                    {name: str(path) for name, path in paths.items()},
                    sort_keys=True,
                )
            )
            return 0
        if parsed.command == "context" and parsed.context_command != "list":
            context_arguments = [parsed.context_command]
            context_arguments.extend(getattr(parsed, "arguments", ()))
            return _run_external("orichum-context", context_arguments)
        paths, config = _load()
        if parsed.command == "provider" and parsed.provider_command == "login":
            return _run_external("orichum-login", list(parsed.arguments))
        if parsed.command == "plugin":
            plugin_arguments = [parsed.plugin_command]
            plugin_arguments.extend(getattr(parsed, "arguments", ()))
            return _run_external("orichum-plugin", plugin_arguments)
        if parsed.command == "run":
            prepared = _prepare_new_session(
                paths, config, launch_dir=Path.cwd()
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=False,
                arguments=parsed.arguments,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command in {"session", "sessions"}:
            route_request = (
                parsed.command == "session"
                or parsed.sessions_command == "routes"
            )
            if route_request:
                logical = load_logical_session(
                    paths["state"], parsed.session_id
                )
                accounts = load_accounts(
                    paths["config"] / "accounts.json"
                )
                validate_account_bindings(
                    accounts, config.documents["providers"]
                )
                print(_session_routes(logical, accounts), end="")
            else:
                print(
                    _session_list(list_logical_sessions(paths["state"])),
                    end="",
                )
            return 0
        if parsed.command == "resume":
            prepared = _prepare_resume(
                paths,
                config,
                identifier=parsed.session_id,
                launch_dir=Path.cwd(),
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=True,
                arguments=parsed.arguments,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command == "fork":
            prepared, handoff = _prepare_fork(
                paths,
                config,
                identifier=parsed.session_id,
                launch_dir=Path.cwd(),
                requested_stack=parsed.stack,
                handoff_file=parsed.handoff_file,
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=False,
                arguments=(),
                handoff=handoff,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command == "config":
            if parsed.config_command == "validate":
                return 0
            print(json.dumps(_config_show(config), indent=2, sort_keys=True))
            return 0
        if parsed.command == "context":
            print(_context_list(config), end="")
            return 0
        if parsed.command == "models":
            if parsed.models_command == "list":
                print(_model_list(config), end="")
            elif parsed.models_command == "resolve":
                print(
                    json.dumps(
                        _resolve_stack(config, parsed.stack),
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0
        if parsed.command == "provider":
            if parsed.provider_command == "list":
                print(_provider_list(config), end="")
            elif parsed.provider_command == "accounts":
                accounts = load_accounts(paths["config"] / "accounts.json")
                validate_account_bindings(
                    accounts, config.documents["providers"]
                )
                print(_account_list(accounts), end="")
            else:
                _mutate_account(parsed, paths, config)
            return 0
    except (
        AccountError,
        CliError,
        ConfigError,
        ContextError,
        CredentialError,
        LogicalSessionError,
        ManagementError,
        OSError,
        RouteError,
        SessionError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
