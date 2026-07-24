#!/usr/bin/env python3
"""Project CLIProxyAPI's live routes into safe model choices."""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import io
import json
import math
import time
from typing import Mapping, Sequence

from .account_registry import Account
from .model_routing import RoutingError, validate_model_id
from .stack_definition import ModelDefinition


MAX_MODEL_CATALOG_BYTES = 2 * 1024 * 1024


class CatalogError(RuntimeError):
    """The live model catalogue is unavailable or unsafe."""


@dataclass(frozen=True)
class LiveModelChoice:
    family: str
    provider: str
    upstream: str
    account_ids: tuple[str, ...] = field(repr=False)
    account_names: tuple[str, ...]


@dataclass(frozen=True)
class UnclassifiedModel:
    provider: str
    upstream: str
    account_names: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class LiveCatalog:
    choices: tuple[LiveModelChoice, ...]
    unclassified: tuple[UnclassifiedModel, ...]


def _reject_constant(value: str) -> object:
    raise CatalogError(f"non-finite JSON constant {value}")


def _unique_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("live model catalogue has duplicate JSON keys")
        result[key] = value
    return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CatalogError("live model catalogue deadline exceeded")
    return remaining


class _DeadlineRawIO(io.RawIOBase):
    def __init__(self, sock: object, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, target: object) -> int:
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv_into(target)


class _DeadlineSocket:
    def __init__(self, sock: object, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def sendall(self, payload: bytes) -> None:
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(payload)

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise CatalogError("CLIProxyAPI response mode is invalid")
        return io.BufferedReader(
            _DeadlineRawIO(self._sock, self._deadline)
        )

    def close(self) -> None:
        self._sock.close()


def fetch_live_catalog(port: int, timeout: float = 4.0) -> object:
    """Fetch one bounded model list from a loopback CLIProxyAPI port."""
    if type(port) is not int or port < 1024 or port > 65535:
        raise CatalogError("CLIProxyAPI port is invalid")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or not math.isfinite(timeout)
    ):
        raise CatalogError("CLIProxyAPI timeout is invalid")
    duration = float(timeout)
    deadline = time.monotonic() + duration
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=duration
    )
    try:
        connection.connect()
        if connection.sock is None:
            raise CatalogError("CLIProxyAPI connection is unavailable")
        _remaining(deadline)
        connection.sock = _DeadlineSocket(connection.sock, deadline)
        connection.request("GET", "/v1/models")
        _remaining(deadline)
        response = connection.getresponse()
        _remaining(deadline)
        if response.status != 200:
            raise CatalogError("CLIProxyAPI model request was rejected")
        payload = response.read(MAX_MODEL_CATALOG_BYTES + 1)
        _remaining(deadline)
        if len(payload) > MAX_MODEL_CATALOG_BYTES:
            raise CatalogError("CLIProxyAPI model catalogue is too large")
        document = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except CatalogError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        http.client.HTTPException,
        OSError,
    ) as error:
        raise CatalogError(
            "CLIProxyAPI model catalogue is unavailable"
        ) from error
    finally:
        connection.close()
    if not isinstance(document, dict):
        raise CatalogError("CLIProxyAPI model catalogue must be an object")
    return document


def classify_model(
    provider: str,
    upstream: str,
    known_models: Mapping[str, ModelDefinition],
    provider_document: Mapping[str, object],
) -> str | None:
    """Classify an upstream route without inferring an undeclared family."""
    exact_families = {
        definition.family
        for definition in known_models.values()
        if definition.routes.get(provider) == upstream
    }
    if len(exact_families) == 1:
        return next(iter(exact_families))
    if exact_families:
        return None

    providers = provider_document.get("providers")
    if not isinstance(providers, Mapping):
        return None
    provider_config = providers.get(provider)
    if not isinstance(provider_config, Mapping):
        return None
    family_prefixes = provider_config.get("familyPrefixes")
    if not isinstance(family_prefixes, Mapping):
        return None
    matches = {
        family
        for family, prefixes in family_prefixes.items()
        if isinstance(family, str)
        and isinstance(prefixes, list)
        and any(
            isinstance(prefix, str) and upstream.startswith(prefix)
            for prefix in prefixes
        )
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _catalog_ids(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        raise CatalogError("live model catalogue must be an object")
    if raw.get("object", "list") != "list":
        raise CatalogError("live model catalogue object must be list")
    data = raw.get("data")
    if not isinstance(data, list):
        raise CatalogError("live model catalogue data must be an array")
    model_ids = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise CatalogError("live model catalogue contains an invalid entry")
        try:
            model_ids.append(validate_model_id(item["id"], "live model"))
        except RoutingError as error:
            raise CatalogError(str(error)) from error
    return tuple(dict.fromkeys(model_ids))


def project_live_catalog(
    raw: object,
    accounts: Sequence[Account],
    known_models: Mapping[str, ModelDefinition],
    provider_document: Mapping[str, object],
) -> LiveCatalog:
    """Strip internal route prefixes and group selectable live models."""
    active_by_prefix = {
        account.routing_prefix: account
        for account in accounts
        if account.state == "active"
    }
    choices: dict[tuple[str, str, str], dict[str, Account]] = {}
    unclassified: dict[tuple[str, str], dict[str, Account]] = {}
    for model_id in _catalog_ids(raw):
        prefix, separator, upstream = model_id.partition("/")
        if not separator:
            continue
        account = active_by_prefix.get(prefix)
        if account is None:
            continue
        family = classify_model(
            account.provider,
            upstream,
            known_models,
            provider_document,
        )
        if family is None:
            key = (account.provider, upstream)
            unclassified.setdefault(key, {})[account.id] = account
            continue
        key = (account.provider, family, upstream)
        choices.setdefault(key, {})[account.id] = account

    projected_choices = []
    for (provider, family, upstream), grouped in sorted(choices.items()):
        ordered = sorted(
            grouped.values(), key=lambda account: (account.name, account.id)
        )
        projected_choices.append(
            LiveModelChoice(
                family=family,
                provider=provider,
                upstream=upstream,
                account_ids=tuple(account.id for account in ordered),
                account_names=tuple(account.name for account in ordered),
            )
        )

    projected_unclassified = []
    for (provider, upstream), grouped in sorted(unclassified.items()):
        ordered = sorted(
            grouped.values(), key=lambda account: (account.name, account.id)
        )
        projected_unclassified.append(
            UnclassifiedModel(
                provider=provider,
                upstream=upstream,
                account_names=tuple(account.name for account in ordered),
                reason="no exact route or declared family prefix",
            )
        )
    return LiveCatalog(
        choices=tuple(projected_choices),
        unclassified=tuple(projected_unclassified),
    )
