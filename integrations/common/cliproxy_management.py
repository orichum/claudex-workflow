#!/usr/bin/env python3
"""Minimal authenticated client for workflow-owned CLIProxyAPI management."""

from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
from typing import Mapping
from urllib.parse import urlparse


_KEY = re.compile(r"[A-Za-z0-9._~-]{32,256}")
_CREDENTIAL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\.json")
_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ALLOWED_FIELDS = {"prefix", "priority"}
_OAUTH_STATE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_OAUTH_PROVIDERS = {
    "claude": "anthropic",
    "codex": "codex",
    "antigravity": "antigravity",
    "kimi": "kimi",
}
_MAX_MANAGEMENT_RESPONSE = 64 * 1024


class ManagementError(RuntimeError):
    """CLIProxyAPI management state or a request failed closed."""


@dataclass(frozen=True)
class ManagementEndpoint:
    port: int
    data_home: Path
    key: str = field(repr=False)


@dataclass(frozen=True)
class OAuthSession:
    provider: str
    url: str = field(repr=False)
    state: str = field(repr=False)


def _private_file(path: Path, label: str) -> bytes:
    try:
        observed = os.lstat(path)
    except OSError as failure:
        raise ManagementError(f"{label} is unavailable") from failure
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise ManagementError(f"{label} is unsafe")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ManagementError(f"{label} changed while opening")
        content = os.read(descriptor, 65537)
        if len(content) > 65536 or os.read(descriptor, 1):
            raise ManagementError(f"{label} is too large")
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
            raise ManagementError(f"{label} changed while reading")
        return content
    except OSError as failure:
        raise ManagementError(f"{label} could not be read safely") from failure
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_management_endpoint(data_home: Path) -> ManagementEndpoint:
    data_home = Path(data_home)
    try:
        directory = os.lstat(data_home)
    except OSError as failure:
        raise ManagementError("Orichum data directory is unavailable") from failure
    if (
        stat.S_ISLNK(directory.st_mode)
        or not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.getuid()
        or stat.S_IMODE(directory.st_mode) != 0o700
    ):
        raise ManagementError("Orichum data directory is unsafe")
    try:
        resolved = data_home.resolve(strict=True)
    except (OSError, RuntimeError) as failure:
        raise ManagementError(
            "Orichum data directory could not be resolved"
        ) from failure
    confirmed = os.lstat(resolved)
    if (
        confirmed.st_dev != directory.st_dev
        or confirmed.st_ino != directory.st_ino
    ):
        raise ManagementError("Orichum data directory changed while resolving")
    data_home = resolved
    try:
        ports = json.loads(
            _private_file(
                data_home / "service-ports.json", "service port state"
            )
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as failure:
        raise ManagementError("service port state is invalid") from failure
    port = ports.get("cliproxyPort") if isinstance(ports, dict) else None
    if type(port) is not int or port < 1 or port > 65535:
        raise ManagementError("CLIProxyAPI port is invalid")
    try:
        key = _private_file(
            data_home / "cliproxy-management.key", "management key"
        ).decode("ascii").strip()
    except UnicodeError as failure:
        raise ManagementError("management key is invalid") from failure
    if not _KEY.fullmatch(key):
        raise ManagementError("management key is invalid")
    return ManagementEndpoint(port=port, data_home=data_home, key=key)


def attest_owned_connection(
    endpoint: ManagementEndpoint,
    client_port: int,
    *,
    verifier: Path | None = None,
) -> None:
    """Prove the managed service PID owns the exact connected TCP tuple."""
    verifier = (
        Path(__file__).resolve().parents[2] / "bin" / "orichum-verify-cliproxy"
        if verifier is None
        else Path(verifier)
    )
    if not verifier.is_file() or verifier.is_symlink():
        raise ManagementError("CLIProxyAPI ownership verifier is unavailable")
    try:
        completed = subprocess.run(
            [
                str(verifier),
                str(endpoint.data_home),
                str(endpoint.port),
                str(client_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        raise ManagementError("CLIProxyAPI ownership could not be verified") from failure
    if completed.returncode != 0:
        raise ManagementError("CLIProxyAPI connection is not Orichum-owned")


def _open_attested_connection(
    endpoint: ManagementEndpoint,
) -> http.client.HTTPConnection:
    try:
        connected = socket.create_connection(
            ("127.0.0.1", endpoint.port), timeout=3
        )
        connected.settimeout(3)
        client_host, client_port = connected.getsockname()
        if client_host != "127.0.0.1" or type(client_port) is not int:
            raise ManagementError("CLIProxyAPI connection is not loopback")
        attest_owned_connection(endpoint, client_port)
        connection = http.client.HTTPConnection(
            "127.0.0.1", endpoint.port, timeout=3
        )
        connection.sock = connected
        return connection
    except BaseException:
        try:
            connected.close()
        except UnboundLocalError:
            pass
        raise


def patch_auth_fields(
    endpoint: ManagementEndpoint,
    credential_ref: str,
    fields: Mapping[str, object],
) -> None:
    if (
        not isinstance(credential_ref, str)
        or not _CREDENTIAL_REF.fullmatch(credential_ref)
        or Path(credential_ref).name != credential_ref
    ):
        raise ManagementError("credential reference is invalid")
    if not fields or not set(fields).issubset(_ALLOWED_FIELDS):
        raise ManagementError("credential field update is invalid")
    prefix = fields.get("prefix")
    if prefix is not None and (
        not isinstance(prefix, str)
        or (prefix != "" and not _PREFIX.fullmatch(prefix))
    ):
        raise ManagementError("credential routing prefix is invalid")
    priority = fields.get("priority")
    if priority is not None and (
        type(priority) is not int or priority < 0 or priority > 1000
    ):
        raise ManagementError("credential priority is invalid")
    payload = json.dumps(
        {"name": credential_ref, **fields},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    connection = None
    try:
        connection = _open_attested_connection(endpoint)
        connection.request(
            "PATCH",
            "/v0/management/auth-files/fields",
            body=payload,
            headers={
            "Content-Type": "application/json",
            "X-Management-Key": endpoint.key,
            },
        )
        response = connection.getresponse()
        response.read(4096)
        if response.status != 200:
            raise ManagementError("CLIProxyAPI rejected credential update")
    except (http.client.HTTPException, TimeoutError, OSError) as failure:
        raise ManagementError("CLIProxyAPI credential update failed") from failure
    finally:
        if connection is not None:
            connection.close()


def _management_json(
    endpoint: ManagementEndpoint,
    method: str,
    path: str,
) -> dict[str, object]:
    connection = None
    try:
        connection = _open_attested_connection(endpoint)
        connection.request(
            method,
            path,
            headers={"X-Management-Key": endpoint.key},
        )
        response = connection.getresponse()
        payload = response.read(_MAX_MANAGEMENT_RESPONSE + 1)
        if response.status != 200:
            raise ManagementError("CLIProxyAPI rejected management request")
        if len(payload) > _MAX_MANAGEMENT_RESPONSE:
            raise ManagementError("CLIProxyAPI management response is too large")
        document = json.loads(payload.decode("utf-8"))
    except ManagementError:
        raise
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
    ) as failure:
        raise ManagementError(
            "CLIProxyAPI management request failed"
        ) from failure
    finally:
        if connection is not None:
            connection.close()
    if not isinstance(document, dict):
        raise ManagementError("CLIProxyAPI management response is invalid")
    return document


def _oauth_state(value: object) -> str:
    if not isinstance(value, str) or not _OAUTH_STATE.fullmatch(value):
        raise ManagementError("CLIProxyAPI OAuth state is invalid")
    return value


def start_oauth(
    endpoint: ManagementEndpoint,
    provider: str,
) -> OAuthSession:
    route = _OAUTH_PROVIDERS.get(provider)
    if route is None:
        raise ManagementError("provider does not support managed OAuth")
    document = _management_json(
        endpoint,
        "GET",
        f"/v0/management/{route}-auth-url?is_webui=true",
    )
    if document.get("status") != "ok":
        raise ManagementError("provider authentication could not start")
    url = document.get("url")
    if not isinstance(url, str):
        raise ManagementError("provider authorization URL is invalid")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ManagementError("provider authorization URL is invalid")
    return OAuthSession(
        provider=provider,
        url=url,
        state=_oauth_state(document.get("state")),
    )


def oauth_status(endpoint: ManagementEndpoint, state: str) -> str:
    state = _oauth_state(state)
    document = _management_json(
        endpoint,
        "GET",
        f"/v0/management/get-auth-status?state={state}",
    )
    status = document.get("status")
    if status == "wait":
        return "wait"
    if status == "ok":
        return "ok"
    raise ManagementError("provider authentication failed")


def cancel_oauth(endpoint: ManagementEndpoint, state: str) -> bool:
    state = _oauth_state(state)
    document = _management_json(
        endpoint,
        "DELETE",
        f"/v0/management/oauth-session?state={state}",
    )
    if document.get("status") != "ok" or type(
        document.get("cancelled")
    ) is not bool:
        raise ManagementError("provider authentication cancellation failed")
    return bool(document["cancelled"])
