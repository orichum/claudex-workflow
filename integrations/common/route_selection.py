#!/usr/bin/env python3
"""Deterministic Orichum account and provider route selection."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Collection, Mapping, Sequence

from pathlib import Path

from .account_registry import Account, find_account
from .provider_credentials import resolve_credential_ref


class RouteError(RuntimeError):
    """No safe configured account route is available."""


@dataclass(frozen=True)
class Route:
    account_id: str
    provider: str
    family: str
    logical_model: str
    upstream_model: str
    claudex_profile: str
    priority: int
    pool: str


def eligible_routes(
    accounts: Sequence[Account],
    *,
    pool: str,
    family: str,
    logical_model: str,
    config: Mapping[str, object],
    allowed_providers: Collection[str],
    locked_account_id: str | None,
    upstream_by_provider: Mapping[str, str],
    available_models: Collection[str] | None = None,
) -> tuple[Route, ...]:
    try:
        models = config.get("models")
        if models is not None:
            metadata = models.get(logical_model)
            if metadata is not None:
                configured_family = (
                    metadata["family"]
                    if isinstance(metadata, Mapping)
                    else metadata.family
                )
                if configured_family != family:
                    raise RouteError(
                        "logical model does not belong to requested family"
                    )
        provider_config = config["providers"]
        pool_providers = set(
            provider_config["accountPools"][pool]["providers"]
        )
        fallback = tuple(provider_config["fallbackRoutes"][family])
        fallback_rank = {
            provider: rank for rank, provider in enumerate(fallback)
        }
    except (KeyError, TypeError) as error:
        raise RouteError("route configuration is incomplete") from error

    allowed = set(allowed_providers)
    routes = []
    for account in accounts:
        if (
            account.state != "active"
            or account.pool != pool
            or (
                locked_account_id is not None
                and account.id != locked_account_id
            )
            or account.provider not in allowed
            or account.provider not in pool_providers
            or account.provider not in fallback_rank
            or account.provider not in upstream_by_provider
        ):
            continue
        digest = hashlib.sha256(
            f"{account.id}\0{logical_model}".encode("utf-8")
        ).hexdigest()[:16]
        upstream_model = (
            f"{account.routing_prefix}/"
            f"{upstream_by_provider[account.provider]}"
        )
        if (
            available_models is not None
            and upstream_model not in available_models
        ):
            continue
        routes.append(
            Route(
                account_id=account.id,
                provider=account.provider,
                family=family,
                logical_model=logical_model,
                upstream_model=upstream_model,
                claudex_profile=f"ocp-{digest}",
                priority=account.priority,
                pool=pool,
            )
        )
    return tuple(
        sorted(
            routes,
            key=lambda route: (
                -route.priority,
                fallback_rank[route.provider],
                route.account_id,
            ),
        )
    )


def choose_new_session_route(
    accounts: Sequence[Account],
    *,
    pools: Sequence[str],
    family: str,
    logical_model: str,
    config: Mapping[str, object],
    health: Mapping[str, str],
    selection_ordinal: int,
    allowed_providers: Collection[str],
    locked_account_id: str | None,
    upstream_by_provider: Mapping[str, str],
    available_models: Collection[str] | None = None,
) -> Route:
    if type(selection_ordinal) is not int or selection_ordinal < 0:
        raise RouteError("selection ordinal must be a non-negative integer")
    for pool in pools:
        healthy = [
            route
            for route in eligible_routes(
                accounts,
                pool=pool,
                family=family,
                logical_model=logical_model,
                config=config,
                allowed_providers=allowed_providers,
                locked_account_id=locked_account_id,
                upstream_by_provider=upstream_by_provider,
                available_models=available_models,
            )
            if health.get(route.account_id, "healthy") == "healthy"
        ]
        if not healthy:
            continue
        highest_priority = max(route.priority for route in healthy)
        tier = [
            route for route in healthy if route.priority == highest_priority
        ]
        return tier[selection_ordinal % len(tier)]
    raise RouteError("no healthy account route is available")


def route_chain(
    accounts: Sequence[Account],
    *,
    pools: Sequence[str],
    family: str,
    logical_model: str,
    config: Mapping[str, object],
    health: Mapping[str, str],
    selection_ordinal: int,
    allowed_providers: Collection[str],
    locked_account_id: str | None,
    upstream_by_provider: Mapping[str, str],
    max_alternates: int = 1,
    available_models: Collection[str] | None = None,
) -> tuple[Route, ...]:
    """Select one primary and a bounded ordered same-model recovery route."""
    if type(max_alternates) is not int or max_alternates < 0 or max_alternates > 1:
        raise RouteError("route chain permits at most one alternate")
    primary = choose_new_session_route(
        accounts,
        pools=pools,
        family=family,
        logical_model=logical_model,
        config=config,
        health=health,
        selection_ordinal=selection_ordinal,
        allowed_providers=allowed_providers,
        locked_account_id=locked_account_id,
        upstream_by_provider=upstream_by_provider,
        available_models=available_models,
    )
    candidates: list[Route] = []
    seen: set[str] = {primary.account_id}
    for pool in pools:
        for route in eligible_routes(
            accounts,
            pool=pool,
            family=family,
            logical_model=logical_model,
            config=config,
            allowed_providers=allowed_providers,
            locked_account_id=locked_account_id,
            upstream_by_provider=upstream_by_provider,
            available_models=available_models,
        ):
            if (
                route.account_id not in seen
                and health.get(route.account_id, "healthy") == "healthy"
            ):
                candidates.append(route)
                seen.add(route.account_id)
    return (primary, *candidates[:max_alternates])


def validate_route_credential(
    route: Route,
    accounts: Sequence[Account],
    *,
    auth_dir: Path,
    provider_document: Mapping[str, object],
) -> None:
    """Revalidate the exact live credential immediately before activation."""
    try:
        account = find_account(accounts, route.account_id)
        provider = provider_document["providers"][account.provider]
        expected_type = provider["authType"]
    except (KeyError, TypeError) as error:
        raise RouteError("route provider configuration is incomplete") from error
    if (
        account.state != "active"
        or account.provider != route.provider
        or account.pool != route.pool
        or route.upstream_model
        != f"{account.routing_prefix}/"
        + route.upstream_model.split("/", 1)[-1]
    ):
        raise RouteError("route no longer matches its account binding")
    try:
        credential = resolve_credential_ref(
            auth_dir,
            account.credential_ref,
            expected_provider=expected_type,
        )
    except RuntimeError as error:
        raise RouteError("route credential is unavailable") from error
    if (
        credential.disabled
        or credential.prefix != account.routing_prefix
        or credential.priority != account.priority
    ):
        raise RouteError("route credential is not active for this account")
