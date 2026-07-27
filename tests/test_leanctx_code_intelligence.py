#!/usr/bin/env python3
"""Product-boundary tests for LeanCTX as the sole code-intelligence layer."""

from pathlib import Path
import sys
import subprocess
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from integrations.common import leanctx_contract


class LeanctxCodeIntelligenceTests(unittest.TestCase):
    def test_retired_optimizer_names_are_absent_from_the_repository(self) -> None:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        retired = ("head" + "room", "graph" + "ify")
        matches: list[str] = []
        for relative in completed.stdout.splitlines():
            path = REPOSITORY_ROOT / relative
            if not path.is_file() or path.is_symlink():
                continue
            try:
                content = path.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError:
                continue
            for name in retired:
                if name in content:
                    matches.append(f"{relative}: {name}")
        self.assertEqual(matches, [])

    def test_fixed_contract_includes_bounded_graph_tools(self) -> None:
        self.assertEqual(
            leanctx_contract.TOOLS,
            (
                "ctx_read",
                "ctx_search",
                "ctx_tree",
                "ctx_expand",
                "ctx_graph",
                "ctx_impact",
                "ctx_callgraph",
                "ctx_knowledge",
                "ctx_overview",
                "ctx_patch",
                "ctx_shell",
            ),
        )
        config = leanctx_contract.config_bytes().decode("utf-8")
        for tool in (
            "ctx_graph",
            "ctx_impact",
            "ctx_callgraph",
            "ctx_knowledge",
            "ctx_overview",
        ):
            self.assertIn(tool, config)
        self.assertIn('disabled_tools = ["ctx_call"', config)
        self.assertNotIn('"ctx_callgraph"', config.split("disabled_tools =", 1)[1])

if __name__ == "__main__":
    unittest.main()
