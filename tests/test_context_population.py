#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from integrations.common.context_population import (
    PopulationError,
    _run,
    discover_git_worktrees,
)
from integrations.common import context_population


class ContextPopulationDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.outside_temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()

    def tearDown(self):
        self.outside_temporary_directory.cleanup()
        self.temporary_directory.cleanup()

    def init_git(self, path):
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )

    def add_commit(self, repository):
        tracked = repository / "tracked.txt"
        tracked.write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repository), "add", tracked.name],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(repository),
                "-c", "user.name=Claudex Tests",
                "-c", "user.email=claudex-tests@example.invalid",
                "commit", "-qm", "fixture",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_discovers_root_children_nested_and_git_file_worktrees(self):
        self.init_git(self.root)
        child = self.root / "child"
        nested = self.root / "group" / "nested"
        submodule = self.root / "vendor" / "module"
        for path in (child, nested, submodule):
            path.mkdir(parents=True, exist_ok=True)
            self.init_git(path)
        git_dir = submodule / ".git"
        metadata = self.root / ".git" / "modules" / "module"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        git_dir.rename(metadata)
        git_dir.write_text(f"gitdir: {metadata}\n", encoding="utf-8")

        self.assertEqual(
            discover_git_worktrees(self.root),
            tuple(sorted((self.root, child, nested, submodule), key=lambda p: str(p))),
        )

    def test_prefers_primary_checkout_over_linked_worktree_in_same_root(self):
        primary = self.root / "service"
        linked = self.root / ".worktrees" / "service-fix"
        self.init_git(primary)
        self.add_commit(primary)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(linked)],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(discover_git_worktrees(self.root), (primary,))

    def test_keeps_linked_worktree_when_primary_checkout_is_outside_root(self):
        primary = Path(self.outside_temporary_directory.name).resolve() / "service"
        linked = self.root / "service-fix"
        self.init_git(primary)
        self.add_commit(primary)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(linked)],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(discover_git_worktrees(self.root), (linked,))

    def test_selects_one_linked_worktree_when_primary_is_outside_root(self):
        primary = Path(self.outside_temporary_directory.name).resolve() / "service"
        first = self.root / "a-service-fix"
        second = self.root / "b-service-fix"
        self.init_git(primary)
        self.add_commit(primary)
        for linked in (first, second):
            subprocess.run(
                [
                    "git", "-C", str(primary), "worktree", "add", "-q",
                    "--detach", str(linked),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        discovery = context_population._discover_git_layout(self.root)

        self.assertEqual(discovery.repositories, (first,))
        self.assertEqual(
            discovery.skipped_worktrees,
            (context_population.SkippedWorktree(second, first),),
        )

    def test_prunes_generated_directories(self):
        self.init_git(self.root)
        for name in (".venv", "node_modules"):
            candidate = self.root / name / "repository"
            candidate.mkdir(parents=True)
            self.init_git(candidate)

        self.assertEqual(discover_git_worktrees(self.root), (self.root,))

    def test_rejects_symlinked_and_outside_worktree_roots(self):
        self.init_git(self.root)
        outside = Path(self.outside_temporary_directory.name).resolve()
        self.init_git(outside)
        link = self.root / "outside-link"
        link.symlink_to(outside, target_is_directory=True)

        self.assertEqual(discover_git_worktrees(self.root), (self.root,))

    def test_requires_root_to_resolve_to_a_directory(self):
        root_file = self.root / "not-a-directory"
        root_file.write_text("fixture", encoding="utf-8")

        with self.assertRaisesRegex(
            PopulationError, "context root must resolve to a directory"
        ):
            discover_git_worktrees(root_file)

    def test_root_resolution_errors_are_bounded(self):
        missing = self.root / "missing"
        broken = self.root / "broken"
        broken.symlink_to(missing)
        loop = self.root / "loop"
        loop.symlink_to(loop)

        for root in (missing, broken, loop):
            with self.subTest(root=root), self.assertRaisesRegex(
                PopulationError, "^context root must resolve to a directory$"
            ):
                discover_git_worktrees(root)

        with mock.patch.object(
            Path, "resolve", autospec=True, side_effect=PermissionError("source path")
        ), self.assertRaisesRegex(
            PopulationError, "^context root must resolve to a directory$"
        ):
            discover_git_worktrees(self.root)

    def test_walk_errors_are_bounded(self):
        def failing_walk(_root, *, followlinks, onerror):
            self.assertFalse(followlinks)
            onerror(OSError("source path"))
            return iter(())

        with mock.patch.object(
            context_population.os, "walk", side_effect=failing_walk
        ), self.assertRaisesRegex(
            PopulationError, "^context root could not be traversed$"
        ):
            discover_git_worktrees(self.root)

    def test_deduplicates_canonical_roots_and_rejects_outside_roots(self):
        first = self.root / "first"
        second = self.root / "second"
        rejected = self.root / "rejected"
        canonical = self.root / "canonical"
        outside = Path(self.outside_temporary_directory.name).resolve()
        for path in (first, second, rejected, canonical, outside):
            path.mkdir(exist_ok=True)
        for path in (first, second, rejected):
            (path / ".git").mkdir()

        completed = [
            subprocess.CompletedProcess([], 0, stdout=f"{canonical}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{canonical}\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=f"{outside}\n", stderr=""),
        ]
        with mock.patch.object(context_population, "_run", side_effect=completed):
            self.assertEqual(discover_git_worktrees(self.root), (canonical,))



class ContextPopulationRunnerTests(unittest.TestCase):
    def test_run_observes_lines_while_retaining_captured_output(self):
        observed = []
        completed = _run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "print('one', flush=True); "
                    "print('warning', file=sys.stderr, flush=True); "
                    "print('two', flush=True)"
                ),
            ],
            line_observer=lambda stream, line: observed.append((stream, line)),
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "one\ntwo\n")
        self.assertEqual(completed.stderr, "warning\n")
        self.assertCountEqual(
            observed,
            [("stdout", "one"), ("stdout", "two"), ("stderr", "warning")],
        )

    def test_run_uses_array_execution_and_translates_os_errors(self):
        process = mock.Mock()
        process.communicate.return_value = ("ok", "")
        process.returncode = 0
        working_directory = Path("working-directory")
        with mock.patch.object(
            context_population.subprocess, "Popen", return_value=process
        ) as popen:
            completed = _run(["git", "status"], cwd=working_directory)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "ok")
        popen.assert_called_once_with(
            ["git", "status"],
            cwd=working_directory,
            env=None,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        with mock.patch.object(
            context_population.subprocess, "Popen", side_effect=OSError("source path")
        ), self.assertRaisesRegex(
            PopulationError, "^required command could not be run$"
        ):
            _run(["git", "status"])

    def test_run_reports_elapsed_heartbeats_and_keeps_output_captured(self):
        heartbeats = []

        completed = _run(
            [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    "print('child output'); sys.stdout.flush(); time.sleep(0.08)"
                ),
            ],
            heartbeat=heartbeats.append,
            heartbeat_interval=0.01,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "child output")
        self.assertGreaterEqual(len(heartbeats), 1)
        self.assertEqual(heartbeats, sorted(heartbeats))
        self.assertGreater(heartbeats[0], 0)

    def test_run_terminates_the_active_child_when_interrupted(self):
        process = mock.Mock()
        process.communicate.side_effect = KeyboardInterrupt
        process.wait.return_value = 130

        with mock.patch.object(
            context_population.subprocess, "Popen", return_value=process
        ), self.assertRaises(KeyboardInterrupt):
            _run(["long-running-tool"])

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()

    def test_run_bounds_post_spawn_capture_errors(self):
        process = mock.Mock()
        process.communicate.side_effect = UnicodeDecodeError(
            "utf-8", b"\xff", 0, 1, "invalid start byte"
        )
        process.wait.return_value = 1

        with mock.patch.object(
            context_population.subprocess, "Popen", return_value=process
        ), self.assertRaisesRegex(
            PopulationError, "^required command could not be run$"
        ):
            _run(["bad-output-tool"])

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_heartbeat_deadlines_do_not_drift_with_callback_time(self):
        clock = [0.0]
        timeouts = []
        process = mock.Mock()
        process.returncode = 0

        def communicate(*, timeout):
            timeouts.append(timeout)
            clock[0] += timeout
            if len(timeouts) < 3:
                raise subprocess.TimeoutExpired(["slow-tool"], timeout)
            return "", ""

        def heartbeat(_elapsed):
            clock[0] += 2.0

        process.communicate.side_effect = communicate
        with mock.patch.object(
            context_population.subprocess, "Popen", return_value=process
        ), mock.patch.object(
            context_population.time, "monotonic", side_effect=lambda: clock[0]
        ):
            _run(
                ["slow-tool"], heartbeat=heartbeat, heartbeat_interval=10.0
            )

        self.assertEqual(timeouts, [10.0, 8.0, 8.0])




class ContextPopulationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.palace = self.root / "palace"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake_packages = self.root / "packages"
        mempalace_package = self.fake_packages / "mempalace"
        mempalace_package.mkdir(parents=True)
        (mempalace_package / "__init__.py").write_text("", encoding="utf-8")
        (mempalace_package / "palace.py").write_text(
            "SKIP_DIRS = set()\n",
            encoding="utf-8",
        )
        self.calls = self.root / "calls.jsonl"
        tool = self.bin / "mempalace"
        tool.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(os.environ["CONTEXT_POPULATION_CALL_LOG"]).open(
    "a", encoding="utf-8"
) as stream:
    stream.write(json.dumps({"args": sys.argv[1:]}) + "\\n")
if os.environ.get("CONTEXT_POPULATION_MEMPALACE_FAIL"):
    print("bounded failure", file=sys.stderr)
    raise SystemExit(7)
""",
            encoding="utf-8",
        )
        tool.chmod(0o755)
        self.environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "CONTEXT_POPULATION_CALL_LOG": str(self.calls),
            "PYTHONPATH": str(self.fake_packages),
        }

    def init_git(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", str(path),
                "-c", "user.name=Orichum Tests",
                "-c", "user.email=tests@example.invalid",
                "commit", "--allow-empty", "-qm", "fixture",
            ],
            check=True,
            capture_output=True,
        )

    def read_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [
            json.loads(line)["args"]
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]

    def populate(self, progress=None):
        with mock.patch.dict(os.environ, self.environment, clear=False):
            return context_population.populate_context(
                self.root,
                self.palace,
                "acme",
                progress=progress,
            )

    def test_population_mines_each_outer_repository_and_verifies_store(
        self,
    ) -> None:
        api = self.root / "api"
        web = self.root / "web"
        nested = api / "vendor" / "nested"
        for repository in (api, web, nested):
            self.init_git(repository)

        result = self.populate()

        self.assertEqual(
            self.read_calls(),
            [
                [
                    "--palace", str(self.palace), "mine", str(api),
                    "--mode", "projects", "--wing", "acme",
                ],
                [
                    "--palace", str(self.palace), "mine", str(web),
                    "--mode", "projects", "--wing", "acme",
                ],
                ["--palace", str(self.palace), "status"],
            ],
        )
        self.assertEqual(result.palace, self.palace)
        self.assertEqual(result.wing, "acme")

    def test_population_skips_redundant_linked_worktree(self) -> None:
        primary = self.root / "service"
        linked = self.root / "service-fix"
        self.init_git(primary)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-q", str(linked)],
            check=True,
            capture_output=True,
        )
        progress: list[str] = []

        self.populate(progress.append)

        mine_calls = [call for call in self.read_calls() if "mine" in call]
        self.assertEqual(len(mine_calls), 1)
        self.assertEqual(Path(mine_calls[0][3]), primary)
        self.assertTrue(
            any("skipped linked worktree service-fix" in line for line in progress)
        )

    def test_population_reports_repository_sources_without_indexing(
        self,
    ) -> None:
        repository = self.root / "service"
        self.init_git(repository)
        progress: list[str] = []

        self.populate(progress.append)

        self.assertIn(
            "[discover] 1/1 service — repository source",
            progress,
        )
        self.assertFalse(any("graph" in line.lower() for line in progress))

    def test_population_failure_is_bounded(self) -> None:
        self.init_git(self.root / "service")
        with (
            mock.patch.dict(
                os.environ,
                {
                    **self.environment,
                    "CONTEXT_POPULATION_MEMPALACE_FAIL": "1",
                },
                clear=False,
            ),
            self.assertRaisesRegex(
                PopulationError,
                "MemPalace mine.*exit code 7",
            ),
        ):
            context_population.populate_context(
                self.root,
                self.palace,
                "acme",
            )
        self.assertEqual(len(self.read_calls()), 1)

    def test_render_population_result_is_concise(self) -> None:
        rendered = context_population.render_population_result(
            context_population.PopulationResult(self.palace, "acme")
        )
        self.assertEqual(
            rendered,
            f"MemPalace: populated wing acme in {self.palace}\n",
        )


if __name__ == "__main__":
    unittest.main()
