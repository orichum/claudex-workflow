"""Bounded, session-scoped public route telemetry."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import threading
from typing import Callable, Literal

from .route_selection import Route


_SESSION_ID = re.compile(r"oc-s-[a-f0-9]{16}")


@dataclass(frozen=True)
class RouteStatus:
    session_id: str
    account_id: str
    provider: str
    family: str
    logical_model: str
    route_state: Literal["primary", "fallback"]
    reason: Literal["primary", "retry", "cooldown"]
    last_http_status: int | None
    updated_at: str

    def as_public_json(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "accountId": self.account_id,
            "provider": self.provider,
            "family": self.family,
            "logicalModel": self.logical_model,
            "routeState": self.route_state,
            "reason": self.reason,
            "lastHttpStatus": self.last_http_status,
            "updatedAt": self.updated_at,
        }


class RouteStatusStore:
    def __init__(
        self,
        *,
        max_entries: int = 256,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if max_entries < 1:
            raise ValueError("route status bound must be at least one")
        self.max_entries = max_entries
        self.wall_clock = wall_clock
        self._entries: OrderedDict[str, RouteStatus] = OrderedDict()
        self._lock = threading.Lock()

    def select(
        self,
        session_id: str,
        route: Route,
        *,
        route_state: Literal["primary", "fallback"],
        reason: Literal["primary", "retry", "cooldown"],
    ) -> None:
        self._validate_session_id(session_id)
        status = RouteStatus(
            session_id=session_id,
            account_id=route.account_id,
            provider=route.provider,
            family=route.family,
            logical_model=route.logical_model,
            route_state=route_state,
            reason=reason,
            last_http_status=None,
            updated_at=self._utc_now(),
        )
        with self._lock:
            self._entries[session_id] = status
            self._entries.move_to_end(session_id)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def complete(self, session_id: str, http_status: int) -> None:
        self._validate_session_id(session_id)
        with self._lock:
            status = self._entries.get(session_id)
            if status is not None:
                self._entries[session_id] = replace(
                    status,
                    last_http_status=http_status,
                    updated_at=self._utc_now(),
                )

    def get(self, session_id: str) -> RouteStatus | None:
        self._validate_session_id(session_id)
        with self._lock:
            return self._entries.get(session_id)

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid logical session identifier")

    def _utc_now(self) -> str:
        return self.wall_clock().astimezone(timezone.utc).isoformat()
