#!/usr/bin/env python3
"""Verify that a stdio MCP server initializes and exposes required tools."""

import argparse
import json
import select
import subprocess
import sys
import time
from typing import Any


class ProbeError(RuntimeError):
    pass


class McpClient:
    def __init__(self, command: list[str], timeout: float):
        self.timeout = timeout
        self.request_id = 0
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise ProbeError("MCP server could not be started") from error

    def _server_failure(self) -> ProbeError:
        detail = ""
        if self.process.stderr is not None and self.process.poll() is not None:
            detail = self.process.stderr.read(4096).strip().splitlines()[-1:]
            detail = f": {detail[0]}" if detail else ""
        return ProbeError(f"MCP server exited before responding{detail}")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        if self.process.stdin is None or self.process.stdout is None:
            raise ProbeError("MCP server pipes are unavailable")
        try:
            self.process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": method, "params": params,
            }, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise self._server_failure() from error

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError(f"MCP request timed out: {method}")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise ProbeError(f"MCP request timed out: {method}")
            line = self.process.stdout.readline()
            if not line:
                raise self._server_failure()
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProbeError("MCP server wrote non-JSON data to stdout") from error
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise ProbeError(f"MCP request failed: {method}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ProbeError(f"MCP response is invalid: {method}")
            return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise ProbeError("MCP server input is unavailable")
        try:
            self.process.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": method, "params": params,
            }, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise self._server_failure() from error

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


def probe(
    command: list[str],
    required_tools: list[str],
    exact_tools: list[str],
    timeout: float,
) -> None:
    client = McpClient(command, timeout)
    try:
        initialized = client.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claudex-mcp-probe", "version": "1"},
        })
        if not isinstance(initialized.get("serverInfo"), dict):
            raise ProbeError("MCP initialize response omitted serverInfo")
        client.notify("notifications/initialized", {})
        names: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            parameters = {} if cursor is None else {"cursor": cursor}
            listed = client.request("tools/list", parameters)
            tools = listed.get("tools")
            if not isinstance(tools, list):
                raise ProbeError("MCP tools/list response omitted tools")
            names.update(
                item["name"]
                for item in tools
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
            )
            next_cursor = listed.get("nextCursor")
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or not next_cursor:
                raise ProbeError("MCP tools/list returned an invalid cursor")
            if next_cursor in seen_cursors:
                raise ProbeError("MCP tools/list repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        for required in required_tools:
            if required not in names:
                raise ProbeError(f"required MCP tool is unavailable: {required}")
        if exact_tools:
            exact = set(exact_tools)
            missing = sorted(exact - names)
            if missing:
                raise ProbeError(
                    f"required MCP tool is unavailable: {missing[0]}"
                )
            unexpected = sorted(names - exact)
            if unexpected:
                raise ProbeError(
                    f"unexpected MCP tool is available: {unexpected[0]}"
                )
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--require-tool", action="append", default=[])
    parser.add_argument("--exact-tool", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command or arguments.timeout <= 0:
        parser.error("a command and positive timeout are required")
    try:
        probe(
            command,
            arguments.require_tool,
            arguments.exact_tool,
            arguments.timeout,
        )
    except ProbeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
