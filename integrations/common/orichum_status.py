#!/usr/bin/env python3
"""Render a compact Orichum identity and session status line."""

from __future__ import annotations

import http.client
import json
import math
import os
from pathlib import Path
import sys
from typing import IO, Mapping, Sequence

from .account_registry import Account, AccountError, load_accounts
from .orichum_sessions import (
    LogicalSession,
    LogicalSessionError,
    load_logical_session,
)


_FAMILY_LABELS = {
    "claude": "Claude",
    "gemini": "Gemini",
    "gpt": "GPT",
    "kimi": "Kimi",
}
_MAX_INPUT_BYTES = 64 * 1024


def _text(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if (
        not value
        or len(value) > 96
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return fallback
    return value


def _percentage(value: object) -> str:
    if type(value) not in (int, float):
        return "—"
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 100:
        return "—"
    return f"{number:.0f}%"


def _nested_percentage(document: object, *path: str) -> str:
    value = document
    for key in path:
        if not isinstance(value, Mapping):
            return "—"
        value = value.get(key)
    return _percentage(value)


def _active_route(
    session: LogicalSession,
    route_status: Mapping[str, object] | None,
) -> tuple[str, str, str]:
    route = session.controller.primary
    state = "primary"
    reason = "primary"
    if (
        isinstance(route_status, Mapping)
        and route_status.get("sessionId") == session.id
        and route_status.get("routeState") in {"primary", "fallback"}
        and route_status.get("reason") in {"primary", "retry", "cooldown"}
        and isinstance(route_status.get("accountId"), str)
    ):
        candidates = (
            session.controller.primary,
            *session.controller.fallbacks,
        )
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.account_id == route_status["accountId"]
            ),
            None,
        )
        if selected is not None:
            route = selected
            state = str(route_status["routeState"])
            reason = str(route_status["reason"])
    label = "primary"
    if state == "fallback":
        label = (
            "fallback: cooldown"
            if reason == "cooldown"
            else "fallback: rate limit"
        )
    return route.family, route.account_id, label


def render_status(
    payload: object,
    session: LogicalSession,
    accounts: Sequence[Account],
    *,
    route_status: Mapping[str, object] | None,
    color: bool,
) -> str:
    """Render two bounded lines from Claude metrics and verified Orichum state."""
    family, account_id, route_label = _active_route(session, route_status)
    account_name = next(
        (
            account.name
            for account in accounts
            if account.id == account_id
        ),
        account_id,
    )
    model_display = session.controller.primary.logical_model
    if isinstance(payload, Mapping):
        model = payload.get("model")
        if isinstance(model, Mapping):
            model_display = _text(
                model.get("display_name"),
                _text(model.get("id"), model_display),
            )
    project = _text(Path(session.project_root).name, "project")
    stack = _text(session.stack, "stack")
    family_label = _FAMILY_LABELS.get(family, family.title())
    context = _nested_percentage(payload, "context_window", "used_percentage")
    five_hour = _nested_percentage(
        payload, "rate_limits", "five_hour", "used_percentage"
    )
    seven_day = _nested_percentage(
        payload, "rate_limits", "seven_day", "used_percentage"
    )

    if color:
        identity = f"\033[1;35mORICHUM\033[0m │ \033[36m{project}\033[0m │ {stack}"
        route = (
            f"\033[32m{family_label} · {model_display}\033[0m │ "
            f"{_text(account_name, account_id)} [{route_label}]"
        )
    else:
        identity = f"ORICHUM │ {project} │ {stack}"
        route = (
            f"{family_label} · {model_display} │ "
            f"{_text(account_name, account_id)} [{route_label}]"
        )
    return (
        f"{identity}\n{route} │ context {context} │ "
        f"5h {five_hour} │ 7d {seven_day}"
    )


def _fetch_route_status(
    data_home: Path,
    session_id: str,
) -> Mapping[str, object] | None:
    try:
        ports = json.loads(
            (data_home / "service-ports.json").read_text(encoding="utf-8")
        )
        port = ports["routeProxyPort"]
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=0.075
        )
        connection.request("GET", f"/status?session_id={session_id}")
        response = connection.getresponse()
        body = response.read(_MAX_INPUT_BYTES + 1)
        connection.close()
        if response.status != 200 or len(body) > _MAX_INPUT_BYTES:
            return None
        document = json.loads(body)
        if (
            not isinstance(document, dict)
            or document.get("sessionId") != session_id
        ):
            return None
        return document
    except (
        OSError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        return None


def main(
    *,
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    environment = os.environ if environment is None else environment
    try:
        raw = input_stream.read(_MAX_INPUT_BYTES + 1)
        if len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ValueError("status input is too large")
        payload = json.loads(raw)
        session_id = environment["ORICHUM_SESSION_ID"]
        state_home = Path(environment["ORICHUM_STATE_HOME"])
        config_home = Path(environment["ORICHUM_CONFIG_HOME"])
        data_home = Path(environment["ORICHUM_DATA_HOME"])
        session = load_logical_session(state_home, session_id)
        accounts = load_accounts(config_home / "accounts.json")
        rendered = render_status(
            payload,
            session,
            accounts,
            route_status=_fetch_route_status(data_home, session_id),
            color=(
                "NO_COLOR" not in environment
                and environment.get("TERM") != "dumb"
            ),
        )
    except (
        AccountError,
        KeyError,
        LogicalSessionError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        rendered = "ORICHUM │ status unavailable"
    print(rendered, file=output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
