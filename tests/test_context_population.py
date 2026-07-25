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
        for name in ("graphify-out", ".venv", "node_modules"):
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
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tool_directory = tempfile.TemporaryDirectory()
        self.fake_package_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.palace = self.root / "palace"
        self.data_root = self.root / "orichum-data"
        self.data_root.mkdir(mode=0o700)
        self.calls_path = self.root / "calls.jsonl"
        self.original_path = os.environ.get("PATH", "")
        self.environ = {
            "PATH": f"{self.tool_directory.name}{os.pathsep}{self.original_path}",
            "CONTEXT_POPULATION_CALL_LOG": str(self.calls_path),
            "PYTHONPATH": self.fake_package_directory.name,
            "ORICHUM_DATA_HOME": str(self.data_root),
        }
        fake_mempalace = Path(self.fake_package_directory.name) / "mempalace"
        fake_mempalace.mkdir()
        (fake_mempalace / "__init__.py").write_text("", encoding="utf-8")
        (fake_mempalace / "palace.py").write_text(
            'SKIP_DIRS = {"existing-generated"}\n', encoding="utf-8"
        )
        self.write_tool("mempalace")
        self.write_tool("graphify")

    def tearDown(self):
        self.fake_package_directory.cleanup()
        self.tool_directory.cleanup()
        self.temporary_directory.cleanup()

    def write_tool(self, name):
        script = self.tool_directory.name + "/" + name
        Path(script).write_text(
            """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
call = {"tool": tool, "args": sys.argv[1:], "cwd": os.getcwd()}
if tool == "mempalace" and os.environ.get("CONTEXT_POPULATION_RECORD_MEMPALACE_ENV"):
    from mempalace.palace import SKIP_DIRS
    call["generated_exclusion"] = os.environ.get(
        "CLAUDEX_MEMPALACE_EXCLUDE_GENERATED"
    )
    call["dont_write_bytecode"] = os.environ.get("PYTHONDONTWRITEBYTECODE")
    call["python_unbuffered"] = os.environ.get("PYTHONUNBUFFERED")
    call["pythonpath"] = os.environ.get("PYTHONPATH")
    call["skip_dirs"] = sorted(SKIP_DIRS)
with Path(os.environ["CONTEXT_POPULATION_CALL_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(call) + "\\n")

if tool == "mempalace":
    if os.environ.get("CONTEXT_POPULATION_MEMPALACE_FAIL"):
        print("mine diagnostic", file=sys.stdout)
        print("mine failed", file=sys.stderr)
        raise SystemExit(1)
    print("raw mempalace success output")
    raise SystemExit(0)

operation = sys.argv[1]
if operation in {"extract", "update"}:
    print("raw graphify success output")
    repository = Path(sys.argv[2])
    failure = os.environ.get("CONTEXT_POPULATION_GRAPHIFY_FAILURE")
    if repository.name == failure:
        for index in range(30):
            print(f"stdout {index}")
            print(f"stderr {index}", file=sys.stderr)
        raise SystemExit(1)
    if repository.name == "empty":
        print("found 0 code")
        print("graph is empty", file=sys.stderr)
        raise SystemExit(1)
    graph = Path(os.environ["GRAPHIFY_OUT"]) / "graph.json"
    graph.parent.mkdir(exist_ok=True)
    if repository.name == "invalid":
        graph.write_text("not json", encoding="utf-8")
    elif repository.name == "empty-nodes":
        graph.write_text(json.dumps({"nodes": []}), encoding="utf-8")
    else:
        graph.write_text(json.dumps({"nodes": [{"id": "node"}]}), encoding="utf-8")
    raise SystemExit(0)

if operation == "hook":
    if sys.argv[2] == "install" and os.environ.get("CONTEXT_POPULATION_HOOK_INSTALL_FAIL"):
        print("install failed", file=sys.stderr)
        raise SystemExit(1)
    if sys.argv[2] == "status" and os.environ.get("CONTEXT_POPULATION_HOOK_NOT_INSTALLED"):
        print("not installed")
    raise SystemExit(0)
""",
            encoding="utf-8",
        )
        Path(script).chmod(0o755)

    def init_git(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        fixture = path / ".context-population-fixture"
        fixture.write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(path), "add", fixture.name],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(path),
                "-c", "user.name=Claudex Tests",
                "-c", "user.email=claudex-tests@example.invalid",
                "commit", "-qm", "fixture",
            ],
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

    def read_calls(self):
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(
            encoding="utf-8"
        ).splitlines()]

    def populate(self):
        with mock.patch.dict(os.environ, self.environ, clear=False):
            return context_population.populate_context(self.root, self.palace, "acme")

    def test_population_mines_each_canonical_repository_and_graphifies_each(self):
        api = self.root / "api"
        web = self.root / "web"
        self.init_git(api)
        self.init_git(web)

        result = self.populate()
        calls = self.read_calls()

        self.assertEqual(
            [
                {key: value for key, value in call.items() if key != "cwd"}
                for call in calls if call["tool"] == "mempalace"
            ],
            [
                {"tool": "mempalace", "args": [
                    "--palace", str(self.palace), "mine", str(api),
                    "--mode", "projects", "--wing", "acme",
                ]},
                {"tool": "mempalace", "args": [
                    "--palace", str(self.palace), "mine", str(web),
                    "--mode", "projects", "--wing", "acme",
                ]},
                {"tool": "mempalace", "args": ["--palace", str(self.palace), "status"]},
            ],
        )
        self.assertEqual(
            [call for call in calls if call["tool"] == "graphify"],
            [
                {"tool": "graphify", "args": ["extract", str(api), "--code-only"], "cwd": str(Path.cwd())},
                {"tool": "graphify", "args": ["extract", str(web), "--code-only"], "cwd": str(Path.cwd())},
            ],
        )
        self.assertLess(
            max(
                index for index, call in enumerate(calls)
                if call["tool"] == "mempalace"
            ),
            min(
                index for index, call in enumerate(calls)
                if call["tool"] == "graphify"
            ),
        )
        self.assertEqual(
            tuple(row.repository for row in result.repositories), (api, web)
        )
        self.assertEqual(len(result.repositories), 2)
        self.assertEqual({row.action for row in result.repositories}, {"created"})
        self.assertEqual(
            {row.hook_status for row in result.repositories}, {"not managed"}
        )
        self.assertFalse((api / "graphify-out").exists())
        self.assertFalse((web / "graphify-out").exists())
        self.assertEqual(
            len(tuple((self.data_root / "graphs").rglob("graph.json"))),
            2,
        )
        self.assertEqual(result.palace, self.palace)
        self.assertEqual(result.wing, "acme")

    def test_population_skips_linked_worktree_for_memory_and_graphify(self):
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
        progress = []

        with mock.patch.dict(os.environ, self.environ, clear=False):
            result = context_population.populate_context(
                self.root, self.palace, "acme", progress=progress.append
            )

        mine_sources = [
            Path(call["args"][3])
            for call in self.read_calls()
            if call["tool"] == "mempalace" and "mine" in call["args"]
        ]
        graphify_sources = [
            Path(call["args"][1])
            for call in self.read_calls()
            if call["tool"] == "graphify" and call["args"][0] in {"extract", "update"}
        ]
        self.assertEqual(mine_sources, [primary])
        self.assertEqual(graphify_sources, [primary])
        self.assertEqual(tuple(row.repository for row in result.repositories), (primary,))
        self.assertIn(
            "[discover] skipped linked worktree service-fix "
            "— same repository as service",
            progress,
        )

    def test_mempalace_mines_outer_repository_once_for_nested_submodule(self):
        repository = self.root / "service"
        submodule = repository / "vendor" / "module"
        self.init_git(repository)
        self.init_git(submodule)

        self.populate()

        mine_sources = [
            Path(call["args"][3])
            for call in self.read_calls()
            if call["tool"] == "mempalace" and "mine" in call["args"]
        ]
        graphify_sources = [
            Path(call["args"][1])
            for call in self.read_calls()
            if call["tool"] == "graphify" and call["args"][0] in {"extract", "update"}
        ]
        self.assertEqual(mine_sources, [repository])
        self.assertEqual(graphify_sources, [repository, submodule])

    def test_mempalace_mine_excludes_graphify_output_without_editing_repository(self):
        repository = self.root / "service"
        self.init_git(repository)
        generated = repository / "graphify-out"
        generated.mkdir()
        (generated / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "generated"}]}), encoding="utf-8"
        )
        source = repository / "service.py"
        source.write_text("print('source')\n", encoding="utf-8")

        with mock.patch.dict(
            os.environ,
            {**self.environ, "CONTEXT_POPULATION_RECORD_MEMPALACE_ENV": "1"},
            clear=False,
        ):
            context_population.populate_context(
                self.root, self.palace, "acme"
            )

        mine_call = next(
            call for call in self.read_calls()
            if call["tool"] == "mempalace" and "mine" in call["args"]
        )
        self.assertEqual(mine_call["generated_exclusion"], "1")
        self.assertEqual(mine_call["dont_write_bytecode"], "1")
        self.assertEqual(mine_call["python_unbuffered"], "1")
        self.assertEqual(
            mine_call["skip_dirs"], ["existing-generated", "graphify-out"]
        )
        self.assertTrue(
            mine_call["pythonpath"].split(os.pathsep)[0].endswith(
                "integrations/mempalace_sitecustomize"
            )
        )
        self.assertFalse((repository / ".gitignore").exists())
        self.assertEqual(source.read_text(encoding="utf-8"), "print('source')\n")

    def test_mempalace_progress_reports_first_five_percent_and_final(self):
        messages = []
        reporter = context_population._MempalaceProgress(
            "[mempalace 1/4]", messages.append, started=10.0
        )
        with mock.patch.object(
            context_population.time, "monotonic", return_value=20.0
        ):
            for current in (1, 4, 5, 6, 10, 100):
                reporter(
                    "stdout",
                    f"  + [{current:4}/100] file-{current}.py +3",
                )
            reporter("stderr", "unrelated warning")
            reporter("stdout", "unrelated output")

        self.assertEqual(len(messages), 4)
        self.assertIn("1/100 (1%)", messages[0])
        self.assertIn("5/100 (5%)", messages[1])
        self.assertIn("10/100 (10%)", messages[2])
        self.assertIn("100/100 (100%)", messages[3])
        self.assertTrue(
            all("00:10 elapsed" in message for message in messages)
        )

    def test_rejects_nested_linked_worktree_before_mempalace_scan(self):
        primary = self.root
        linked = self.root / ".worktrees" / "service-fix"
        self.init_git(primary)
        self.add_commit(primary)
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-q", "--detach", str(linked)],
            check=True,
            capture_output=True,
            text=True,
        )

        with mock.patch.dict(
            os.environ, self.environ, clear=False
        ), self.assertRaisesRegex(
            PopulationError,
            "linked worktree is nested inside a MemPalace source",
        ):
            context_population.populate_context(
                self.root, self.palace, "acme"
            )

        self.assertEqual(self.read_calls(), [])

    def test_existing_repository_local_graph_is_migrated(self):
        repository = self.root / "api"
        self.init_git(repository)
        graph = repository / "graphify-out" / "graph.json"
        graph.parent.mkdir()
        graph.write_text(json.dumps({"nodes": [{"id": "old"}]}), encoding="utf-8")

        result = self.populate()

        self.assertEqual(result.repositories[0].action, "migrated")
        self.assertFalse((repository / "graphify-out").exists())
        self.assertNotIn(
            "update",
            [
                call["args"][0]
                for call in self.read_calls()
                if call["tool"] == "graphify"
            ],
        )

    def test_relative_tool_path_is_canonical_for_central_sync(self):
        repository = self.root / "api"
        self.init_git(repository)
        relative_tools = os.path.relpath(self.tool_directory.name, Path.cwd())
        environ = {**self.environ, "PATH": f"{relative_tools}{os.pathsep}{self.original_path}"}

        with mock.patch.dict(os.environ, environ, clear=False):
            self.assertEqual(
                context_population._resolve_executable("graphify", "Graphify"),
                str((Path(self.tool_directory.name) / "graphify").resolve()),
            )
            result = context_population.populate_context(self.root, self.palace, "acme")

        self.assertEqual(result.repositories[0].hook_status, "not managed")
        hook_calls = [
            call for call in self.read_calls()
            if call["tool"] == "graphify" and call["args"][0] == "hook"
        ]
        self.assertEqual(hook_calls, [])

    def test_empty_code_repository_is_not_applicable(self):
        repository = self.root / "empty"
        self.init_git(repository)
        progress = []

        with mock.patch.object(
            context_population, "_format_elapsed", return_value="00:03"
        ), mock.patch.dict(os.environ, self.environ, clear=False):
            result = context_population.populate_context(
                self.root, self.palace, "acme", progress=progress.append
            )

        self.assertEqual(
            result.repositories,
            (context_population.RepositoryResult(
                repository, "not applicable", "not applicable"
            ),),
        )
        self.assertIn(
            "[graphify 1/1] not-applicable empty",
            progress,
        )

    def test_rejects_invalid_or_empty_graph_json(self):
        for name in ("invalid", "empty-nodes"):
            with self.subTest(name=name):
                repository = self.root / name
                self.init_git(repository)
                with self.assertRaisesRegex(PopulationError, "Graphify graph is invalid"):
                    self.populate()
                shutil.rmtree(repository)

    def test_population_defers_upstream_graphify_hooks(self):
        self.init_git(self.root / "api")

        result = self.populate()

        self.assertEqual(result.repositories[0].hook_status, "not managed")
        self.assertFalse(any(
            call["tool"] == "graphify" and call["args"][0] == "hook"
            for call in self.read_calls()
        ))

    def test_missing_tools_are_rejected_but_graphify_is_not_needed_without_repositories(self):
        original_which = context_population.shutil.which

        with mock.patch.object(
            context_population.shutil,
            "which",
            side_effect=lambda tool: None if tool == "mempalace" else original_which(tool),
        ), mock.patch.dict(os.environ, self.environ, clear=False), \
             self.assertRaisesRegex(PopulationError, "MemPalace executable"):
            context_population.populate_context(self.root, self.palace, "acme")

        with mock.patch.object(
            context_population.shutil,
            "which",
            side_effect=lambda tool: None if tool == "graphify" else original_which(tool),
        ), mock.patch.dict(os.environ, self.environ, clear=False), \
             self.assertRaisesRegex(PopulationError, "Graphify executable"):
            self.init_git(self.root / "api")
            context_population.populate_context(self.root, self.palace, "acme")

        with tempfile.TemporaryDirectory() as no_worktrees_directory:
            no_worktrees = Path(no_worktrees_directory)
            with mock.patch.object(
                context_population.shutil,
                "which",
                side_effect=lambda tool: None if tool == "graphify" else original_which(tool),
            ), mock.patch.dict(os.environ, self.environ, clear=False):
                result = context_population.populate_context(no_worktrees, self.palace, "acme")
            self.assertEqual(result.repositories, ())

    def test_failure_diagnostics_are_bounded(self):
        self.init_git(self.root / "api")
        with mock.patch.dict(
            os.environ,
            {**self.environ, "CONTEXT_POPULATION_GRAPHIFY_FAILURE": "api"},
            clear=False,
        ), self.assertRaisesRegex(
            PopulationError, "Graphify failed with exit code 1"
        ) as caught:
            context_population.populate_context(self.root, self.palace, "acme")

        message = str(caught.exception)
        self.assertLessEqual(len(message), 4_000)
        self.assertIn("stdout 29", message)
        self.assertNotIn("stdout 0", message)
        self.assertIn("stderr 29", message)

    def test_failed_run_retries_idempotently_and_retains_prior_graph(self):
        first = self.root / "first"
        second = self.root / "second"
        self.init_git(first)
        self.init_git(second)

        with mock.patch.dict(
            os.environ,
            {**self.environ, "CONTEXT_POPULATION_GRAPHIFY_FAILURE": "second"},
            clear=False,
        ), self.assertRaises(PopulationError):
            context_population.populate_context(self.root, self.palace, "acme")
        first_graphs = tuple((self.data_root / "graphs").rglob("graph.json"))
        self.assertEqual(len(first_graphs), 1)
        self.assertFalse((first / "graphify-out").exists())

        result = self.populate()

        actions = {row.repository.name: row.action for row in result.repositories}
        self.assertEqual(actions, {"first": "updated", "second": "created"})

    def test_progress_reports_mempalace_and_each_completed_repository(self):
        first = self.root / "first"
        second = self.root / "second"
        self.init_git(first)
        self.init_git(second)
        progress = []
        original_run = context_population._run

        def run_with_heartbeat(command, **keywords):
            heartbeat = keywords.get("heartbeat")
            if heartbeat is not None:
                heartbeat(10.0)
            return original_run(command, **keywords)

        with mock.patch.object(
            context_population, "_run", side_effect=run_with_heartbeat
        ), mock.patch.object(
            context_population, "_format_elapsed", return_value="00:10"
        ), mock.patch.dict(
            os.environ,
            {**self.environ, "CONTEXT_POPULATION_GRAPHIFY_FAILURE": "second"},
            clear=False,
        ), self.assertRaises(PopulationError):
            result = context_population.populate_context(
                self.root, self.palace, "acme", progress=progress.append
            )

        self.assertEqual(progress[-2:], [
            "[graphify 1/2] created first",
            "[graphify 2/2] created second",
        ])
        self.assertLess(
            progress.index("[mempalace] store verified"),
            progress.index("[graphify 1/2] created first"),
        )

    def test_default_monitor_reports_every_success_stage_without_raw_output(self):
        repository = self.root / "api"
        self.init_git(repository)
        progress = []

        with mock.patch.object(
            context_population, "_format_elapsed", return_value="00:00"
        ), mock.patch.dict(os.environ, self.environ, clear=False):
            result = context_population.populate_context(
                self.root, self.palace, "acme", progress=progress.append
            )

        self.assertEqual(result.repositories[0].action, "created")
        self.assertIn("[discover] 1/1 api — Graphify sync pending", progress)
        self.assertIn("[graphify 1/1] created api", progress)
        self.assertEqual(progress[-1], "[graphify 1/1] created api")
        joined = "\n".join(progress)
        self.assertNotIn("raw mempalace success output", joined)
        self.assertNotIn("raw graphify success output", joined)

    def test_monitor_escapes_control_characters_in_dynamic_fields(self):
        repository = self.root / (
            "bad\n[graphify forged] status\x1b[31m\u0085\u2028"
        )
        self.init_git(repository)
        progress = []

        with mock.patch.object(
            context_population,
            "_discover_git_layout",
            return_value=context_population.RepositoryDiscovery((repository,), ()),
        ), mock.patch.dict(os.environ, self.environ, clear=False):
            result = context_population.populate_context(
                self.root,
                self.palace,
                "wing\nforged\x1b[31m\u0085\u2028",
                progress=progress.append,
            )

        for event in progress:
            self.assertNotIn("\n", event)
            self.assertNotIn("\x1b", event)
            self.assertNotIn("\u0085", event)
            self.assertNotIn("\u2028", event)
        joined = "\n".join(progress)
        escaped_name = (
            r"bad\n[graphify forged] status\u001b[31m\u0085\u2028"
        )
        escaped_wing = r"wing\nforged\u001b[31m\u0085\u2028"
        self.assertIn(escaped_name, joined)
        self.assertIn(escaped_wing, joined)

        rendered = context_population.render_population_result(result)
        self.assertNotIn("\n[graphify forged]", rendered)
        self.assertNotIn("\u0085", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertIn(escaped_name, rendered)
        self.assertIn(escaped_wing, rendered)

    def test_render_population_result_uses_dynamic_table_and_empty_message(self):
        result = context_population.PopulationResult(
            self.palace,
            "acme",
            (context_population.RepositoryResult(self.root / "api", "created", "installed"),),
        )

        rendered = context_population.render_population_result(result)

        self.assertIn(f"MemPalace: populated wing acme in {self.palace}", rendered)
        self.assertIn("| REPOSITORY", rendered)
        self.assertIn(str(self.root / "api"), rendered)
        self.assertIn("| created", rendered)
        self.assertIn("| installed", rendered)
        self.assertIn(
            "Graphify: no applicable Git repositories found.",
            context_population.render_population_result(
                context_population.PopulationResult(self.palace, "acme", ())
            ),
        )


if __name__ == "__main__":
    unittest.main()
