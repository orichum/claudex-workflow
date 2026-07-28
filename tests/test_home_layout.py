#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations.common.home_layout import commit, prepare, rollback


class HomeLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home" / ".orichum"
        self.legacy_data = self.root / "home" / ".local" / "share" / "orichum"
        self.legacy_config = self.root / "home" / ".config" / "orichum"
        self.legacy_cache = self.root / "home" / ".cache" / "orichum"
        self.journal_root = self.root / "home" / ".local" / "state" / "orichum"
        for directory in (
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
        ):
            directory.mkdir(parents=True, mode=0o700)
            (directory / "marker").write_text(
                directory.name, encoding="utf-8"
            )

    def test_prepare_and_commit_consolidate_without_legacy_links(self) -> None:
        journal = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )

        self.assertIsNotNone(journal)
        self.assertTrue(self.legacy_data.is_symlink())
        self.assertTrue(self.legacy_config.is_symlink())
        self.assertTrue(self.legacy_cache.is_symlink())
        self.assertTrue((self.home / "marker").is_file())
        self.assertTrue((self.home / "config" / "marker").is_file())
        self.assertTrue((self.home / "cache" / "marker").is_file())

        commit(journal)

        self.assertFalse(self.legacy_data.exists())
        self.assertFalse(self.legacy_data.is_symlink())
        self.assertFalse(self.legacy_config.exists())
        self.assertFalse(self.legacy_cache.exists())
        self.assertFalse(journal.exists())

    def test_prepare_and_rollback_restore_the_exact_legacy_layout(self) -> None:
        journal = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )

        rollback(journal)

        self.assertTrue((self.legacy_data / "marker").is_file())
        self.assertTrue((self.legacy_config / "marker").is_file())
        self.assertTrue((self.legacy_cache / "marker").is_file())
        self.assertFalse(self.legacy_data.is_symlink())
        self.assertFalse(self.legacy_config.is_symlink())
        self.assertFalse(self.legacy_cache.is_symlink())
        self.assertFalse(self.home.exists())
        self.assertFalse(journal.exists())

    def test_no_legacy_layout_creates_an_empty_private_home(self) -> None:
        for directory in (
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
        ):
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

        journal = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )

        self.assertIsNone(journal)
        self.assertTrue(self.home.is_dir())
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)

    def test_rollback_recovers_a_move_before_its_journal_update(self) -> None:
        journal = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )
        document = json.loads(journal.read_text(encoding="utf-8"))
        document["moves"][0]["moved"] = False
        journal.write_text(
            json.dumps(document, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        journal.chmod(0o600)
        self.legacy_data.unlink()

        rollback(journal)

        self.assertTrue((self.legacy_data / "marker").is_file())
        self.assertFalse(self.home.exists())

    def test_prepare_finishes_an_interrupted_commit(self) -> None:
        journal = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )
        document = json.loads(journal.read_text(encoding="utf-8"))
        document["phase"] = "committing"
        journal.write_text(
            json.dumps(document, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        journal.chmod(0o600)
        self.legacy_data.unlink()

        recovered = prepare(
            self.home,
            self.legacy_data,
            self.legacy_config,
            self.legacy_cache,
            self.journal_root,
        )

        self.assertIsNone(recovered)
        self.assertFalse(journal.exists())
        self.assertFalse(self.legacy_config.exists())
        self.assertFalse(self.legacy_cache.exists())
        self.assertTrue((self.home / "marker").is_file())
        self.assertTrue((self.home / "config" / "marker").is_file())


if __name__ == "__main__":
    unittest.main()
