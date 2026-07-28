#!/usr/bin/env python3
"""Launch one project-bound mcp-atlassian process."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence
from urllib.parse import urlsplit


_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class AtlassianError(RuntimeError):
    """A project Atlassian configuration is invalid or unavailable."""


def _non_blank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AtlassianError(f"{label} must be a non-empty string")
    return value.strip()


def _jira_url(value: object) -> str:
    value = _non_blank(value, "Jira URL").rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise AtlassianError("Jira URL is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AtlassianError(
            "Jira URL must be an HTTPS origin without credentials, "
            "a query, or a fragment"
        )
    return value


@dataclass(frozen=True)
class AtlassianConfig:
    url: str
    username: str
    api_token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _jira_url(self.url))
        object.__setattr__(
            self, "username", _non_blank(self.username, "Jira username")
        )
        object.__setattr__(
            self, "api_token", _non_blank(self.api_token, "Jira API token")
        )

    def as_json(self) -> dict[str, str]:
        return {
            "url": self.url,
            "username": self.username,
            "apiToken": self.api_token,
        }


def normalize_atlassian(value: object) -> AtlassianConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "url",
        "username",
        "apiToken",
    }:
        raise AtlassianError(
            "atlassian must contain exactly url, username, and apiToken"
        )
    return AtlassianConfig(
        url=value["url"],
        username=value["username"],
        api_token=value["apiToken"],
    )


def load_project_atlassian(
    config_path: Path, project_root: Path
) -> AtlassianConfig:
    try:
        document = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AtlassianError(
            "projects configuration is unavailable"
        ) from error
    contexts = document.get("contexts") if isinstance(document, dict) else None
    if not isinstance(contexts, list):
        raise AtlassianError("projects configuration is invalid")
    project_root = Path(project_root).expanduser().resolve(strict=True)
    for context in contexts:
        if not isinstance(context, dict) or "root" not in context:
            raise AtlassianError("projects configuration is invalid")
        try:
            root = Path(context["root"]).expanduser().resolve(strict=True)
        except (TypeError, OSError, RuntimeError) as error:
            raise AtlassianError("projects configuration is invalid") from error
        if root == project_root:
            configured = normalize_atlassian(context.get("atlassian"))
            if configured is None:
                raise AtlassianError(
                    "project does not configure Atlassian"
                )
            return configured
    raise AtlassianError("project context is not configured")


def mcp_environment(
    config: AtlassianConfig,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {
        key: value
        for key, value in inherited.items()
        if key in _SAFE_ENVIRONMENT_KEYS
    }
    environment.update(
        {
            "JIRA_URL": config.url,
            "JIRA_USERNAME": config.username,
            "JIRA_API_TOKEN": config.api_token,
            "MCP_TRANSPORT": "stdio",
            "MCP_VERBOSE": "false",
            "READ_ONLY_MODE": "false",
        }
    )
    return environment


def managed_binary(data_root: Path) -> Path:
    data_root = Path(data_root).expanduser().resolve(strict=True)
    candidate = data_root / "tools" / "bin" / "mcp-atlassian"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(data_root / "tools")
    except (OSError, RuntimeError, ValueError) as error:
        raise AtlassianError(
            "managed mcp-atlassian executable is unavailable"
        ) from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AtlassianError(
            "managed mcp-atlassian executable is unavailable"
        )
    return resolved


def serve(
    data_root: Path, config_path: Path, project_root: Path
) -> None:
    config = load_project_atlassian(config_path, project_root)
    binary = managed_binary(data_root)
    os.execve(str(binary), [str(binary)], mcp_environment(config))


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orichum-atlassian-mcp")
    parser.add_argument("data_root", type=Path)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("project_root", type=Path)
    parsed = parser.parse_args(arguments)
    try:
        serve(parsed.data_root, parsed.config_path, parsed.project_root)
    except (AtlassianError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    raise AssertionError("mcp-atlassian process returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
