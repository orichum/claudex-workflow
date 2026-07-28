#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from integrations.common.runtime_bundle import (
    RuntimeBundleError,
    activate,
    build,
    rollback_activation,
    rollback_attempt,
    validate,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.stage = self.root / "stage"

    def test_build_copies_only_the_runtime_allowlist(self) -> None:
        release = build(REPOSITORY_ROOT, self.stage)

        self.assertTrue((release / "bin" / "orichum").is_file())
        self.assertTrue(
            (release / "integrations" / "common" / "orichum_cli.py").is_file()
        )
        self.assertTrue(
            (release / "controller" / "plugin" / "hooks" / "hooks.json").is_file()
        )
        self.assertTrue((release / "controller" / "settings.json").is_file())
        self.assertTrue((release / "config" / "runtime.json").is_file())
        self.assertTrue((release / "runtime-manifest.json").is_file())
        self.assertFalse((release / "README.md").exists())
        self.assertFalse((release / "docs").exists())
        self.assertFalse((release / "tests").exists())
        self.assertFalse((release / ".git").exists())
        self.assertFalse(any(release.rglob("__pycache__")))
        self.assertFalse(any(release.rglob("*.pyc")))
        validate(release)

    def test_build_is_content_addressed_and_reproducible(self) -> None:
        first = build(REPOSITORY_ROOT, self.stage / "first")
        second = build(REPOSITORY_ROOT, self.stage / "second")

        self.assertEqual(first.name, second.name)
        first_manifest = json.loads(
            (first / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        second_manifest = json.loads(
            (second / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_manifest["digest"], first.name)

    def test_validate_rejects_modified_release(self) -> None:
        release = build(REPOSITORY_ROOT, self.stage)
        launcher = release / "bin" / "orichum"
        launcher.write_bytes(launcher.read_bytes() + b"\n")

        with self.assertRaisesRegex(
            RuntimeBundleError, "runtime file (?:size|digest) mismatch"
        ):
            validate(release)

    def test_activate_installs_real_release_and_switches_pointer(self) -> None:
        staged = build(REPOSITORY_ROOT, self.stage)

        release, previous = activate(staged, self.home)

        self.assertIsNone(previous)
        self.assertTrue(release.is_dir())
        self.assertFalse(release.is_symlink())
        current = self.home / "runtime" / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(
            current.resolve(strict=True),
            release.resolve(strict=True),
        )
        validate(release)

    def test_activation_can_be_rolled_back_without_runtime_debris(self) -> None:
        staged = build(REPOSITORY_ROOT, self.stage)
        release, previous = activate(staged, self.home)

        rollback_activation(self.home, release, previous)

        self.assertFalse((self.home / "runtime").exists())

    def test_failed_activation_attempt_without_a_release_is_reversible(self) -> None:
        predicted = self.home / "runtime" / "releases" / ("0" * 64)

        rollback_attempt(self.home, predicted, None)

        self.assertFalse((self.home / "runtime").exists())

    def test_build_rejects_symlinked_source_payload(self) -> None:
        source = self.root / "source"
        shutil.copytree(
            REPOSITORY_ROOT,
            source,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        target = source / "VERSION"
        target.unlink()
        target.symlink_to(REPOSITORY_ROOT / "VERSION")

        with self.assertRaisesRegex(RuntimeBundleError, "symlink"):
            build(source, self.stage)


if __name__ == "__main__":
    unittest.main()
