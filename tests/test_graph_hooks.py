#!/usr/bin/env python3
"""Behavioral tests for Orichum-owned repository graph hooks."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from integrations.common.graph_hooks import (
    GraphHookError,
    _graph_log_path,
    _launch_detached_update,
    graph_hook_status,
    install_graph_hooks,
    remove_upstream_graphify_hooks,
)
from integrations.common.graph_manager import graph_main, sync_graph


class GraphHookTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.git(self.repository, "init", "-q")
        self.git(self.repository, "config", "user.name", "Graph Hook Tests")
        self.git(self.repository, "config", "user.email", "hooks@example.test")
        (self.repository / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self.git(self.repository, "add", "tracked.txt")
        self.git(self.repository, "commit", "-qm", "Initial")
        self.recorded = self.root / "recorded-path"
        self.launcher = self.root / "orichum launcher"
        self.launcher.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$3\" >> '{self.recorded}'\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o755)

    def git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()

    def hooks_dir(self, repository: Path | None = None) -> Path:
        repository = repository or self.repository
        value = self.git(repository, "rev-parse", "--git-path", "hooks")
        path = Path(value)
        return path if path.is_absolute() else repository / path

    def hook(self, name: str, repository: Path | None = None) -> Path:
        return self.hooks_dir(repository) / name

    def write_hook(self, name: str, content: str) -> Path:
        hook = self.hook(name)
        hook.write_text(content, encoding="utf-8")
        hook.chmod(0o755)
        return hook

    def run_hook(
        self, name: str, repository: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.hook(name, repository)), *arguments],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_hook_resolves_current_worktree_at_runtime(self) -> None:
        linked = self.root / "linked worktree"
        self.git(
            self.repository,
            "worktree",
            "add",
            "-q",
            "-b",
            "linked",
            str(linked),
        )

        install_graph_hooks(self.repository, self.launcher)
        completed = self.run_hook("post-commit", linked)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.recorded.read_text(encoding="utf-8").splitlines(),
            [str(linked)],
        )

    def test_install_preserves_unrelated_hook_content(self) -> None:
        hook = self.write_hook("post-commit", "#!/bin/sh\necho user-hook\n")

        install_graph_hooks(self.repository, self.launcher)

        content = hook.read_text(encoding="utf-8")
        self.assertIn("echo user-hook", content)
        self.assertIn("# orichum-graph-hook-start", content)
        self.assertEqual(stat.S_IMODE(hook.stat().st_mode) & stat.S_IXUSR, stat.S_IXUSR)

    def test_install_is_idempotent(self) -> None:
        install_graph_hooks(self.repository, self.launcher)
        first = self.hook("post-commit").read_bytes()

        install_graph_hooks(self.repository, self.launcher)

        self.assertEqual(self.hook("post-commit").read_bytes(), first)
        self.assertEqual(
            first.count(b"# orichum-graph-hook-start"),
            1,
        )

    def test_post_checkout_hook_uses_the_runtime_checkout(self) -> None:
        install_graph_hooks(self.repository, self.launcher)

        completed = self.run_hook(
            "post-checkout",
            self.repository,
            "0" * 40,
            "1" * 40,
            "1",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.recorded.read_text(encoding="utf-8").strip(),
            str(self.repository),
        )

    def test_install_removes_only_complete_upstream_marker_sections(self) -> None:
        commit = self.write_hook(
            "post-commit",
            "#!/bin/sh\n"
            "echo before\n"
            "# graphify-hook-start\n"
            "echo old-graphify\n"
            "# graphify-hook-end\n"
            "echo after\n",
        )
        checkout = self.write_hook(
            "post-checkout",
            "#!/bin/sh\n"
            "# graphify-checkout-hook-start\n"
            "echo old-checkout\n"
            "# graphify-checkout-hook-end\n"
            "echo user-checkout\n",
        )

        install_graph_hooks(self.repository, self.launcher)

        commit_content = commit.read_text(encoding="utf-8")
        checkout_content = checkout.read_text(encoding="utf-8")
        self.assertNotIn("old-graphify", commit_content)
        self.assertNotIn("old-checkout", checkout_content)
        self.assertIn("echo before", commit_content)
        self.assertIn("echo after", commit_content)
        self.assertIn("echo user-checkout", checkout_content)

    def test_marker_text_inside_user_command_is_preserved(self) -> None:
        hook = self.write_hook(
            "post-commit",
            "#!/bin/sh\n"
            "echo '# graphify-hook-end is documentation'\n",
        )

        install_graph_hooks(self.repository, self.launcher)

        self.assertIn(
            "echo '# graphify-hook-end is documentation'",
            hook.read_text(encoding="utf-8"),
        )

    def test_malformed_upstream_marker_is_refused_without_rewriting_hook(self) -> None:
        hook = self.write_hook(
            "post-commit",
            "#!/bin/sh\n# graphify-hook-start\necho incomplete\n",
        )
        original = hook.read_bytes()

        with self.assertRaisesRegex(GraphHookError, "marker"):
            install_graph_hooks(self.repository, self.launcher)

        self.assertEqual(hook.read_bytes(), original)

    def test_merge_driver_cleanup_requires_matching_driver_and_attribute(self) -> None:
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        attributes.write_text(
            "*.lock merge=ours\n"
            "graphify-out/graph.json merge=graphify\n",
            encoding="utf-8",
        )
        install_graph_hooks(self.repository, self.launcher)

        remove_upstream_graphify_hooks(self.repository)

        driver = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "config",
                "--get",
                "merge.graphify.driver",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(driver.returncode, 1)
        self.assertEqual(
            attributes.read_text(encoding="utf-8"),
            "*.lock merge=ours\n",
        )

    def test_merge_driver_cleanup_preserves_mismatched_registration(self) -> None:
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "custom graph merger",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "custom-merge %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        original = "graphify-out/graph.json merge=graphify\n"
        attributes.write_text(original, encoding="utf-8")
        install_graph_hooks(self.repository, self.launcher)

        remove_upstream_graphify_hooks(self.repository)

        self.assertEqual(
            self.git(
                self.repository,
                "config",
                "--get",
                "merge.graphify.driver",
            ),
            "custom-merge %O %A %B",
        )
        self.assertEqual(attributes.read_text(encoding="utf-8"), original)

    def test_merge_cleanup_refuses_an_unmanaged_repository(self) -> None:
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        original = "graphify-out/graph.json merge=graphify\n"
        attributes.write_text(original, encoding="utf-8")

        with self.assertRaisesRegex(GraphHookError, "not managed"):
            remove_upstream_graphify_hooks(self.repository)

        self.assertEqual(attributes.read_text(encoding="utf-8"), original)
        self.assertEqual(
            self.git(
                self.repository,
                "config",
                "--get",
                "merge.graphify.driver",
            ),
            "graphify merge-driver %O %A %B",
        )

    def test_forged_orichum_markers_do_not_authorize_merge_cleanup(self) -> None:
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        original = b"graphify-out/graph.json merge=graphify\n"
        attributes.write_bytes(original)
        for name in ("post-commit", "post-checkout"):
            self.write_hook(
                name,
                "#!/bin/sh\n"
                "# orichum-graph-hook-start\n"
                "echo foreign-command\n"
                "# orichum-graph-hook-end\n",
            )

        self.assertNotEqual(graph_hook_status(self.repository), "installed")
        with self.assertRaisesRegex(GraphHookError, "not managed"):
            remove_upstream_graphify_hooks(self.repository)

        self.assertEqual(attributes.read_bytes(), original)
        self.assertEqual(
            self.git(
                self.repository,
                "config",
                "--get",
                "merge.graphify.driver",
            ),
            "graphify merge-driver %O %A %B",
        )

    def test_status_requires_executable_hooks_and_live_launcher(self) -> None:
        install_graph_hooks(self.repository, self.launcher)
        hook = self.hook("post-commit")

        hook.chmod(0o644)
        self.assertNotEqual(graph_hook_status(self.repository), "installed")
        hook.chmod(0o755)
        self.launcher.rename(self.root / "stale-orichum")
        self.assertNotEqual(graph_hook_status(self.repository), "installed")

    def test_status_rejects_empty_and_foreign_managed_blocks(self) -> None:
        for command in ("", "echo forged"):
            with self.subTest(command=command):
                for name in ("post-commit", "post-checkout"):
                    self.write_hook(
                        name,
                        "#!/bin/sh\n"
                        "# orichum-graph-hook-start\n"
                        f"{command}\n"
                        "# orichum-graph-hook-end\n",
                    )
                self.assertNotEqual(
                    graph_hook_status(self.repository),
                    "installed",
                )

    def test_status_rejects_hooks_with_different_launchers(self) -> None:
        install_graph_hooks(self.repository, self.launcher)
        alternate = self.root / "alternate-orichum"
        alternate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        alternate.chmod(0o755)
        checkout = self.hook("post-checkout")
        checkout.write_text(
            checkout.read_text(encoding="utf-8").replace(
                str(self.launcher),
                str(alternate),
            ),
            encoding="utf-8",
        )

        self.assertNotEqual(graph_hook_status(self.repository), "installed")

    def test_status_and_cleanup_require_canonical_command_serialization(
        self,
    ) -> None:
        install_graph_hooks(self.repository, self.launcher)
        canonical = f"'{self.launcher}' graph hook-update \"$PWD\""
        self.assertEqual(graph_hook_status(self.repository), "installed")
        self.assertIn(
            f"\n{canonical}\n",
            self.hook("post-commit").read_text(encoding="utf-8"),
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        original_attributes = b"graphify-out/graph.json merge=graphify\n"
        attributes.write_bytes(original_attributes)
        variants = (
            canonical.replace('"$PWD"', "'$PWD'"),
            canonical.replace('"$PWD"', r"\$PWD"),
            canonical.replace('"$PWD"', "$PWD"),
            f"{canonical}; echo forged",
            canonical.replace(" graph ", "  graph "),
            f"{canonical} ",
        )

        for command in variants:
            with self.subTest(command=command):
                for name in ("post-commit", "post-checkout"):
                    self.write_hook(
                        name,
                        "#!/bin/sh\n"
                        "# orichum-graph-hook-start\n"
                        f"{command}\n"
                        "# orichum-graph-hook-end\n",
                    )
                self.assertNotEqual(
                    graph_hook_status(self.repository),
                    "installed",
                )
                with self.assertRaisesRegex(GraphHookError, "not managed"):
                    remove_upstream_graphify_hooks(self.repository)
                self.assertEqual(
                    attributes.read_bytes(),
                    original_attributes,
                )
                self.assertEqual(
                    self.git(
                        self.repository,
                        "config",
                        "--get",
                        "merge.graphify.driver",
                    ),
                    "graphify merge-driver %O %A %B",
                )

    def test_attribute_cleanup_preserves_unrelated_bytes_and_line_endings(
        self,
    ) -> None:
        self.git(
            self.repository,
            "config",
            "merge.graphify.name",
            "graphify graph.json union merge",
        )
        self.git(
            self.repository,
            "config",
            "merge.graphify.driver",
            "graphify merge-driver %O %A %B",
        )
        attributes = self.repository / ".gitattributes"
        attributes.write_bytes(
            b"*.txt  text eol=crlf\r\n"
            b"graphify-out/graph.json merge=graphify\r\n"
            b"*.lock   merge=ours"
        )

        install_graph_hooks(self.repository, self.launcher)

        self.assertEqual(
            attributes.read_bytes(),
            b"*.txt  text eol=crlf\r\n*.lock   merge=ours",
        )

    def test_linked_worktrees_share_one_default_hook_installation(self) -> None:
        linked = self.root / "linked"
        self.git(
            self.repository,
            "worktree",
            "add",
            "-q",
            "-b",
            "shared-hooks",
            str(linked),
        )

        install_graph_hooks(self.repository, self.launcher)
        install_graph_hooks(linked, self.launcher)

        self.assertEqual(
            self.hook("post-commit", self.repository).resolve(),
            self.hook("post-commit", linked).resolve(),
        )
        self.assertEqual(
            self.hook("post-commit").read_text(encoding="utf-8").count(
                "# orichum-graph-hook-start"
            ),
            1,
        )

    def test_install_refuses_symlink_hook_without_touching_target(self) -> None:
        outside = self.root / "outside-hook"
        outside.write_text("#!/bin/sh\necho outside\n", encoding="utf-8")
        hook = self.hook("post-commit")
        hook.symlink_to(outside)

        with self.assertRaisesRegex(GraphHookError, "unsafe"):
            install_graph_hooks(self.repository, self.launcher)

        self.assertEqual(
            outside.read_text(encoding="utf-8"),
            "#!/bin/sh\necho outside\n",
        )

    def test_install_refuses_world_writable_hook_directory(self) -> None:
        hooks = self.hooks_dir()
        original_mode = stat.S_IMODE(hooks.stat().st_mode)
        self.addCleanup(hooks.chmod, original_mode)
        hooks.chmod(0o777)

        with self.assertRaisesRegex(GraphHookError, "unsafe"):
            install_graph_hooks(self.repository, self.launcher)

        self.assertFalse(self.hook("post-commit").exists())

    def test_status_reports_only_complete_safe_installation(self) -> None:
        self.assertEqual(graph_hook_status(self.repository), "missing")
        install_graph_hooks(self.repository, self.launcher)
        self.assertEqual(graph_hook_status(self.repository), "installed")
        self.hook("post-checkout").write_text(
            "#!/bin/sh\n# orichum-graph-hook-start\n",
            encoding="utf-8",
        )
        self.assertEqual(graph_hook_status(self.repository), "unsafe")

    def test_detached_update_returns_promptly_and_logs_output(self) -> None:
        data_root = self.root / "data"
        data_root.mkdir(mode=0o700)
        command = [
            sys.executable,
            "-c",
            "import time; time.sleep(0.5); print('detached-complete')",
        ]

        started = time.monotonic()
        log = _launch_detached_update(self.repository, data_root, command)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.4)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and "detached-complete" in log.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.05)
        else:
            self.fail("detached update did not append its output")
        self.assertEqual(log.parent, data_root / "graphs" / "logs")

    def test_log_rotation_retains_only_active_and_previous(self) -> None:
        data_root = self.root / "data"
        data_root.mkdir(mode=0o700)
        log = _graph_log_path(self.repository, data_root)
        graphs = data_root / "graphs"
        graphs.mkdir(mode=0o700)
        log.parent.mkdir(mode=0o700)
        log.write_bytes(b"x" * (1024 * 1024 + 1))
        log.chmod(0o600)
        previous = log.with_name(f"{log.name}.previous")
        previous.write_text("older\n", encoding="utf-8")
        previous.chmod(0o600)

        launched = _launch_detached_update(
            self.repository,
            data_root,
            [sys.executable, "-c", "print('new-active')"],
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if launched.exists() and "new-active" in launched.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.05)
        self.assertEqual(previous.stat().st_size, 1024 * 1024 + 1)
        self.assertEqual(
            sorted(path.name for path in log.parent.iterdir()),
            sorted((log.name, previous.name, f"{log.name}.lock")),
        )

    def test_concurrent_updates_serialize_rotation_and_log_placement(self) -> None:
        data_root = self.root / "serialized-data"
        data_root.mkdir(mode=0o700)
        log = _graph_log_path(self.repository, data_root)
        graphs = data_root / "graphs"
        graphs.mkdir(mode=0o700)
        log.parent.mkdir(mode=0o700)
        log.write_bytes(b"x" * (1024 * 1024 + 1))
        log.chmod(0o600)
        lock = log.with_name(f"{log.name}.lock")
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)

        _launch_detached_update(
            self.repository,
            data_root,
            [
                sys.executable,
                "-c",
                "import time; time.sleep(0.2); print('first-update')",
            ],
        )
        _launch_detached_update(
            self.repository,
            data_root,
            [sys.executable, "-c", "print('second-update')"],
        )
        time.sleep(0.3)
        self.assertEqual(log.stat().st_size, 1024 * 1024 + 1)
        self.assertFalse(log.with_name(f"{log.name}.previous").exists())

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            active = log.read_bytes() if log.exists() else b""
            if b"first-update" in active and b"second-update" in active:
                break
            time.sleep(0.05)
        else:
            self.fail("serialized updates were skipped or logged elsewhere")
        previous = log.with_name(f"{log.name}.previous")
        self.assertEqual(previous.stat().st_size, 1024 * 1024 + 1)
        self.assertNotIn(b"update", previous.read_bytes())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_successful_graph_activation_installs_orichum_hooks(self) -> None:
        self.git(
            self.repository,
            "remote",
            "add",
            "origin",
            "https://github.com/example/repository.git",
        )
        data_root = self.root / "activation-data"
        data_root.mkdir(mode=0o700)
        graphify = self.root / "fake-graphify"
        graphify.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "output = Path(os.environ['GRAPHIFY_OUT'])\n"
            "output.mkdir(parents=True, exist_ok=True)\n"
            "(output / 'graph.json').write_text("
            "json.dumps({'nodes': [{'id': 'node', 'source_file': 'tracked.txt'}]}),"
            " encoding='utf-8')\n",
            encoding="utf-8",
        )
        graphify.chmod(0o755)

        sync_graph(self.repository, data_root, graphify=str(graphify))

        self.assertEqual(graph_hook_status(self.repository), "installed")
        self.assertIn(
            str(Path(__file__).resolve().parents[1] / "bin" / "orichum"),
            self.hook("post-commit").read_text(encoding="utf-8"),
        )

    def test_hidden_hook_update_detaches_a_bounded_graph_refresh(self) -> None:
        self.git(
            self.repository,
            "remote",
            "add",
            "origin",
            "https://github.com/example/repository.git",
        )
        data_root = self.root / "hook-data"
        data_root.mkdir(mode=0o700)
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        graphify = fake_bin / "graphify"
        graphify.write_text(
            "#!/bin/sh\n"
            "echo hook-refresh-started >&2\n"
            "exit 7\n",
            encoding="utf-8",
        )
        graphify.chmod(0o755)
        environment = {
            "ORICHUM_DATA_HOME": str(data_root),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }

        started = time.monotonic()
        with mock.patch.dict(os.environ, environment, clear=False):
            return_code = graph_main(["hook-update", str(self.repository)])
        elapsed = time.monotonic() - started

        self.assertEqual(return_code, 0)
        self.assertLess(elapsed, 1.0)
        log = _graph_log_path(self.repository, data_root)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if log.exists() and "hook-refresh-started" in log.read_text(
                encoding="utf-8"
            ):
                break
            time.sleep(0.05)
        else:
            self.fail("hidden hook refresh did not run in the detached child")


if __name__ == "__main__":
    unittest.main()
