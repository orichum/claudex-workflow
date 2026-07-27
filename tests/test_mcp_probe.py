#!/usr/bin/env python3
"""Tests for protocol-level MCP readiness validation."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPOSITORY_ROOT / "integrations/common/mcp_probe.py"


class McpProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = Path(self.temporary.name) / "fake_mcp.py"
        self.server.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

if "--help" in sys.argv:
    print("fake help")
    raise SystemExit(0)
if os.environ.get("FAKE_MCP_BROKEN") == "1":
    print("missing runtime dependency", file=sys.stderr)
    raise SystemExit(3)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }
    elif method == "tools/list":
        cursor = request.get("params", {}).get("cursor")
        if os.environ.get("FAKE_MCP_PAGINATED") == "1" and cursor == "page-2":
            result = {"tools": [
                {"name": "ctx_call", "inputSchema": {"type": "object"}},
            ]}
        else:
            result = {"tools": [
                {"name": "query_graph", "inputSchema": {"type": "object"}},
                {"name": "graph_stats", "inputSchema": {"type": "object"}},
            ]}
            if os.environ.get("FAKE_MCP_PAGINATED") == "1":
                result["nextCursor"] = "page-2"
    elif method == "tools/call":
        name = request.get("params", {}).get("name")
        if os.environ.get("FAKE_MCP_CALL_ERROR") == "1":
            result = {
                "content": [{"type": "text", "text": "failed"}],
                "isError": True,
            }
        else:
            result = {
                "content": [
                    {"type": "text", "text": f"{name} completed"}
                ],
                "isError": False,
            }
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""",
            encoding="utf-8",
        )

    def run_probe(
        self,
        *required: str,
        exact: tuple[str, ...] = (),
        broken: bool = False,
        paginated: bool = False,
        calls: tuple[dict[str, object], ...] = (),
        call_error: bool = False,
    ) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        if broken:
            environment["FAKE_MCP_BROKEN"] = "1"
        if paginated:
            environment["FAKE_MCP_PAGINATED"] = "1"
        if call_error:
            environment["FAKE_MCP_CALL_ERROR"] = "1"
        command = [sys.executable, str(PROBE)]
        for tool in required:
            command.extend(["--require-tool", tool])
        for tool in exact:
            command.extend(["--exact-tool", tool])
        for call in calls:
            command.extend(
                [
                    "--probe-call",
                    json.dumps(call, separators=(",", ":")),
                ]
            )
        command.extend(["--", sys.executable, str(self.server)])
        return subprocess.run(
            command, env=environment, text=True, capture_output=True, check=False,
        )

    def test_initializes_and_lists_required_tools(self):
        completed = self.run_probe("query_graph", "graph_stats")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")

    def test_rejects_a_missing_required_tool(self):
        completed = self.run_probe("missing_tool")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("required MCP tool is unavailable: missing_tool", completed.stderr)

    def test_accepts_an_exact_tool_surface(self):
        completed = self.run_probe(
            exact=("query_graph", "graph_stats"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rejects_an_unexpected_advertised_tool(self):
        completed = self.run_probe(exact=("query_graph",))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "unexpected MCP tool is available: graph_stats",
            completed.stderr,
        )

    def test_exact_surface_checks_every_tools_page(self):
        completed = self.run_probe(
            exact=("query_graph", "graph_stats"),
            paginated=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "unexpected MCP tool is available: ctx_call",
            completed.stderr,
        )

    def test_rejects_an_entrypoint_whose_help_works_but_runtime_fails(self):
        help_check = subprocess.run(
            [sys.executable, str(self.server), "--help"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(help_check.returncode, 0)
        completed = self.run_probe("query_graph", broken=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MCP server exited before responding", completed.stderr)

    def test_calls_a_tool_and_checks_expected_output(self):
        completed = self.run_probe(
            calls=(
                {
                    "name": "query_graph",
                    "arguments": {"action": "build"},
                    "contains": "query_graph completed",
                },
            ),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_rejects_a_failed_tool_call(self):
        completed = self.run_probe(
            calls=({"name": "query_graph"},),
            call_error=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MCP tool call failed: query_graph", completed.stderr)

    def test_rejects_missing_expected_tool_output(self):
        completed = self.run_probe(
            calls=(
                {
                    "name": "query_graph",
                    "contains": "missing output",
                },
            ),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "MCP tool call omitted expected output: query_graph",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
