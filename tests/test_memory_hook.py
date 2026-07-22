#!/usr/bin/env python3
"""Tests for zero-prompt, digest-bound MemPalace wing routing."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from integrations.common.session_config import create_session


class MemoryHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve()
        self.workflow = self.fixture / "workflow"
        self.project = self.fixture / "xebia" / "repo"
        self.palace = self.fixture / "palaces" / "xebia"
        for directory, mode in (
            (self.workflow / "runtime", 0o755),
            (self.project, 0o755),
            (self.palace, 0o700),
        ):
            directory.mkdir(parents=True)
            directory.chmod(mode)
        self.config = self.fixture / "context.json"
        self.config.write_text(json.dumps({"contexts": [{
            "root": str(self.project.parent),
            "dockerProfile": "xebia",
            "memoryPalace": str(self.palace),
            "memoryWing": "xebia",
        }]}), encoding="utf-8")
        with mock.patch("integrations.common.session_config.shutil.which", return_value=None):
            self.session = create_session(self.workflow, self.project, self.config)

    def invoke(self, payload, *, digest=None):
        environment = os.environ.copy()
        environment.update({
            "CLAUDEX_WORKFLOW_ROOT": str(self.workflow),
            "CLAUDEX_RUN_DIR": str(self.session.run_dir),
            "CLAUDEX_RUN_ID": self.session.run_id,
            "CLAUDEX_CONTEXT_FILE": str(self.session.context_file),
            "CLAUDEX_CONTEXT_SHA256": digest or self.session.context_sha256,
        })
        return subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "controller/plugin/scripts/route-mempalace-input.py")],
            input=json.dumps(payload), text=True, capture_output=True,
            env=environment, cwd=REPOSITORY_ROOT, check=False,
        )

    def test_injects_project_wing_without_model_context(self):
        completed = self.invoke({
            "tool_name": "mcp__mempalace__mempalace_search",
            "tool_input": {"query": "deployment convention", "limit": 5},
        })
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output, {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {
                "query": "deployment convention", "limit": 5, "wing": "xebia"
            },
        }})
        self.assertNotIn("additionalContext", completed.stdout)

    def test_rewrites_checkpoint_items_and_diary(self):
        completed = self.invoke({
            "tool_name": "mcp__mempalace__mempalace_checkpoint",
            "tool_input": {
                "items": [{"wing": "wrong", "room": "decisions", "content": "keep"}],
                "diary": {"agent_name": "claudex", "entry": "bounded"},
            },
        })
        updated = json.loads(completed.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["items"][0]["wing"], "xebia")
        self.assertEqual(updated["diary"]["wing"], "xebia")

    def test_digest_mismatch_fails_closed(self):
        completed = self.invoke({
            "tool_name": "mcp__mempalace__mempalace_search",
            "tool_input": {"query": "x"},
        }, digest="0" * 64)
        self.assertEqual(completed.returncode, 0)
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertNotIn(str(self.palace), completed.stdout)


if __name__ == "__main__":
    unittest.main()
