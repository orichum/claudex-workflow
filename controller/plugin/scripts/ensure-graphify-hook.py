#!/usr/bin/env python3
"""Install Graphify's official Git hooks for the verified current repository."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

WORKFLOW_SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKFLOW_SOURCE))
from integrations.common.session_config import SessionError, verify_context_binding


def main() -> int:
    try:
        data_value = os.environ.get("CLAUDEX_DATA_DIR")
        binding = verify_context_binding(
            Path(os.environ["CLAUDEX_WORKFLOW_ROOT"]),
            Path(os.environ["CLAUDEX_RUN_DIR"]),
            Path(os.environ["CLAUDEX_CONTEXT_FILE"]),
            os.environ["CLAUDEX_CONTEXT_SHA256"],
            os.environ["CLAUDEX_RUN_ID"],
            Path(data_value) if data_value else None,
        )
        repo_value = binding.context.get("repoRootReal")
        if not isinstance(repo_value, str) or not repo_value:
            return 0
        repo = Path(repo_value).resolve(strict=True)
        graph = (repo / "graphify-out" / "graph.json").resolve(strict=True)
        graph.relative_to(repo)
        if not graph.is_file():
            return 0
        graphify = shutil.which("graphify")
        if graphify is None:
            return 0
        status = subprocess.run(
            [graphify, "hook", "status"], cwd=repo, capture_output=True,
            text=True, timeout=15, check=False,
        )
        if status.returncode != 0 or "not installed" in status.stdout.lower():
            installed = subprocess.run(
                [graphify, "hook", "install"], cwd=repo, capture_output=True,
                text=True, timeout=30, check=False,
            )
            return 0 if installed.returncode == 0 else 1
        return 0
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError, SessionError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
