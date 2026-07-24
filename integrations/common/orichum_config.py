#!/usr/bin/env python3
"""Load and validate Orichum's focused, non-secret control-plane files."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from .model_routing import (
    ROLES,
    RoutingError,
    validate_model_id,
    validate_routing_document,
)
from .project_context import ContextError, validate_config_document
from .github_identity import GithubIdentityError, validate_github_account


MAX_CONFIG_BYTES = 2 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_EFFORTS = {"low", "medium", "high", "max"}
_ADAPTER_TYPES = {"anthropic", "openai-compatible"}
_REDACTED_KEYS = (
    "token",
    "password",
    "secret",
    "credential",
    "apikey",
    "api_key",
    "authorization",
    "cookie",
    "accesskey",
    "privatekey",
    "signature",
)
_AUTH_VALUE = re.compile(
    r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+\S+"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SENSITIVE_QUERY_KEYS = {
    "key",
    "sig",
    "signature",
    "apikey",
    "accesskey",
    "accesskeyid",
    "secretkey",
    "secretaccesskey",
    "token",
    "accesstoken",
    "idtoken",
    "refreshtoken",
    "clientsecret",
    "sharedaccesssignature",
    "sas",
}
_CONTROL_PLANE_DOCUMENTS = {
    "model-stacks",
    "projects",
    "providers",
    "plugins",
    "runtime",
    "controller-policy",
}


class ConfigError(RuntimeError):
    """The focused Orichum configuration is invalid or inconsistent."""


@dataclass(frozen=True)
class ConfigPaths:
    root: Path
    model_stacks: Path
    projects: Path
    providers: Path
    plugins: Path
    runtime: Path
    controller_policy: Path


@dataclass(frozen=True)
class ResolvedConfig:
    documents: Mapping[str, object]
    sources: Mapping[str, str]


def default_config_paths(root: Path) -> ConfigPaths:
    root = Path(root)
    return ConfigPaths(
        root=root,
        model_stacks=root / "model-stacks.json",
        projects=root / "projects.json",
        providers=root / "providers.json",
        plugins=root / "plugins.json",
        runtime=root / "runtime.json",
        controller_policy=root / "controller-policy.md",
    )


def _reject_constant(value: str) -> object:
    raise ConfigError(f"non-finite JSON constant {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> object:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ConfigError(f"{label} could not be read") from error
    if len(content) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{label} is too large")
    try:
        return json.loads(
            content.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ConfigError(f"{label} is not valid JSON") from error


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ConfigError(f"{label} is invalid")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value


def _bounded_int(value: object, low: int, high: int, label: str) -> int:
    if type(value) is not int or value < low or value > high:
        raise ConfigError(f"{label} must be between {low} and {high}")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{label} must be a boolean")
    return value


def _sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(
        re.sub(r"[^a-z0-9]", "", marker) in normalized
        for marker in _REDACTED_KEYS
    )


def _sensitive_query_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return normalized in _SENSITIVE_QUERY_KEYS or _sensitive_key(value)


def _sensitive_string(value: str) -> bool:
    if _AUTH_VALUE.search(value) or _PRIVATE_KEY.search(value):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.scheme and parsed.netloc:
        if parsed.username is not None or parsed.password is not None:
            return True
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            if _sensitive_query_key(key):
                return True
    return False


def _reject_sensitive_values(value: object, label: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _sensitive_key(key):
                raise ConfigError(f"{label} contains a forbidden secret field")
            _reject_sensitive_values(item, label)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_values(item, label)
    elif isinstance(value, str) and _sensitive_string(value):
        raise ConfigError(f"{label} contains credential-bearing text")


def _unique_identifiers(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{label} must be a non-empty array")
    result = tuple(_identifier(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ConfigError(f"{label} must be unique")
    return result


def _validate_models(document: object) -> tuple[dict[str, object], dict[str, object]]:
    raw = _exact(
        document,
        {"schemaVersion", "defaultStack", "models", "stacks"},
        "model-stacks",
    )
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ConfigError("model-stacks schemaVersion must be exactly 1")
    models = raw["models"]
    if not isinstance(models, dict) or not models:
        raise ConfigError("models must be a non-empty object")
    normalized_models: dict[str, object] = {}
    for raw_model, raw_metadata in models.items():
        model = validate_model_id(raw_model, "model")
        metadata = _exact(
            raw_metadata, {"provider", "family", "upstream"}, f"model {model}"
        )
        normalized_models[model] = {
            "provider": _identifier(metadata["provider"], f"model {model} provider"),
            "family": _identifier(metadata["family"], f"model {model} family"),
            "upstream": validate_model_id(
                metadata["upstream"], f"model {model} upstream"
            ),
        }
    routing = validate_routing_document(
        {
            "schemaVersion": raw["schemaVersion"],
            "defaultStack": raw["defaultStack"],
            "stacks": raw["stacks"],
        }
    )
    referenced: set[str] = set()
    for stack in routing["stacks"].values():
        referenced.add(stack["controller"])
        for role in ROLES:
            referenced.update(stack["agents"][role])
    missing = sorted(referenced - set(normalized_models))
    if missing:
        raise ConfigError(f"model declarations are missing: {', '.join(missing)}")
    return normalized_models, routing


def _validate_providers(
    document: object,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, tuple[str, ...]]]:
    raw = _exact(
        document,
        {"schemaVersion", "providers", "accountPools", "fallbackRoutes"},
        "providers",
    )
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ConfigError("providers schemaVersion must be exactly 1")
    raw_providers = raw["providers"]
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ConfigError("providers must be a non-empty object")
    providers: dict[str, set[str]] = {}
    for raw_name, raw_provider in raw_providers.items():
        name = _identifier(raw_name, "provider name")
        provider = _exact(
            raw_provider,
            {"type", "transport", "families", "authType"},
            f"provider {name}",
        )
        if provider["type"] not in _ADAPTER_TYPES:
            raise ConfigError(f"provider {name} has an unsupported adapter type")
        if provider["transport"] != "cliproxy":
            raise ConfigError(f"provider {name} transport must be cliproxy")
        _identifier(
            provider["authType"], f"provider {name} auth type"
        )
        providers[name] = set(
            _unique_identifiers(provider["families"], f"provider {name} families")
        )

    raw_pools = raw["accountPools"]
    if not isinstance(raw_pools, dict) or not raw_pools:
        raise ConfigError("accountPools must be a non-empty object")
    pools: dict[str, set[str]] = {}
    for raw_name, raw_pool in raw_pools.items():
        name = _identifier(raw_name, "account pool")
        pool = _exact(raw_pool, {"providers"}, f"account pool {name}")
        pool_providers = set(
            _unique_identifiers(
                pool["providers"], f"account pool {name} providers"
            )
        )
        unknown = pool_providers - set(providers)
        if unknown:
            raise ConfigError(f"account pool {name} names an unknown provider")
        pools[name] = pool_providers

    raw_routes = raw["fallbackRoutes"]
    if not isinstance(raw_routes, dict) or not raw_routes:
        raise ConfigError("fallbackRoutes must be a non-empty object")
    routes: dict[str, tuple[str, ...]] = {}
    for raw_family, raw_route in raw_routes.items():
        family = _identifier(raw_family, "fallback family")
        route = _unique_identifiers(raw_route, f"fallback route {family}")
        for provider in route:
            if provider not in providers:
                raise ConfigError(f"fallback route {family} names an unknown provider")
            if family not in providers[provider]:
                raise ConfigError(
                    f"provider {provider} does not support family {family}"
                )
        routes[family] = route
    declared_families = set().union(*providers.values())
    missing_routes = declared_families - set(routes)
    if missing_routes:
        raise ConfigError(
            "provider families have no fallback route: "
            + ", ".join(sorted(missing_routes))
        )
    return providers, pools, routes


def _validate_projects(
    document: object,
    *,
    routing: Mapping[str, object],
    models: Mapping[str, object],
    pools: Mapping[str, object],
    routes: Mapping[str, tuple[str, ...]],
) -> None:
    raw = _exact(document, {"schemaVersion", "contexts"}, "projects")
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ConfigError("projects schemaVersion must be exactly 1")
    contexts = raw["contexts"]
    if not isinstance(contexts, list):
        raise ConfigError("project contexts must be an array")
    portable_contexts = []
    for index, raw_context in enumerate(contexts):
        base_keys = {
            "root",
            "dockerProfile",
            "modelStack",
            "accountPools",
            "memoryPalace",
            "memoryWing",
        }
        if not isinstance(raw_context, dict) or set(raw_context) not in (
            base_keys,
            base_keys | {"githubAccount"},
        ):
            raise ConfigError(
                f"project context {index} has invalid fields"
            )
        context = raw_context
        try:
            validate_github_account(context.get("githubAccount"))
        except GithubIdentityError as error:
            raise ConfigError(
                f"project context {index} has invalid githubAccount"
            ) from error
        selected_pools = _unique_identifiers(
            context["accountPools"], f"project context {index} accountPools"
        )
        if any(pool not in pools for pool in selected_pools):
            raise ConfigError(f"project context {index} names an unknown account pool")
        eligible_providers: set[str] = set()
        for pool in selected_pools:
            eligible_providers.update(pools[pool])
        selected_stack = context["modelStack"] or routing["defaultStack"]
        stack = routing["stacks"].get(selected_stack)
        if stack is None:
            raise ConfigError(f"project context {index} names an unknown model stack")

        def model_is_routable(model: str) -> bool:
            family = models[model]["family"]
            return bool(eligible_providers.intersection(routes[family]))

        if not model_is_routable(stack["controller"]):
            raise ConfigError(
                f"project context {index} cannot route its controller"
            )
        for role in ROLES:
            if not any(
                model_is_routable(candidate)
                for candidate in stack["agents"][role]
            ):
                raise ConfigError(
                    f"project context {index} cannot route role {role}"
                )
        portable_contexts.append(
            {
                key: value
                for key, value in context.items()
                if key != "accountPools"
            }
        )
    validate_config_document(
        {"contexts": portable_contexts},
        Path.home(),
        dict(routing["stacks"]),
    )


def _validate_plugins(document: object) -> None:
    raw = _exact(
        document, {"schemaVersion", "marketplaces", "plugins"}, "plugins"
    )
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ConfigError("plugins schemaVersion must be exactly 1")
    marketplaces = raw["marketplaces"]
    plugins = raw["plugins"]
    if not isinstance(marketplaces, list) or not isinstance(plugins, list):
        raise ConfigError("plugin declarations must be arrays")
    names = []
    for index, raw_marketplace in enumerate(marketplaces):
        marketplace = _exact(
            raw_marketplace, {"name", "source"}, f"marketplace {index}"
        )
        names.append(_identifier(marketplace["name"], "marketplace name"))
        _nonempty_string(marketplace["source"], "marketplace source")
    if len(names) != len(set(names)):
        raise ConfigError("marketplace names must be unique")
    plugin_ids = tuple(_nonempty_string(value, "plugin ID") for value in plugins)
    if len(plugin_ids) != len(set(plugin_ids)):
        raise ConfigError("plugin IDs must be unique")


def _validate_runtime(document: object) -> None:
    raw = _exact(
        document,
        {"schemaVersion", "controller"},
        "runtime",
    )
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise ConfigError("runtime schemaVersion must be exactly 1")
    controller = _exact(
        raw["controller"],
        {"effort", "maxToolUseConcurrency", "maxSubagentsPerSession"},
        "runtime controller",
    )
    if controller["effort"] not in _EFFORTS:
        raise ConfigError("controller effort is invalid")
    _bounded_int(
        controller["maxToolUseConcurrency"], 1, 24, "maxToolUseConcurrency"
    )
    _bounded_int(
        controller["maxSubagentsPerSession"], 1, 64, "maxSubagentsPerSession"
    )


def validate_control_plane(config: ResolvedConfig) -> None:
    try:
        if set(config.documents) != _CONTROL_PLANE_DOCUMENTS:
            raise ConfigError("control-plane documents are incomplete")
        if set(config.sources) != _CONTROL_PLANE_DOCUMENTS or any(
            not isinstance(source, str) or not source
            for source in config.sources.values()
        ):
            raise ConfigError("control-plane sources are incomplete")
        for name, document in config.documents.items():
            _reject_sensitive_values(document, name)
        models, routing = _validate_models(config.documents["model-stacks"])
        providers, pools, routes = _validate_providers(
            config.documents["providers"]
        )
        for model, metadata in models.items():
            provider = metadata["provider"]
            family = metadata["family"]
            if provider not in providers:
                raise ConfigError(f"model {model} names an unknown provider")
            if family not in providers[provider]:
                raise ConfigError(
                    f"provider {provider} does not support model family {family}"
                )
            if family not in routes or provider not in routes[family]:
                raise ConfigError(f"model {model} family has no provider route")
        _validate_projects(
            config.documents["projects"],
            routing=routing,
            models=models,
            pools=pools,
            routes=routes,
        )
        _validate_plugins(config.documents["plugins"])
        _validate_runtime(config.documents["runtime"])
        policy = config.documents["controller-policy"]
        if not isinstance(policy, str) or not policy.strip():
            raise ConfigError("controller policy must not be empty")
    except KeyError as error:
        raise ConfigError(f"control-plane document is missing: {error.args[0]}") from error
    except (RoutingError, ContextError) as error:
        raise ConfigError(str(error)) from error


def load_control_plane(paths: ConfigPaths) -> ResolvedConfig:
    documents: dict[str, object] = {
        "model-stacks": _read_json(paths.model_stacks, "model-stacks"),
        "projects": _read_json(paths.projects, "projects"),
        "providers": _read_json(paths.providers, "providers"),
        "plugins": _read_json(paths.plugins, "plugins"),
        "runtime": _read_json(paths.runtime, "runtime"),
    }
    try:
        policy_bytes = paths.controller_policy.read_bytes()
    except OSError as error:
        raise ConfigError("controller policy could not be read") from error
    if len(policy_bytes) > MAX_CONFIG_BYTES:
        raise ConfigError("controller policy is too large")
    try:
        documents["controller-policy"] = policy_bytes.decode("utf-8")
    except UnicodeError as error:
        raise ConfigError("controller policy is not UTF-8") from error
    root_real = paths.root.resolve(strict=False)

    def source(path: Path) -> str:
        path_real = path.resolve(strict=False)
        try:
            relative = path_real.relative_to(root_real)
        except ValueError:
            return str(path_real)
        return str(Path(root_real.name) / relative)

    sources = {
        "model-stacks": source(paths.model_stacks),
        "projects": source(paths.projects),
        "providers": source(paths.providers),
        "plugins": source(paths.plugins),
        "runtime": source(paths.runtime),
        "controller-policy": source(paths.controller_policy),
    }
    resolved = ResolvedConfig(documents=documents, sources=sources)
    validate_control_plane(resolved)
    return resolved


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if _sensitive_key(key)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str) and _sensitive_string(value):
        return "<redacted>"
    return value


def redact_control_plane(config: ResolvedConfig) -> dict[str, object]:
    redacted = _redact(deepcopy(dict(config.documents)))
    redacted["controller-policy"] = "<policy omitted>"
    return redacted
