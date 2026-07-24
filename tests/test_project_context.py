#!/usr/bin/env python3
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from integrations.common import project_context
from integrations.common.project_context import ContextError, load_config, resolve_context


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.xebia = self.root / "xebia"
        self.complion = self.root / "complion"
        self.xebia_repo = self.xebia / "repo"
        self.complion_repo = self.complion / "nested" / "repo"
        self.palace = self.root / "palaces" / "xebia"
        self.complion_palace = self.root / "palaces" / "complion"
        for directory in (
            self.xebia_repo,
            self.complion_repo,
            self.palace,
            self.complion_palace,
            self.root / "elsewhere",
            self.root / "xebia-old",
        ):
            directory.mkdir(parents=True)
        os.chmod(self.palace, 0o700)
        os.chmod(self.complion_palace, 0o700)
        self.config = {
            "contexts": [
                {
                    "root": str(self.xebia),
                    "dockerProfile": "xebia",
                    "memoryPalace": str(self.palace),
                    "memoryWing": "xebia",
                },
                {
                    "root": str(self.complion),
                    "dockerProfile": "realtime",
                    "memoryPalace": str(self.complion_palace),
                    "memoryWing": "complion",
                },
            ],
        }
        self.config_path = self.write_config(self.config)

    def tearDown(self):
        os.chmod(self.palace, 0o700)
        os.chmod(self.complion_palace, 0o700)
        self.temporary_directory.cleanup()

    def write_config(self, payload):
        path = self.root / f"config-{len(list(self.root.glob('config-*.json')))}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def resolve(self, launch_dir, payload=None):
        config_path = self.config_path if payload is None else self.write_config(payload)
        return resolve_context(load_config(config_path, home=self.root), launch_dir)

    def init_git(self, path):
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )

    def assert_memory_failure(self, result, code, repo_root):
        self.assertEqual(result["route"]["id"], "xebia")
        self.assertEqual(result["route"]["contextRootReal"], str(self.xebia))
        self.assertEqual(result["route"]["dockerProfile"], "xebia")
        self.assertEqual(result["route"]["memoryWing"], "xebia")
        self.assertFalse(result["route"]["memoryAvailable"])
        self.assertEqual(result["route"]["memoryFailureCode"], code)
        self.assertIsNone(result["route"]["palacePathReal"])
        self.assertEqual(result["repoRootReal"], str(repo_root))

    def test_longest_component_boundary_and_unmapped(self):
        xebia = self.resolve(self.xebia / "repo")["route"]
        self.assertEqual(xebia["id"], "xebia")
        self.assertEqual(xebia["contextRootReal"], str(self.xebia))
        self.assertEqual(xebia["dockerProfile"], "xebia")

        complion = self.resolve(self.complion / "nested" / "repo")["route"]
        self.assertEqual(complion["id"], "complion")
        self.assertEqual(complion["contextRootReal"], str(self.complion))
        self.assertEqual(complion["dockerProfile"], "realtime")
        self.assertIsNone(self.resolve(self.root / "xebia-old")["route"])
        self.assertIsNone(self.resolve(self.root / "elsewhere")["route"])

    def test_resolver_selects_the_deepest_matching_normalized_root(self):
        nested_palace = self.root / "palaces" / "nested"
        nested_palace.mkdir()
        nested_palace.chmod(0o700)
        normalized = {
            "contexts": [
                {
                    "root": self.xebia,
                    "dockerProfile": "xebia",
                    "modelStack": None,
                    "memoryPalace": self.palace,
                    "memoryWing": "xebia",
                },
                {
                    "root": self.xebia_repo,
                    "dockerProfile": "nested",
                    "modelStack": None,
                    "memoryPalace": nested_palace,
                    "memoryWing": "nested",
                },
            ]
        }

        result = resolve_context(normalized, self.xebia_repo)

        self.assertEqual(result["route"]["id"], "nested")
        self.assertEqual(
            result["route"]["contextRootReal"], str(self.xebia_repo)
        )
        self.assertEqual(result["route"]["dockerProfile"], "nested")

    def test_overlapping_canonical_roots_fail_closed(self):
        nested = json.loads(json.dumps(self.config))
        nested["contexts"].append(
            {
                "root": str(self.xebia_repo),
                "dockerProfile": "nested-profile",
                "memoryPalace": str(self.root / "palaces" / "nested"),
                "memoryWing": "nested-wing",
            }
        )
        (self.root / "palaces" / "nested").mkdir()
        os.chmod(self.root / "palaces" / "nested", 0o700)
        with self.assertRaises(ContextError):
            self.resolve(self.xebia_repo, nested)

    def test_symlink_uses_physical_target(self):
        link = self.root / "linked-repo"
        link.symlink_to(self.xebia / "repo", target_is_directory=True)
        result = self.resolve(link)
        self.assertEqual(result["launchDirReal"], str((self.xebia / "repo").resolve()))
        self.assertEqual(result["route"]["id"], "xebia")

    def test_duplicate_roots_palaces_or_wings_fail_closed(self):
        for field in ("root", "memoryPalace", "memoryWing"):
            bad = json.loads(json.dumps(self.config))
            bad["contexts"][1][field] = bad["contexts"][0][field]
            with self.subTest(field=field), self.assertRaises(ContextError):
                load_config(self.write_config(bad), home=self.root)

    def test_duplicate_canonical_symlink_roots_fail_closed(self):
        linked_root = self.root / "linked-xebia"
        linked_root.symlink_to(self.xebia, target_is_directory=True)
        bad = json.loads(json.dumps(self.config))
        bad["contexts"][1]["root"] = str(linked_root)
        with self.assertRaises(ContextError):
            load_config(self.write_config(bad), home=self.root)

    def test_canonical_home_or_filesystem_root_is_rejected(self):
        for target in (self.root, Path("/")):
            with self.subTest(target=target):
                linked_root = self.root / f"unsafe-{len(list(self.root.glob('unsafe-*')))}"
                linked_root.symlink_to(target, target_is_directory=True)
                bad = json.loads(json.dumps(self.config))
                bad["contexts"] = [bad["contexts"][0]]
                bad["contexts"][0]["root"] = str(linked_root)
                with self.assertRaises(ContextError):
                    load_config(self.write_config(bad), home=self.root)

    def test_invalid_mapped_palace_disables_only_memory(self):
        os.chmod(self.palace, 0o750)
        result = self.resolve(self.xebia)
        self.assertEqual(result["route"]["id"], "xebia")
        self.assertFalse(result["route"]["memoryAvailable"])
        self.assertEqual(result["route"]["memoryFailureCode"], "palace_permissions")
        self.assertIsNone(result["route"]["palacePathReal"])
        os.chmod(self.palace, 0o700)
        palace_link = self.root / "palace-link"
        palace_link.symlink_to(self.palace, target_is_directory=True)
        bad = json.loads(json.dumps(self.config))
        bad["contexts"][0]["memoryPalace"] = str(palace_link)
        linked = self.resolve(self.xebia, bad)
        self.assertFalse(linked["route"]["memoryAvailable"])
        self.assertEqual(linked["route"]["memoryFailureCode"], "palace_symlink")
        self.assertEqual(linked["route"]["dockerProfile"], "xebia")

    def test_all_bounded_palace_failures_preserve_route_and_git_root(self):
        self.init_git(self.xebia_repo)
        nested = self.xebia_repo / "nested"
        nested.mkdir()

        missing = json.loads(json.dumps(self.config))
        missing_path = self.root / "missing-palace"
        missing["contexts"][0]["memoryPalace"] = str(missing_path)
        missing_result = self.resolve(nested, missing)
        self.assert_memory_failure(missing_result, "palace_missing", self.xebia_repo)
        self.assertNotIn(str(missing_path), json.dumps(missing_result))

        not_directory_path = self.root / "palace-file"
        not_directory_path.write_text("not a directory", encoding="utf-8")
        not_directory = json.loads(json.dumps(self.config))
        not_directory["contexts"][0]["memoryPalace"] = str(not_directory_path)
        self.assert_memory_failure(
            self.resolve(nested, not_directory),
            "palace_not_directory",
            self.xebia_repo,
        )

        original_stat = Path.stat

        def wrong_owner(path, *args, **kwargs):
            result = original_stat(path, *args, **kwargs)
            if path == self.palace:
                return SimpleNamespace(st_mode=result.st_mode, st_uid=os.getuid() + 1)
            return result

        with mock.patch.object(Path, "stat", autospec=True, side_effect=wrong_owner):
            owner_result = self.resolve(nested)
        self.assert_memory_failure(owner_result, "palace_owner", self.xebia_repo)

        original_resolve = Path.resolve

        def inaccessible(path, *args, **kwargs):
            if path == self.palace:
                raise PermissionError("fixture denial must not escape")
            return original_resolve(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", autospec=True, side_effect=inaccessible):
            inaccessible_result = self.resolve(nested)
        self.assert_memory_failure(
            inaccessible_result,
            "palace_inaccessible",
            self.xebia_repo,
        )
        self.assertNotIn("fixture denial", json.dumps(inaccessible_result))

        os.chmod(self.palace, 0o750)
        permissions_result = self.resolve(nested)
        self.assert_memory_failure(
            permissions_result,
            "palace_permissions",
            self.xebia_repo,
        )
        os.chmod(self.palace, 0o700)

        palace_link = self.root / "palace-component-link"
        palace_link.symlink_to(self.palace, target_is_directory=True)
        linked = json.loads(json.dumps(self.config))
        linked["contexts"][0]["memoryPalace"] = str(palace_link)
        self.assert_memory_failure(
            self.resolve(nested, linked),
            "palace_symlink",
            self.xebia_repo,
        )

    def test_git_root_is_independent_physical_and_optional(self):
        self.init_git(self.xebia_repo)
        nested = self.xebia_repo / "nested" / "deeper"
        nested.mkdir(parents=True)
        result = self.resolve(nested)
        self.assertEqual(result["repoRootReal"], str(self.xebia_repo.resolve(strict=True)))

        self.assertIsNone(self.resolve(self.xebia)["repoRootReal"])

        unmapped_repo = self.root / "unmapped-repo"
        unmapped_nested = unmapped_repo / "nested"
        unmapped_nested.mkdir(parents=True)
        self.init_git(unmapped_repo)
        unmapped = self.resolve(unmapped_nested)
        self.assertIsNone(unmapped["route"])
        self.assertEqual(unmapped["repoRootReal"], str(unmapped_repo.resolve(strict=True)))

    def test_unmapped_route_never_validates_palace(self):
        with mock.patch.object(
            project_context,
            "_validate_palace_candidate",
            side_effect=AssertionError("palace validation was called"),
        ) as validate:
            result = self.resolve(self.root / "elsewhere")
        validate.assert_not_called()
        self.assertIsNone(result["route"])

    def test_successful_route_exposes_only_canonical_palace(self):
        result = self.resolve(self.complion_repo)
        self.assertEqual(
            result,
            {
                "schemaVersion": 1,
                "launchDirReal": str(self.complion_repo),
                "repoRootReal": None,
                "route": {
                    "id": "complion",
                    "contextRootReal": str(self.complion),
                    "dockerProfile": "realtime",
                    "modelStack": None,
                    "memoryWing": "complion",
                    "memoryAvailable": True,
                    "memoryFailureCode": None,
                    "palacePathReal": str(self.complion_palace),
                },
            },
        )

    def test_legacy_context_without_model_stack_inherits_default(self):
        config = load_config(self.config_path, home=self.root)
        self.assertIsNone(config["contexts"][0]["modelStack"])

    def test_resolved_route_carries_explicit_model_stack(self):
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        document["contexts"][0]["modelStack"] = "xebia"
        self.config_path.write_text(json.dumps(document), encoding="utf-8")

        route = resolve_context(
            load_config(self.config_path, home=self.root), self.xebia_repo
        )["route"]

        self.assertEqual(route["modelStack"], "xebia")

    def test_configuration_schema_is_closed_and_paths_are_strict(self):
        invalid_payloads = []

        for key in ("contexts",):
            missing = json.loads(json.dumps(self.config))
            del missing[key]
            invalid_payloads.append(missing)
        extra = json.loads(json.dumps(self.config))
        extra["extra"] = True
        invalid_payloads.append(extra)

        for key in ("root", "dockerProfile", "memoryPalace", "memoryWing"):
            missing = json.loads(json.dumps(self.config))
            del missing["contexts"][0][key]
            invalid_payloads.append(missing)
        extra_context = json.loads(json.dumps(self.config))
        extra_context["contexts"][0]["extra"] = True
        invalid_payloads.append(extra_context)

        for key in ("dockerProfile", "memoryWing"):
            blank = json.loads(json.dumps(self.config))
            blank["contexts"][0][key] = "  "
            invalid_payloads.append(blank)

        relative_palace = json.loads(json.dumps(self.config))
        relative_palace["contexts"][0]["memoryPalace"] = "relative/palace"
        invalid_payloads.append(relative_palace)

        relative_root = json.loads(json.dumps(self.config))
        relative_root["contexts"][0]["root"] = "relative/root"
        invalid_payloads.append(relative_root)

        missing_root = json.loads(json.dumps(self.config))
        missing_root["contexts"][0]["root"] = str(self.root / "missing-root")
        invalid_payloads.append(missing_root)

        root_file = self.root / "root-file"
        root_file.write_text("not a directory", encoding="utf-8")
        file_root = json.loads(json.dumps(self.config))
        file_root["contexts"][0]["root"] = str(root_file)
        invalid_payloads.append(file_root)

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ContextError):
                load_config(self.write_config(payload), home=self.root)

    def test_tilde_paths_expand_against_explicit_home(self):
        tilde = json.loads(json.dumps(self.config))
        tilde["contexts"][0]["memoryPalace"] = "~/palaces/xebia"
        tilde["contexts"][1]["memoryPalace"] = "~/palaces/complion"
        tilde["contexts"][0]["root"] = "~/xebia"
        tilde["contexts"][1]["root"] = "~/complion"
        result = self.resolve(self.xebia_repo, tilde)
        self.assertEqual(result["route"]["contextRootReal"], str(self.xebia))
        self.assertEqual(result["route"]["palacePathReal"], str(self.palace))

    def test_launch_directory_must_resolve_strictly(self):
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / "missing-launch")

    def test_cli_writes_canonical_atomic_private_output(self):
        output = self.root / "context-output.json"
        output.write_text("old contents", encoding="utf-8")
        os.chmod(output, 0o644)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "--config",
                str(self.config_path),
                "--launch-dir",
                str(self.complion_repo),
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        payload = json.loads(output.read_text(encoding="utf-8"))
        expected = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        self.assertEqual(output.read_text(encoding="utf-8"), expected)
        self.assertEqual(list(self.root.glob(f".{output.name}.*")), [])


class ContextCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.tool_directory = self.root / "fake-tools"
        self.tool_directory.mkdir()
        self.fake_package_directory = self.root / "fake-packages"
        fake_mempalace = self.fake_package_directory / "mempalace"
        fake_mempalace.mkdir(parents=True)
        (fake_mempalace / "__init__.py").write_text("", encoding="utf-8")
        (fake_mempalace / "palace.py").write_text(
            "SKIP_DIRS = set()\n", encoding="utf-8"
        )
        self.tool_calls_path = self.root / "fake-tool-calls.jsonl"
        for name in ("mempalace", "graphify"):
            tool = self.tool_directory / name
            tool.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
with Path(os.environ["FAKE_TOOL_CALLS"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps({"tool": tool, "args": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")

if tool == "mempalace":
    palace = Path(sys.argv[sys.argv.index("--palace") + 1])
    if "mine" in sys.argv:
        (palace / "population-artifact").write_text("populated", encoding="utf-8")
    print("raw mempalace success output")
    raise SystemExit(0)

operation = sys.argv[1]
if operation in {"extract", "update"}:
    print("raw graphify success output")
    repository = Path(sys.argv[2])
    failure = os.environ.get("FAKE_GRAPHIFY_FAIL")
    if failure and failure in {"1", repository.name}:
        print("fixture graphify failure", file=sys.stderr)
        raise SystemExit(1)
    graph = repository / "graphify-out" / "graph.json"
    graph.parent.mkdir(exist_ok=True)
    graph.write_text(json.dumps({"nodes": [{"id": "fixture"}]}), encoding="utf-8")
if operation == "hook" and sys.argv[2] == "status" and os.environ.get("FAKE_CONFIG_MUTATION"):
    config = Path(os.environ["FAKE_CONFIG_MUTATION"])
    root = Path(os.environ["FAKE_CONFIG_ROOT"])
    config.write_text(json.dumps({"contexts": [{
        "root": str(root),
        "dockerProfile": "concurrent",
        "memoryPalace": str(root.parent / "concurrent-palace"),
        "memoryWing": "concurrent",
    }]}) + "\\n", encoding="utf-8")
raise SystemExit(0)
""",
                encoding="utf-8",
            )
            tool.chmod(0o755)
        self.config_path = self.root / "project-context.json"
        self.config_path.write_text('{\n  "contexts": []\n}\n', encoding="utf-8")
        os.chmod(self.config_path, 0o640)
        self.routing_path = self.root / "model-routing.json"
        self.routing_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "defaultStack": "balanced",
                    "stacks": {
                        "balanced": {
                            "controller": "controller-balanced",
                            "agents": {
                                "repository-explorer": ["explorer-balanced"],
                                "repository-verifier": ["verifier-balanced"],
                                "correctness-critic": ["critic-balanced"],
                                "architecture-advisor": ["advisor-balanced"],
                                "implementation-worker": ["worker-balanced"],
                            },
                        },
                        "xebia": {
                            "controller": "controller-xebia",
                            "agents": {
                                "repository-explorer": ["explorer-xebia"],
                                "repository-verifier": ["verifier-xebia"],
                                "correctness-critic": ["critic-xebia"],
                                "architecture-advisor": ["advisor-xebia"],
                                "implementation-worker": ["worker-xebia"],
                            },
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.providers_path = self.root / "providers.json"
        self.providers_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "providers": {},
                    "accountPools": {
                        "docker-dev": {"providers": []},
                        "shared": {"providers": []},
                    },
                    "fallbackRoutes": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.focused_routing_path = self.root / "model-stacks.json"
        focused_routing = json.loads(self.routing_path.read_text(encoding="utf-8"))
        focused_routing["models"] = {}
        self.focused_routing_path.write_text(
            json.dumps(focused_routing) + "\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_context(self, *arguments, input_text=None, environment=None):
        command_environment = os.environ.copy()
        command_environment.update(
            {
                "HOME": str(self.root),
                "PATH": f"{self.tool_directory}{os.pathsep}{command_environment.get('PATH', '')}",
                "PYTHONPATH": str(self.fake_package_directory),
                "FAKE_TOOL_CALLS": str(self.tool_calls_path),
            }
        )
        if environment is not None:
            command_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "context",
                "--config",
                str(self.config_path),
                "--routing-config",
                str(self.routing_path),
                *arguments,
            ],
            cwd=REPO_ROOT,
            env=command_environment,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_focused_context(self, *arguments, input_text=None, environment=None):
        command_environment = os.environ.copy()
        command_environment.update(
            {
                "HOME": str(self.root),
                "PATH": f"{self.tool_directory}{os.pathsep}{command_environment.get('PATH', '')}",
                "PYTHONPATH": str(self.fake_package_directory),
                "FAKE_TOOL_CALLS": str(self.tool_calls_path),
            }
        )
        if environment is not None:
            command_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "context",
                "--config",
                str(self.config_path),
                "--routing-config",
                str(self.focused_routing_path),
                "--providers-config",
                str(self.providers_path),
                *arguments,
            ],
            cwd=REPO_ROOT,
            env=command_environment,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )

    def load_contexts(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))["contexts"]

    def write_contexts(self, contexts):
        self.config_path.write_text(
            json.dumps({"contexts": contexts}, indent=2) + "\n",
            encoding="utf-8",
        )

    def read_tool_calls(self):
        if not self.tool_calls_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.tool_calls_path.read_text(encoding="utf-8").splitlines()
        ]

    def init_git(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_add_defaults_lists_and_updates_a_context_with_readable_atomic_json(self):
        added = self.run_context("add", str(self.workspace), "--docker", "docker-dev")
        self.assertEqual(added.returncode, 0, added.stderr)
        expected_palace = self.root / ".mempalace" / "palaces" / "workspace"
        self.assertEqual(
            self.load_contexts(),
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "docker-dev",
                    "modelStack": None,
                    "memoryPalace": "~/.mempalace/palaces/workspace",
                    "memoryWing": "workspace",
                }
            ],
        )
        self.assertTrue(expected_palace.is_dir())
        self.assertEqual(stat.S_IMODE(expected_palace.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)
        self.assertEqual(
            self.config_path.read_text(encoding="utf-8"),
            json.dumps({"contexts": self.load_contexts()}, indent=2) + "\n",
        )
        self.assertEqual(list(self.root.glob(f".{self.config_path.name}.*")), [])

        listed = self.run_context("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("| PROJECT ROOT", listed.stdout)
        self.assertIn(
            "| MCP_DOCKER PROFILE | GITHUB ACCOUNT | MEMPALACE PATH",
            listed.stdout,
        )
        self.assertIn("docker-dev", listed.stdout)

        updated = self.run_context(
            "update", str(self.workspace), "--docker", "docker-prod", "--wing", "prod"
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(self.load_contexts()[0]["dockerProfile"], "docker-prod")
        self.assertEqual(self.load_contexts()[0]["memoryWing"], "prod")

    def test_focused_add_preserves_schema_and_assigns_ordered_account_pools(self):
        self.config_path.write_text(
            '{"schemaVersion":1,"contexts":[]}\n', encoding="utf-8"
        )

        added = self.run_focused_context(
            "add",
            str(self.workspace),
            "--docker",
            "docker-dev",
            "--github-account",
            "work-account",
        )

        self.assertEqual(added.returncode, 0, added.stderr)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(
            document["contexts"][0]["accountPools"],
            ["docker-dev", "shared"],
        )
        self.assertEqual(
            document["contexts"][0]["githubAccount"], "work-account"
        )

        updated = self.run_focused_context(
            "update",
            str(self.workspace),
            "--pool",
            "shared",
            "--no-docker",
            "--no-github-account",
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsNone(document["contexts"][0]["dockerProfile"])
        self.assertIsNone(document["contexts"][0]["githubAccount"])
        self.assertEqual(document["contexts"][0]["accountPools"], ["shared"])

    def test_focused_unknown_account_pool_fails_before_population(self):
        self.config_path.write_text(
            '{"schemaVersion":1,"contexts":[]}\n', encoding="utf-8"
        )

        rejected = self.run_focused_context(
            "add", str(self.workspace), "--pool", "missing"
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(
            json.loads(self.config_path.read_text(encoding="utf-8"))["contexts"],
            [],
        )
        self.assertEqual(self.read_tool_calls(), [])

    def test_add_rejects_unknown_model_stack_without_population(self):
        other_project = self.root / "other-project"
        other_project.mkdir()

        rejected = self.run_context(
            "add",
            str(other_project),
            "--model-stack",
            "missing",
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.load_contexts(), [])
        self.assertEqual(self.read_tool_calls(), [])

    def test_add_persists_explicit_model_stack_with_one_population(self):
        added = self.run_context(
            "add",
            str(self.workspace),
            "--model-stack",
            "xebia",
        )

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(self.load_contexts()[0]["modelStack"], "xebia")
        mempalace_calls = [
            call for call in self.read_tool_calls() if call["tool"] == "mempalace"
        ]
        self.assertEqual(len(mempalace_calls), 2)

    def test_update_sets_and_inherits_model_stack_without_population(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "dev",
                    "memoryPalace": str(self.root / "palace"),
                    "memoryWing": "workspace",
                }
            ]
        )

        explicit = self.run_context(
            "update",
            str(self.workspace),
            "--model-stack",
            "xebia",
        )
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(self.load_contexts()[0]["modelStack"], "xebia")
        self.assertEqual(self.read_tool_calls(), [])

        inherited = self.run_context(
            "update",
            str(self.workspace),
            "--inherit-model-stack",
        )
        self.assertEqual(inherited.returncode, 0, inherited.stderr)
        self.assertIsNone(self.load_contexts()[0]["modelStack"])
        self.assertEqual(self.read_tool_calls(), [])

    def test_update_rejects_explicit_and_inherited_model_stack_together(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "dev",
                    "modelStack": None,
                    "memoryPalace": str(self.root / "palace"),
                    "memoryWing": "workspace",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        rejected = self.run_context(
            "update",
            str(self.workspace),
            "--model-stack",
            "xebia",
            "--inherit-model-stack",
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.read_tool_calls(), [])

    def test_validate_and_list_reject_persisted_undeclared_model_stack(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "dev",
                    "modelStack": "missing",
                    "memoryPalace": str(self.root / "palace"),
                    "memoryWing": "workspace",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        for command in ("validate", "list"):
            with self.subTest(command=command):
                rejected = self.run_context(command)
                self.assertEqual(rejected.returncode, 1)
                self.assertEqual(
                    self.config_path.read_text(encoding="utf-8"), original
                )
                self.assertEqual(self.read_tool_calls(), [])

    def test_mutation_candidate_cannot_preserve_undeclared_model_stack(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "dev",
                    "modelStack": "missing",
                    "memoryPalace": str(self.root / "palace"),
                    "memoryWing": "workspace",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        rejected = self.run_context(
            "update",
            str(self.workspace),
            "--docker",
            "next",
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.read_tool_calls(), [])

        repaired = self.run_context(
            "update",
            str(self.workspace),
            "--model-stack",
            "xebia",
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.load_contexts()[0]["modelStack"], "xebia")
        self.assertEqual(self.read_tool_calls(), [])

    def test_add_without_docker_omits_profile_and_renders_placeholder(self):
        added = self.run_context("add", str(self.workspace))

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIsNone(self.load_contexts()[0]["dockerProfile"])

        listed = self.run_context("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("| —", listed.stdout)

    def test_add_populates_before_committing_mapping(self):
        added = self.run_context("add", str(self.workspace), "--docker", "dev")

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(len(self.load_contexts()), 1)
        self.assertIn(f"[discover] scanning {self.workspace}", added.stdout)
        self.assertIn("[discover] found 0 repositories", added.stdout)
        self.assertIn("[mempalace] mining", added.stdout)
        self.assertIn("[mempalace] verifying store", added.stdout)
        self.assertIn("MemPalace: populated wing workspace", added.stdout)
        self.assertNotIn("raw mempalace success output", added.stdout)
        self.assertNotIn("raw graphify success output", added.stdout)

    def test_population_failure_does_not_commit_new_mapping(self):
        self.init_git(self.workspace)

        failed = self.run_context(
            "add",
            str(self.workspace),
            "--docker",
            "dev",
            environment={"FAKE_GRAPHIFY_FAIL": "1"},
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.load_contexts(), [])
        self.assertEqual(
            failed.stderr.splitlines()[0],
            "ERROR: project context operation rejected",
        )
        self.assertIn("Graphify extract failed", failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)

    def test_later_repository_failure_keeps_completed_population_progress(self):
        first = self.workspace / "first"
        second = self.workspace / "second"
        self.init_git(first)
        self.init_git(second)

        failed = self.run_context(
            "add",
            str(self.workspace),
            "--docker",
            "dev",
            environment={"FAKE_GRAPHIFY_FAIL": "second"},
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.load_contexts(), [])
        self.assertIn("[mempalace] store verified", failed.stdout)
        self.assertIn(
            "[graphify 1/2] hooks installed and verified", failed.stdout
        )
        self.assertIn("[graphify 2/2] creating second", failed.stdout)
        self.assertNotIn(
            "[graphify 2/2] hooks installed and verified", failed.stdout
        )
        self.assertLessEqual(len(failed.stderr), 4_100)

    def test_final_revalidation_failure_keeps_all_population_progress(self):
        first = self.workspace / "first"
        second = self.workspace / "second"
        self.init_git(first)
        self.init_git(second)

        failed = self.run_context(
            "add",
            str(self.workspace),
            "--docker",
            "dev",
            environment={
                "FAKE_CONFIG_MUTATION": str(self.config_path),
                "FAKE_CONFIG_ROOT": str(self.workspace),
            },
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(
            self.load_contexts(),
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "concurrent",
                    "memoryPalace": str(self.root / "concurrent-palace"),
                    "memoryWing": "concurrent",
                }
            ],
        )
        self.assertIn("[mempalace] store verified", failed.stdout)
        self.assertIn(
            "[graphify 1/2] hooks installed and verified", failed.stdout
        )
        self.assertIn(
            "[graphify 2/2] hooks installed and verified", failed.stdout
        )
        self.assertNotIn("| REPOSITORY", failed.stdout)
        self.assertLessEqual(len(failed.stderr), 4_100)

    def test_populate_rejects_an_unconfigured_root(self):
        unconfigured = self.root / "unconfigured"
        unconfigured.mkdir()

        rejected = self.run_context("populate", str(unconfigured))

        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.load_contexts(), [])
        self.assertEqual(self.read_tool_calls(), [])
        self.assertEqual(
            rejected.stderr.splitlines()[0],
            "ERROR: project context operation rejected",
        )
        self.assertNotIn("usage:", rejected.stderr)

    def test_populate_uses_configured_route_without_rewriting_json(self):
        palace = self.root / "configured-palace"
        palace.mkdir(mode=0o700)
        alias = self.root / "workspace-alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "configured-docker",
                    "memoryPalace": str(palace),
                    "memoryWing": "configured-wing",
                }
            ]
        )
        original = self.config_path.read_bytes()
        original_inode = self.config_path.stat().st_ino

        populated = self.run_context("populate", str(alias))

        self.assertEqual(populated.returncode, 0, populated.stderr)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(self.config_path.stat().st_ino, original_inode)
        self.assertIn("MemPalace: populated wing configured-wing", populated.stdout)
        mempalace_calls = [
            call for call in self.read_tool_calls() if call["tool"] == "mempalace"
        ]
        self.assertEqual(
            mempalace_calls[0]["args"],
            [
                "--palace",
                str(palace),
                "mine",
                str(self.workspace),
                "--mode",
                "projects",
                "--wing",
                "configured-wing",
            ],
        )

    def test_populate_initializes_a_repository_created_after_add(self):
        added = self.run_context("add", str(self.workspace), "--docker", "dev")
        self.assertEqual(added.returncode, 0, added.stderr)
        repository = self.workspace / "later-repository"
        self.init_git(repository)

        populated = self.run_context("populate", str(self.workspace))

        self.assertEqual(populated.returncode, 0, populated.stderr)
        self.assertIn(str(repository), populated.stdout)
        self.assertTrue((repository / "graphify-out" / "graph.json").is_file())

    def test_palace_artifacts_survive_population_failure(self):
        self.init_git(self.workspace)
        palace = self.root / "failed-palace"

        failed = self.run_context(
            "add",
            str(self.workspace),
            "--docker",
            "dev",
            "--palace",
            str(palace),
            environment={"FAKE_GRAPHIFY_FAIL": "1"},
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.load_contexts(), [])
        self.assertEqual(
            (palace / "population-artifact").read_text(encoding="utf-8"),
            "populated",
        )

    def test_add_revalidates_after_concurrent_configuration_mutation(self):
        concurrent_context = {
            "root": str(self.workspace),
            "dockerProfile": "concurrent",
            "memoryPalace": str(self.root / "concurrent-palace"),
            "memoryWing": "concurrent",
        }

        def mutate_configuration(*_arguments, **_keywords):
            self.write_contexts([concurrent_context])
            return SimpleNamespace()

        with mock.patch.object(Path, "home", return_value=self.root), mock.patch.object(
            project_context, "_prepare_palace"
        ), mock.patch.object(
            project_context, "populate_context", side_effect=mutate_configuration
        ) as populate, mock.patch.object(
            project_context, "render_population_result", return_value="populated"
        ), contextlib.redirect_stderr(io.StringIO()):
            result = project_context.context_main(
                [
                    "--config",
                    str(self.config_path),
                    "--routing-config",
                    str(self.routing_path),
                    "add",
                    str(self.workspace),
                    "--docker",
                    "dev",
                ]
            )

        self.assertNotEqual(result, 0)
        self.assertEqual(self.load_contexts(), [concurrent_context])
        populate.assert_called_once_with(
            self.workspace,
            self.root / ".mempalace" / "palaces" / "workspace",
            "workspace",
            progress=project_context._print_population_progress,
        )

    def test_list_renders_a_bordered_table_with_dynamic_equal_width_lines(self):
        second = self.root / "a-much-longer-project-root"
        second.mkdir()
        self.write_contexts(
            [
                {
                    "root": "~/xebia",
                    "dockerProfile": "xebia",
                    "modelStack": None,
                    "memoryPalace": "~/.mempalace/palaces/xebia",
                    "memoryWing": "xebia",
                },
                {
                    "root": "~/complion/a-much-longer-project-root",
                    "dockerProfile": "realtime-production",
                    "modelStack": "xebia",
                    "memoryPalace": "~/.mempalace/palaces/complion",
                    "memoryWing": "complion",
                },
            ]
        )

        listed = self.run_context("list")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn("ROOT\tDOCKER\tPALACE\tWING", listed.stdout)
        self.assertIn("| PROJECT ROOT", listed.stdout)
        self.assertIn("| MODEL STACK", listed.stdout)
        self.assertIn("| MCP_DOCKER PROFILE", listed.stdout)
        self.assertIn("| MEMPALACE PATH", listed.stdout)
        self.assertIn("| MEMPALACE WING |", listed.stdout)
        self.assertIn("balanced (global)", listed.stdout)
        self.assertIn("xebia", listed.stdout)
        self.assertIn("~/complion/a-much-longer-project-root", listed.stdout)
        self.assertIn("realtime-production", listed.stdout)
        lines = listed.stdout.splitlines()
        self.assertTrue(lines)
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertTrue(lines[0].startswith("+") and lines[0].endswith("+"))
        self.assertEqual(lines[0], lines[2])
        self.assertEqual(lines[0], lines[-1])

    def test_list_renders_headers_for_an_empty_configuration(self):
        listed = self.run_context("list")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("| PROJECT ROOT | MODEL STACK | MCP_DOCKER PROFILE", listed.stdout)
        self.assertEqual(len(listed.stdout.splitlines()), 4)

    def test_add_and_update_reject_unsafe_or_conflicting_candidates_without_writing(self):
        second = self.root / "second"
        nested = self.workspace / "nested"
        second.mkdir()
        nested.mkdir()
        self.assertEqual(
            self.run_context("add", str(self.workspace), "--docker", "dev").returncode,
            0,
        )
        original = self.config_path.read_text(encoding="utf-8")

        rejected = (
            self.run_context("add", str(nested), "--docker", "nested"),
            self.run_context("add", str(second), "--docker", " "),
            self.run_context("add", str(second), "--docker", "next", "--wing", "workspace"),
            self.run_context(
                "add",
                str(second),
                "--docker",
                "next",
                "--palace",
                str(self.root / ".mempalace" / "palaces" / "workspace"),
            ),
            self.run_context("add", "/", "--docker", "root"),
            self.run_context("update", str(self.workspace)),
            self.run_context("update", str(self.workspace), "--palace", "~other/palace"),
        )
        for result in rejected:
            with self.subTest(stderr=result.stderr):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_remove_confirmation_preserves_palace_data_and_yes_removes_only_mapping(self):
        palace = self.root / "palace"
        self.assertEqual(
            self.run_context(
                "add", str(self.workspace), "--docker", "dev", "--palace", str(palace)
            ).returncode,
            0,
        )
        marker = palace / "preserve-me"
        marker.write_text("context data", encoding="utf-8")

        declined = self.run_context("remove", str(self.workspace), input_text="no\n")
        self.assertNotEqual(declined.returncode, 0)
        self.assertIn(str(self.workspace), declined.stdout)
        self.assertEqual(len(self.load_contexts()), 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "context data")

        removed = self.run_context("remove", str(self.workspace), "--yes")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn(str(palace), removed.stdout)
        self.assertEqual(self.load_contexts(), [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "context data")

    def test_launcher_resolves_an_installed_symlink(self):
        installed = self.root / "bin" / "orichum-context"
        installed.parent.mkdir()
        installed.symlink_to(REPO_ROOT / "bin" / "orichum-context")
        environment = os.environ.copy()
        environment["ORICHUM_CONFIG_HOME"] = str(REPO_ROOT / "config")
        completed = subprocess.run(
            [str(installed), "list"],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("| PROJECT ROOT", completed.stdout)
        self.assertIn("| MCP_DOCKER PROFILE", completed.stdout)

    def test_add_rejects_canonical_root_alias_overlap_before_writing(self):
        alias = self.root / "workspace-alias"
        nested = self.workspace / "nested"
        alias.symlink_to(self.workspace, target_is_directory=True)
        nested.mkdir()
        self.write_contexts(
            [
                {
                    "root": str(alias),
                    "dockerProfile": "dev",
                    "memoryPalace": str(self.root / "palace-one"),
                    "memoryWing": "one",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")
        rejected = self.run_context("add", str(nested), "--docker", "nested")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_update_rejects_canonical_palace_alias_before_writing(self):
        second = self.root / "second"
        palace = self.root / "palace"
        palace_alias = self.root / "palace-alias"
        second.mkdir()
        palace.mkdir()
        palace_alias.symlink_to(palace, target_is_directory=True)
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "dockerProfile": "dev",
                    "memoryPalace": str(palace_alias),
                    "memoryWing": "one",
                },
                {
                    "root": str(second),
                    "dockerProfile": "dev",
                    "memoryPalace": str(self.root / "palace-two"),
                    "memoryWing": "two",
                },
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")
        rejected = self.run_context("update", str(second), "--palace", str(palace))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_update_rejects_roots_that_canonically_resolve_to_home_or_filesystem_root(self):
        for target in (self.root, Path("/")):
            with self.subTest(target=target):
                alias = self.root / f"unsafe-{len(list(self.root.glob('unsafe-*')))}"
                alias.symlink_to(target, target_is_directory=True)
                self.write_contexts(
                    [
                        {
                            "root": str(alias),
                            "dockerProfile": "dev",
                            "memoryPalace": str(self.root / "palace"),
                            "memoryWing": "unsafe",
                        }
                    ]
                )
                original = self.config_path.read_text(encoding="utf-8")
                rejected = self.run_context("update", str(alias), "--docker", "next")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_palace_creation_failure_preserves_the_existing_config(self):
        original = self.config_path.read_text(encoding="utf-8")
        with mock.patch.object(
            project_context,
            "_ensure_new_palace",
            side_effect=OSError("fixture failure"),
        ), contextlib.redirect_stderr(io.StringIO()):
            result = project_context.context_main(
                [
                    "--config",
                    str(self.config_path),
                    "--routing-config",
                    str(self.routing_path),
                    "add",
                    str(self.workspace),
                    "--docker",
                    "dev",
                    "--palace",
                    str(self.root / "new-palace"),
                ]
            )
        self.assertNotEqual(result, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_add_and_update_never_create_through_a_symlinked_palace_ancestor(self):
        second = self.root / "second"
        outside = self.root / "outside"
        linked = self.root / "linked-palaces"
        second.mkdir()
        outside.mkdir()
        linked.symlink_to(outside, target_is_directory=True)
        self.assertEqual(
            self.run_context("add", str(self.workspace), "--docker", "dev").returncode,
            0,
        )
        original = self.config_path.read_text(encoding="utf-8")

        for arguments, child in (
            (("add", str(second), "--docker", "next", "--palace",
              str(linked / "add-target")), "add-target"),
            (("update", str(self.workspace), "--palace",
              str(linked / "update-target")), "update-target"),
        ):
            with self.subTest(arguments=arguments):
                rejected = self.run_context(*arguments)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)
                self.assertFalse((outside / child).exists())

    def test_validate_rejects_unsafe_canonical_roots_without_changing_list(self):
        home_alias = self.root / "home-alias"
        filesystem_alias = self.root / "filesystem-alias"
        home_alias.symlink_to(self.root, target_is_directory=True)
        filesystem_alias.symlink_to(Path("/"), target_is_directory=True)
        for root in ("/", "~", str(home_alias), str(filesystem_alias)):
            with self.subTest(root=root):
                self.write_contexts(
                    [
                        {
                            "root": root,
                            "dockerProfile": "dev",
                            "memoryPalace": str(self.root / "palace"),
                            "memoryWing": "unsafe",
                        }
                    ]
                )
                validated = self.run_context("validate")
                self.assertNotEqual(validated.returncode, 0)
                listed = self.run_context("list")
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertIn(root, listed.stdout)

    def test_remove_yes_can_recover_an_exact_unsafe_root_mapping(self):
        self.write_contexts(
            [
                {
                    "root": "/",
                    "dockerProfile": "dev",
                    "memoryPalace": str(self.root / "palace"),
                    "memoryWing": "unsafe",
                }
            ]
        )
        removed = self.run_context("remove", "/", "--yes")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.load_contexts(), [])


class StructuralConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name).resolve()
        self.config = {
            "contexts": [
                {
                    "root": "~/xebia",
                    "dockerProfile": "xebia",
                    "memoryPalace": "~/.mempalace/palaces/xebia",
                    "memoryWing": "xebia",
                },
                {
                    "root": "~/complion",
                    "dockerProfile": "realtime",
                    "memoryPalace": "~/.mempalace/palaces/complion",
                    "memoryWing": "complion",
                },
            ],
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_config(self, payload, *, raw=False):
        path = self.home / "structural-config.json"
        path.write_text(payload if raw else json.dumps(payload), encoding="utf-8")
        return path

    def validate(self, payload):
        return project_context.validate_config_structure(
            self.write_config(payload), home=self.home
        )

    def run_cli(self, config_path):
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "validate-config",
                "--config",
                str(config_path),
            ],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_valid_tilde_paths_pass_while_every_candidate_is_absent(self):
        self.assertFalse((self.home / "xebia").exists())
        self.assertFalse((self.home / "complion").exists())
        self.assertFalse((self.home / ".mempalace/palaces/xebia").exists())

    def test_structural_validation_allows_missing_final_palaces(self):
        self.assertIsNone(self.validate(self.config))
        self.assertFalse((self.home / "xebia").exists())
        self.assertFalse((self.home / "complion").exists())
        self.assertFalse((self.home / ".mempalace/palaces/xebia").exists())

    def test_structural_validation_rejects_root_home_and_canonical_aliases(self):
        alias = self.home / "home-alias"
        alias.symlink_to(self.home, target_is_directory=True)
        cases = (
            ("root", "/"),
            ("root", "~"),
            ("root", str(alias)),
            ("memoryPalace", "/"),
            ("memoryPalace", "~"),
            ("memoryPalace", str(alias / "missing")),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = json.loads(json.dumps(self.config))
                invalid["contexts"][0][field] = value
                with self.assertRaises(ContextError):
                    self.validate(invalid)

    def test_structural_validation_rejects_symlinked_existing_palace_ancestor(self):
        physical = self.home / "physical-palaces"
        linked = self.home / "linked-palaces"
        physical.mkdir()
        linked.symlink_to(physical, target_is_directory=True)
        invalid = json.loads(json.dumps(self.config))
        invalid["contexts"][0]["memoryPalace"] = str(linked / "missing" / "palace")
        with self.assertRaises(ContextError):
            self.validate(invalid)
        self.assertFalse((physical / "missing").exists())

    def test_lexically_duplicate_roots_palaces_and_wings_fail(self):
        duplicate_root = json.loads(json.dumps(self.config))
        duplicate_root["contexts"][1]["root"] = "~/xebia/../xebia"
        duplicate_root["contexts"][0]["root"] = str(self.home / "xebia")
        duplicate_wing = json.loads(json.dumps(self.config))
        duplicate_wing["contexts"][1]["memoryWing"] = "xebia"
        duplicate_palace = json.loads(json.dumps(self.config))
        duplicate_palace["contexts"][1]["memoryPalace"] = \
            str(self.home / ".mempalace/palaces/../palaces/xebia")
        duplicate_palace["contexts"][0]["memoryPalace"] = \
            str(self.home / ".mempalace/palaces/xebia")

        for payload in (duplicate_root, duplicate_palace, duplicate_wing):
            with self.subTest(payload=payload), self.assertRaises(ContextError):
                self.validate(payload)

    def test_overlapping_roots_fail_closed(self):
        overlapping = json.loads(json.dumps(self.config))
        overlapping["contexts"][1]["root"] = "~/xebia/nested"
        with self.assertRaises(ContextError):
            self.validate(overlapping)

    def test_schema_blank_path_and_type_errors_fail_closed(self):
        invalid_payloads = []
        for key in ("contexts",):
            payload = json.loads(json.dumps(self.config))
            del payload[key]
            invalid_payloads.append(payload)
        extra = json.loads(json.dumps(self.config))
        extra["extra"] = True
        invalid_payloads.append(extra)
        for key in ("root", "dockerProfile", "memoryPalace", "memoryWing"):
            missing = json.loads(json.dumps(self.config))
            del missing["contexts"][0][key]
            invalid_payloads.append(missing)
            blank = json.loads(json.dumps(self.config))
            blank["contexts"][0][key] = "  "
            invalid_payloads.append(blank)
        extra_context = json.loads(json.dumps(self.config))
        extra_context["contexts"][0]["extra"] = True
        invalid_payloads.append(extra_context)
        blank_palace = json.loads(json.dumps(self.config))
        blank_palace["contexts"][0]["memoryPalace"] = " "
        invalid_payloads.append(blank_palace)
        relative_palace = json.loads(json.dumps(self.config))
        relative_palace["contexts"][0]["memoryPalace"] = "relative/palace"
        invalid_payloads.append(relative_palace)
        relative_root = json.loads(json.dumps(self.config))
        relative_root["contexts"][0]["root"] = "relative/root"
        invalid_payloads.append(relative_root)
        unsupported_tilde = json.loads(json.dumps(self.config))
        unsupported_tilde["contexts"][0]["root"] = "~someone/xebia"
        invalid_payloads.append(unsupported_tilde)
        not_a_list = json.loads(json.dumps(self.config))
        not_a_list["contexts"] = {}
        invalid_payloads.append(not_a_list)

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ContextError):
                self.validate(payload)

        malformed = self.write_config('{"palacePath":', raw=True)
        with self.assertRaises(ContextError):
            project_context.validate_config_structure(malformed, home=self.home)

    def test_cli_is_silent_on_success_and_bounded_on_failure(self):
        valid = self.run_cli(self.write_config(self.config))
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(valid.stdout, "")
        self.assertEqual(valid.stderr, "")

        invalid = json.loads(json.dumps(self.config))
        unresolved_palace = str(self.home / "private-never-resolve-palace")
        invalid["contexts"][0]["memoryPalace"] = unresolved_palace
        invalid["contexts"][1]["memoryWing"] = "xebia"
        failed = self.run_cli(self.write_config(invalid))
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(failed.stdout, "")
        self.assertLessEqual(len(failed.stderr), 96)
        self.assertNotIn(unresolved_palace, failed.stderr)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertNotIn("Exception", failed.stderr)


if __name__ == "__main__":
    unittest.main()
