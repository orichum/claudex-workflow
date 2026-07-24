#!/usr/bin/env python3
"""Tests for automatic, existing-graph Git hook maintenance."""

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


class GraphifyHookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name).resolve()
        self.workflow = self.fixture / "workflow"
        self.repo = self.fixture / "xebia" / "repo"
        self.palace = self.fixture / "palace"
        (self.workflow / "runtime").mkdir(parents=True)
        self.repo.mkdir(parents=True)
        self.palace.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.graph = self.repo / "graphify-out" / "graph.json"
        self.graph.parent.mkdir()
        self.graph.write_text("{}", encoding="utf-8")
        config = self.fixture / "context.json"
        config.write_text(json.dumps({"contexts": [{
            "root": str(self.repo.parent), "dockerProfile": "xebia",
            "memoryPalace": str(self.palace), "memoryWing": "xebia",
        }]}), encoding="utf-8")
        models = self.fixture / "models.json"
        models.write_text(json.dumps({"object": "list", "data": [
            {"id": "gpt-5.6-sol"},
            {"id": "gpt-5.6-terra"},
            {"id": "claude-sonnet-5"},
            {"id": "claude-opus-4-8"},
        ]}), encoding="utf-8")
        with mock.patch("integrations.common.session_config.shutil.which", return_value=None):
            self.session = create_session(
                self.workflow,
                self.repo,
                config,
                routing_path=REPOSITORY_ROOT / "config" / "model-stacks.json",
                models_path=models,
                plugin_source=REPOSITORY_ROOT / "controller" / "plugin",
            )

    def test_installs_only_when_official_status_reports_missing(self):
        fake_bin = self.fixture / "bin"
        fake_bin.mkdir()
        calls = self.fixture / "calls"
        graphify = fake_bin / "graphify"
        graphify.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*|$PWD\" >> {calls}\n"
            "if [ \"$1 $2\" = \"hook status\" ]; then "
            "printf 'post-commit: not installed\\n'; fi\n",
            encoding="utf-8",
        )
        graphify.chmod(0o755)
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "CLAUDEX_WORKFLOW_ROOT": str(self.workflow),
            "CLAUDEX_RUN_DIR": str(self.session.run_dir),
            "CLAUDEX_RUN_ID": self.session.run_id,
            "CLAUDEX_CONTEXT_FILE": str(self.session.context_file),
            "CLAUDEX_CONTEXT_SHA256": self.session.context_sha256,
        })
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "controller/plugin/scripts/ensure-graphify-hook.py")],
            env=environment, text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(calls.read_text().splitlines(), [
            f"hook status|{self.repo}", f"hook install|{self.repo}",
        ])

    def test_missing_graph_is_a_silent_noop(self):
        self.graph.unlink()
        environment = os.environ.copy()
        environment.update({
            "CLAUDEX_WORKFLOW_ROOT": str(self.workflow),
            "CLAUDEX_RUN_DIR": str(self.session.run_dir),
            "CLAUDEX_RUN_ID": self.session.run_id,
            "CLAUDEX_CONTEXT_FILE": str(self.session.context_file),
            "CLAUDEX_CONTEXT_SHA256": self.session.context_sha256,
        })
        completed = subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "controller/plugin/scripts/ensure-graphify-hook.py")],
            env=environment, text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
