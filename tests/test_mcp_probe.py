#!/usr/bin/env python3
"""Tests for protocol-level MCP readiness validation."""

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
        result = {"tools": [
            {"name": "query_graph", "inputSchema": {"type": "object"}},
            {"name": "graph_stats", "inputSchema": {"type": "object"}},
        ]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""",
            encoding="utf-8",
        )

    def run_probe(self, *required: str, broken: bool = False) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        if broken:
            environment["FAKE_MCP_BROKEN"] = "1"
        command = [sys.executable, str(PROBE)]
        for tool in required:
            command.extend(["--require-tool", tool])
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

    def test_rejects_an_entrypoint_whose_help_works_but_runtime_fails(self):
        help_check = subprocess.run(
            [sys.executable, str(self.server), "--help"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(help_check.returncode, 0)
        completed = self.run_probe("query_graph", broken=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MCP server exited before responding", completed.stderr)


if __name__ == "__main__":
    unittest.main()
