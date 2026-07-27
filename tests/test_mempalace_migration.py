#!/usr/bin/env python3
"""Unit tests for the bounded legacy-palace migration policy."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations import mempalace_migration
from integrations.mempalace_migration import (
    MigrationError,
    fingerprint_drawers,
    route_drawers,
)


class MempalaceMigrationTests(unittest.TestCase):
    def test_routes_only_approved_project_wings(self):
        drawers = [
            {"drawer_id": "1", "wing": "xebia", "room": "decisions", "content": "a"},
            {"drawer_id": "2", "wing": "wing_xebia", "room": "patterns", "content": "b"},
            {"drawer_id": "3", "wing": "complion", "room": "decisions", "content": "c"},
            {"drawer_id": "4", "wing": "wing_complion", "room": "patterns", "content": "d"},
            {"drawer_id": "5", "wing": "sessions", "room": "general", "content": "exclude"},
            {"drawer_id": "6", "wing": "other_repo", "room": "general", "content": "exclude"},
        ]
        routed = route_drawers(drawers)
        self.assertEqual([item["content"] for item in routed["xebia"]], ["a", "b"])
        self.assertEqual([item["content"] for item in routed["complion"]], ["c", "d"])
        self.assertTrue(all(item["wing"] in {"xebia", "complion"}
                            for values in routed.values() for item in values))

    def test_fingerprint_is_order_independent_and_content_sensitive(self):
        first = [
            {"wing": "xebia", "room": "one", "content": "alpha"},
            {"wing": "xebia", "room": "two", "content": "beta"},
        ]
        self.assertEqual(fingerprint_drawers(first), fingerprint_drawers(reversed(first)))
        changed = [dict(first[0]), {**first[1], "content": "changed"}]
        self.assertNotEqual(fingerprint_drawers(first), fingerprint_drawers(changed))

    def test_absent_identity_normalizes_case_only_on_insensitive_filesystems(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            with mock.patch.object(
                mempalace_migration,
                "_filesystem_is_case_sensitive",
                return_value=False,
                create=True,
            ):
                with self.assertRaises(MigrationError):
                    mempalace_migration.validate_migration_paths(
                        source, root / "Future", root / "future" / "Child"
                    )

            with mock.patch.object(
                mempalace_migration,
                "_filesystem_is_case_sensitive",
                return_value=True,
                create=True,
            ):
                identities = mempalace_migration.validate_migration_paths(
                    source, root / "Future", root / "future" / "Child"
                )
            self.assertEqual(identities[1], root / "Future")
            self.assertEqual(identities[2], root / "future" / "Child")

    def test_unknown_case_sensitivity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            with mock.patch.object(
                mempalace_migration, "_case_variant", return_value=None
            ):
                self.assertFalse(
                    mempalace_migration._filesystem_is_case_sensitive(root)
                )


class MigrationRerunTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.source = self.root / "source"
        self.xebia_target = self.root / "xebia"
        self.complion_target = self.root / "complion"
        self.source.mkdir()
        self.source_drawers = {
            "xebia": [
                {"wing": "xebia", "room": "decisions", "content": "x"},
                {"wing": "xebia", "room": "patterns", "content": "y"},
            ],
            "complion": [
                {"wing": "complion", "room": "decisions", "content": "c"},
            ],
        }
        self.target_drawers = {"xebia": [], "complion": []}
        self.target_counts = {"xebia": 0, "complion": 0}
        self.writes = []

    def tearDown(self):
        self.temporary_directory.cleanup()

    def fake_selected(self, _executable, palace):
        if palace == self.source:
            return {name: list(values) for name, values in self.source_drawers.items()}
        if palace == self.xebia_target:
            return {"xebia": list(self.target_drawers["xebia"]), "complion": []}
        if palace == self.complion_target:
            return {"xebia": [], "complion": list(self.target_drawers["complion"])}
        raise AssertionError(f"unexpected palace: {palace}")

    def fake_counts(self, _executable, palace):
        if palace == self.source:
            return dict(mempalace_migration.EXPECTED_RECORD_COUNTS)
        if palace == self.xebia_target:
            return {"xebia": self.target_counts["xebia"], "complion": 0}
        if palace == self.complion_target:
            return {"xebia": 0, "complion": self.target_counts["complion"]}
        raise AssertionError(f"unexpected palace: {palace}")

    def fake_write(self, _executable, palace, drawers):
        name = "xebia" if palace == self.xebia_target else "complion"
        self.assertTrue(all(item["wing"] == name for item in drawers))
        self.writes.append((name, [item["content"] for item in drawers]))
        self.target_drawers[name].extend(drawers)
        if fingerprint_drawers(self.target_drawers[name]) == \
           fingerprint_drawers(self.source_drawers[name]) and \
           len(self.target_drawers[name]) == len(self.source_drawers[name]):
            self.target_counts[name] = mempalace_migration.EXPECTED_RECORD_COUNTS[name]
        else:
            self.target_counts[name] = min(
                len(self.target_drawers[name]),
                mempalace_migration.EXPECTED_RECORD_COUNTS[name] - 1,
            )

    def run_execute(self):
        arguments = [
            "mempalace_migration.py", "--execute", "--source", str(self.source),
            "--xebia-target", str(self.xebia_target),
            "--complion-target", str(self.complion_target),
        ]
        with mock.patch.object(sys, "argv", arguments), \
             mock.patch.object(mempalace_migration.shutil, "which", return_value="mcp"), \
             mock.patch.object(mempalace_migration, "read_selected", self.fake_selected), \
             mock.patch.object(mempalace_migration, "read_record_counts", self.fake_counts), \
             mock.patch.object(mempalace_migration, "write_target", self.fake_write), \
             contextlib.redirect_stdout(io.StringIO()):
            return mempalace_migration.main()

    def test_empty_targets_migrate_and_exact_rerun_is_a_noop(self):
        self.assertEqual(self.run_execute(), 0)
        self.assertEqual(self.writes, [("xebia", ["x", "y"]),
                                       ("complion", ["c"])])
        self.writes.clear()
        self.assertEqual(self.run_execute(), 0)
        self.assertEqual(self.writes, [])

    def test_partial_target_adds_only_missing_items(self):
        self.xebia_target.mkdir()
        self.target_drawers["xebia"] = self.source_drawers["xebia"][:1]
        self.target_counts["xebia"] = 1
        self.assertEqual(self.run_execute(), 0)
        self.assertEqual(self.writes, [("xebia", ["y"]),
                                       ("complion", ["c"])])
        self.assertEqual(
            fingerprint_drawers(self.target_drawers["xebia"]),
            fingerprint_drawers(self.source_drawers["xebia"]),
        )
        self.assertEqual(
            self.target_counts["xebia"],
            mempalace_migration.EXPECTED_RECORD_COUNTS["xebia"],
        )

    def test_mid_target_interruption_rerun_fills_missing_without_duplicates(self):
        def interrupted_write(_executable, palace, drawers):
            name = "xebia" if palace == self.xebia_target else "complion"
            first = drawers[0]
            self.target_drawers[name].append(first)
            self.target_counts[name] = 1
            self.writes.append((name, [first["content"]]))
            raise MigrationError("fixture interruption")

        with mock.patch.object(self, "fake_write", side_effect=interrupted_write):
            with self.assertRaises(MigrationError):
                self.run_execute()
        self.assertEqual(self.writes, [("xebia", ["x"])])
        self.writes.clear()
        self.assertEqual(self.run_execute(), 0)
        self.assertEqual(self.writes, [("xebia", ["y"]),
                                       ("complion", ["c"])])
        self.assertEqual(self.target_drawers["xebia"], self.source_drawers["xebia"])
        self.assertEqual(
            self.target_counts,
            dict(mempalace_migration.EXPECTED_RECORD_COUNTS),
        )

    def test_unexpected_or_excess_duplicate_target_items_reject_all_writes(self):
        invalid_targets = (
            [{"wing": "xebia", "room": "decisions", "content": "unexpected"}],
            [self.source_drawers["xebia"][0], self.source_drawers["xebia"][0]],
        )
        for existing in invalid_targets:
            with self.subTest(existing=existing):
                self.target_drawers = {"xebia": list(existing), "complion": []}
                self.target_counts = {"xebia": len(existing), "complion": 0}
                self.writes.clear()
                self.xebia_target.mkdir(exist_ok=True)
                with self.assertRaises(MigrationError):
                    self.run_execute()
                self.assertEqual(self.writes, [])
                self.assertEqual(self.target_drawers["complion"], [])

    def test_symlinked_target_or_existing_ancestor_rejects_without_mutation(self):
        source_marker = self.source / "preserve"
        source_marker.write_text("source", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        outside_marker = outside / "preserve"
        outside_marker.write_text("outside", encoding="utf-8")

        linked_target = self.root / "linked-target"
        linked_target.symlink_to(outside, target_is_directory=True)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(outside, target_is_directory=True)
        cases = (linked_target, linked_parent / "missing-target")
        for target in cases:
            with self.subTest(target=target):
                self.xebia_target = target
                self.complion_target = self.root / "safe-complion"
                self.writes.clear()
                with self.assertRaises(MigrationError):
                    self.run_execute()
                self.assertEqual(self.writes, [])
                self.assertEqual(source_marker.read_text(encoding="utf-8"), "source")
                self.assertEqual(outside_marker.read_text(encoding="utf-8"), "outside")
                self.assertFalse((outside / "missing-target").exists())
                self.assertFalse(self.complion_target.exists())

    def test_same_alias_and_overlapping_paths_reject_before_any_mutation(self):
        source_marker = self.source / "preserve"
        source_marker.write_text("source", encoding="utf-8")
        cases = (
            (self.source, self.root / "complion-safe"),
            (self.root / "same", self.root / "same"),
            (self.root / "alias-parent" / ".." / "same", self.root / "same"),
            (self.root / "nested", self.root / "nested" / "child"),
        )
        for xebia_target, complion_target in cases:
            with self.subTest(xebia_target=xebia_target,
                              complion_target=complion_target):
                self.xebia_target = xebia_target
                self.complion_target = complion_target
                self.writes.clear()
                with self.assertRaises(MigrationError):
                    self.run_execute()
                self.assertEqual(self.writes, [])
                self.assertEqual(source_marker.read_text(encoding="utf-8"), "source")
                if xebia_target != self.source:
                    self.assertFalse(xebia_target.exists())
                self.assertFalse(complion_target.exists())

    def test_case_detection_and_real_case_alias_reject_before_mutation(self):
        case_alias = self.root / self.source.name.swapcase()
        observed_case_sensitive = not case_alias.exists()
        detector = getattr(
            mempalace_migration, "_filesystem_is_case_sensitive", None
        )
        self.assertIsNotNone(detector)
        self.assertEqual(
            detector(self.source),
            observed_case_sensitive,
        )
        if observed_case_sensitive:
            return

        source_marker = self.source / "preserve"
        source_marker.write_text("source", encoding="utf-8")
        self.xebia_target = case_alias
        self.complion_target = self.root / "case-safe-complion"
        with self.assertRaises(MigrationError):
            self.run_execute()
        self.assertEqual(self.writes, [])
        self.assertEqual(source_marker.read_text(encoding="utf-8"), "source")
        self.assertFalse(self.complion_target.exists())

    def test_root_and_case_alias_home_reject_before_reads_or_mutation(self):
        home = Path.home().resolve()
        alternate_parts = list(home.parts)
        for index, component in enumerate(alternate_parts[1:], start=1):
            alternate = component.swapcase()
            if alternate != component:
                alternate_parts[index] = alternate
                break
        alternate_home = Path(*alternate_parts)
        real_case_alias = alternate_home.exists() and \
            alternate_home.samefile(home)
        source_marker = self.source / "preserve"
        source_marker.write_text("source", encoding="utf-8")

        for unsafe_target, force_insensitive in (
            (Path("/"), False),
            (alternate_home, not real_case_alias),
        ):
            with self.subTest(
                unsafe_target=unsafe_target,
                real_case_alias=real_case_alias,
            ):
                self.xebia_target = unsafe_target
                self.complion_target = self.root / "home-guard-complion"
                arguments = [
                    "mempalace_migration.py", "--execute",
                    "--source", str(self.source),
                    "--xebia-target", str(self.xebia_target),
                    "--complion-target", str(self.complion_target),
                ]
                read_counts = mock.Mock(
                    return_value=dict(mempalace_migration.EXPECTED_RECORD_COUNTS)
                )
                read_selected = mock.Mock(return_value={
                    name: list(values)
                    for name, values in self.source_drawers.items()
                })
                write_target = mock.Mock()
                sensitivity = mock.patch.object(
                    mempalace_migration,
                    "_filesystem_is_case_sensitive",
                    return_value=False,
                ) if force_insensitive else contextlib.nullcontext()
                with sensitivity, \
                     mock.patch.object(sys, "argv", arguments), \
                     mock.patch.object(
                         mempalace_migration.shutil, "which", return_value="mcp"
                     ), \
                     mock.patch.object(
                         mempalace_migration,
                         "read_record_counts",
                         read_counts,
                     ), \
                     mock.patch.object(
                         mempalace_migration, "read_selected", read_selected
                     ), \
                     mock.patch.object(
                         mempalace_migration, "write_target", write_target
                     ), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(MigrationError):
                        mempalace_migration.main()
                read_counts.assert_not_called()
                read_selected.assert_not_called()
                write_target.assert_not_called()
                self.assertEqual(
                    source_marker.read_text(encoding="utf-8"), "source"
                )
                self.assertFalse(self.complion_target.exists())

    def test_shipped_utility_has_no_source_deletion_surface(self):
        source = Path(mempalace_migration.__file__).read_text(encoding="utf-8")
        for forbidden in ("--delete-source", "safe_delete_source", "rmtree"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
