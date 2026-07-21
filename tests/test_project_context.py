#!/usr/bin/env python3
import contextlib
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
        self.palace = self.root / "palace"
        for directory in (
            self.xebia_repo,
            self.complion_repo,
            self.palace,
            self.root / "elsewhere",
            self.root / "xebia-old",
        ):
            directory.mkdir(parents=True)
        os.chmod(self.palace, 0o700)
        self.config = {
            "palacePath": str(self.palace),
            "contexts": [
                {
                    "root": str(self.xebia),
                    "dockerProfile": "xebia",
                    "memoryWing": "xebia",
                },
                {
                    "root": str(self.complion),
                    "dockerProfile": "realtime",
                    "memoryWing": "complion",
                },
            ],
        }
        self.config_path = self.write_config(self.config)

    def tearDown(self):
        os.chmod(self.palace, 0o700)
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
        self.assertEqual(self.resolve(self.xebia / "repo")["route"]["id"], "xebia")
        self.assertEqual(
            self.resolve(self.complion / "nested" / "repo")["route"]["id"],
            "complion",
        )
        self.assertIsNone(self.resolve(self.root / "xebia-old")["route"])
        self.assertIsNone(self.resolve(self.root / "elsewhere")["route"])

    def test_longest_matching_canonical_root_wins(self):
        nested = json.loads(json.dumps(self.config))
        nested["contexts"].append(
            {
                "root": str(self.xebia_repo),
                "dockerProfile": "nested-profile",
                "memoryWing": "nested-wing",
            }
        )
        self.assertEqual(self.resolve(self.xebia_repo, nested)["route"]["id"], "nested-wing")

    def test_symlink_uses_physical_target(self):
        link = self.root / "linked-repo"
        link.symlink_to(self.xebia / "repo", target_is_directory=True)
        result = self.resolve(link)
        self.assertEqual(result["launchDirReal"], str((self.xebia / "repo").resolve()))
        self.assertEqual(result["route"]["id"], "xebia")

    def test_duplicate_roots_or_wings_fail_closed(self):
        for field in ("root", "memoryWing"):
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
        bad["palacePath"] = str(palace_link)
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
        missing["palacePath"] = str(missing_path)
        missing_result = self.resolve(nested, missing)
        self.assert_memory_failure(missing_result, "palace_missing", self.xebia_repo)
        self.assertNotIn(str(missing_path), json.dumps(missing_result))

        not_directory_path = self.root / "palace-file"
        not_directory_path.write_text("not a directory", encoding="utf-8")
        not_directory = json.loads(json.dumps(self.config))
        not_directory["palacePath"] = str(not_directory_path)
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
        linked["palacePath"] = str(palace_link)
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
                    "memoryWing": "complion",
                    "memoryAvailable": True,
                    "memoryFailureCode": None,
                    "palacePathReal": str(self.palace),
                },
            },
        )

    def test_configuration_schema_is_closed_and_paths_are_strict(self):
        invalid_payloads = []

        for key in ("palacePath", "contexts"):
            missing = json.loads(json.dumps(self.config))
            del missing[key]
            invalid_payloads.append(missing)
        extra = json.loads(json.dumps(self.config))
        extra["extra"] = True
        invalid_payloads.append(extra)

        for key in ("root", "dockerProfile", "memoryWing"):
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
        relative_palace["palacePath"] = "relative/palace"
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
        tilde["palacePath"] = "~/palace"
        tilde["contexts"][0]["root"] = "~/xebia"
        tilde["contexts"][1]["root"] = "~/complion"
        result = self.resolve(self.xebia_repo, tilde)
        self.assertEqual(result["route"]["contextRootReal"], str(self.xebia))
        self.assertEqual(result["route"]["palacePathReal"], str(self.palace))

    def test_launch_directory_must_resolve_strictly(self):
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / "missing-launch")

    def test_authoritative_configuration_is_exact(self):
        payload = json.loads((REPO_ROOT / "controller/project-context.json").read_text())
        self.assertEqual(
            payload,
            {
                "palacePath": "~/.mempalace/palace",
                "contexts": [
                    {
                        "root": "~/xebia",
                        "dockerProfile": "xebia",
                        "memoryWing": "xebia",
                    },
                    {
                        "root": "~/complion",
                        "dockerProfile": "realtime",
                        "memoryWing": "complion",
                    },
                ],
            },
        )

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


class StructuralConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name).resolve()
        self.config = {
            "palacePath": "~/.mempalace/palace",
            "contexts": [
                {
                    "root": "~/xebia",
                    "dockerProfile": "xebia",
                    "memoryWing": "xebia",
                },
                {
                    "root": "~/complion",
                    "dockerProfile": "realtime",
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
        self.assertFalse((self.home / ".mempalace/palace").exists())

    def test_structural_validation_performs_no_configured_path_probe(self):
        config_path = self.write_config(self.config)

        def forbidden_probe(*_args, **_kwargs):
            raise AssertionError("configured path probe was attempted")

        path_methods = ("resolve", "stat", "lstat", "exists", "is_dir", "is_symlink")
        os_methods = ("stat", "lstat")
        os_path_methods = ("exists", "isdir", "islink", "realpath")
        patches = [
            mock.patch.object(Path, name, side_effect=forbidden_probe)
            for name in path_methods
        ]
        patches.extend(
            mock.patch.object(os, name, side_effect=forbidden_probe)
            for name in os_methods
        )
        patches.extend(
            mock.patch.object(os.path, name, side_effect=forbidden_probe)
            for name in os_path_methods
        )

        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            self.assertIsNone(
                project_context.validate_config_structure(config_path, home=self.home)
            )
        self.assertIsNone(self.validate(self.config))
        self.assertFalse((self.home / "xebia").exists())
        self.assertFalse((self.home / "complion").exists())
        self.assertFalse((self.home / ".mempalace/palace").exists())

    def test_lexically_duplicate_roots_and_duplicate_wings_fail(self):
        duplicate_root = json.loads(json.dumps(self.config))
        duplicate_root["contexts"][1]["root"] = "~/xebia/../xebia"
        duplicate_root["contexts"][0]["root"] = str(self.home / "xebia")
        duplicate_wing = json.loads(json.dumps(self.config))
        duplicate_wing["contexts"][1]["memoryWing"] = "xebia"

        for payload in (duplicate_root, duplicate_wing):
            with self.subTest(payload=payload), self.assertRaises(ContextError):
                self.validate(payload)

    def test_schema_blank_path_and_type_errors_fail_closed(self):
        invalid_payloads = []
        for key in ("palacePath", "contexts"):
            payload = json.loads(json.dumps(self.config))
            del payload[key]
            invalid_payloads.append(payload)
        extra = json.loads(json.dumps(self.config))
        extra["extra"] = True
        invalid_payloads.append(extra)
        for key in ("root", "dockerProfile", "memoryWing"):
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
        blank_palace["palacePath"] = " "
        invalid_payloads.append(blank_palace)
        relative_palace = json.loads(json.dumps(self.config))
        relative_palace["palacePath"] = "relative/palace"
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
        invalid["palacePath"] = unresolved_palace
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
