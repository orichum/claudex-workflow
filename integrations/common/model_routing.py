#!/usr/bin/env python3
"""Validate and resolve portable provider-agnostic model stacks."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping, Optional, Sequence


ROLES: tuple[str, ...] = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
)
_STACK_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}$")


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EffectiveStack:
    stack_name: str
    controller: str
    candidates: Mapping[str, tuple[str, ...]]
    agents: Mapping[str, str]

    def as_json(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "stack": self.stack_name,
            "controller": self.controller,
            "configuredCandidates": {
                role: list(self.candidates[role]) for role in ROLES
            },
            "agents": {role: self.agents[role] for role in ROLES},
        }


def _exact_object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise RoutingError(f"{label} must contain exactly {sorted(keys)}")
    return value


def validate_stack_name(value: object, label: str = "stack name") -> str:
    if not isinstance(value, str) or not _STACK_PATTERN.fullmatch(value):
        raise RoutingError(f"{label} is invalid")
    return value


def validate_model_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _MODEL_PATTERN.fullmatch(value):
        raise RoutingError(f"{label} has an unsafe model ID")
    return value


def load_routing(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingError("model routing could not be parsed") from error
    document = _exact_object(
        raw, {"schemaVersion", "defaultStack", "stacks"}, "routing"
    )
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise RoutingError("schemaVersion must be exactly 1")
    default = validate_stack_name(document["defaultStack"], "defaultStack")
    stacks = document["stacks"]
    if not isinstance(stacks, dict) or not stacks:
        raise RoutingError("stacks must be a non-empty object")
    normalized: dict[str, object] = {}
    for raw_name, raw_stack in stacks.items():
        name = validate_stack_name(raw_name)
        stack = _exact_object(
            raw_stack, {"controller", "agents"}, f"stack {name}"
        )
        controller = validate_model_id(
            stack["controller"], f"stack {name} controller"
        )
        agents = _exact_object(
            stack["agents"], set(ROLES), f"stack {name} agents"
        )
        normalized_agents = {}
        for role in ROLES:
            values = agents[role]
            if not isinstance(values, list) or not values:
                raise RoutingError(
                    f"stack {name} role {role} needs candidates"
                )
            candidates = tuple(
                validate_model_id(value, f"stack {name} role {role}")
                for value in values
            )
            if len(candidates) != len(set(candidates)):
                raise RoutingError(
                    f"stack {name} role {role} has duplicate candidates"
                )
            normalized_agents[role] = candidates
        normalized[name] = {
            "controller": controller,
            "agents": normalized_agents,
        }
    if default not in normalized:
        raise RoutingError("defaultStack does not name an existing stack")
    return {
        "schemaVersion": 1,
        "defaultStack": default,
        "stacks": normalized,
    }


def load_catalog(path: Path) -> tuple[str, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingError("model catalogue could not be parsed") from error
    if (
        not isinstance(raw, dict)
        or raw.get("object") != "list"
        or not isinstance(raw.get("data"), list)
    ):
        raise RoutingError("model catalogue has an invalid shape")
    result = []
    for item in raw["data"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(validate_model_id(item["id"], "catalogue"))
    if not result:
        raise RoutingError("model catalogue is empty")
    return tuple(dict.fromkeys(result))


def resolve_effective(
    routing: Mapping[str, object],
    catalogue: Sequence[str],
    requested_stack: Optional[str] = None,
) -> EffectiveStack:
    name = requested_stack or str(routing["defaultStack"])
    stacks = routing["stacks"]
    if not isinstance(stacks, Mapping) or name not in stacks:
        raise RoutingError(f"model stack {name!r} is missing")
    stack = stacks[name]
    if not isinstance(stack, Mapping):
        raise RoutingError(f"model stack {name!r} is invalid")
    available = set(catalogue)
    controller = str(stack["controller"])
    if controller not in available:
        raise RoutingError(
            f"stack {name} controller {controller} is unavailable"
        )
    candidates = stack["agents"]
    if not isinstance(candidates, Mapping):
        raise RoutingError(f"model stack {name!r} is invalid")
    selected = {}
    for role in ROLES:
        role_candidates = tuple(candidates[role])
        selected_model = next(
            (model for model in role_candidates if model in available), None
        )
        if selected_model is None:
            raise RoutingError(
                f"stack {name} role {role} has no available candidate: "
                + ", ".join(role_candidates)
            )
        selected[role] = selected_model
    return EffectiveStack(name, controller, candidates, selected)
