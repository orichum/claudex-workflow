#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations.common.plugin_registry import (
    PluginRegistryError,
    update_plugins,
)


class PluginRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.manifest = self.root / "plugins.json"
        self.manifest.write_text(
            '{"schemaVersion":1,"marketplaces":[],"plugins":[]}\n'
        )
        self.manifest.chmod(0o600)

    def test_update_plugins_writes_valid_private_manifest(self) -> None:
        updated = update_plugins(
            self.manifest,
            lambda document: {
                **document,
                "marketplaces": [
                    {"name": "acme", "source": "example/acme"}
                ],
                "plugins": ["sample@acme"],
            },
        )
        self.assertEqual(updated["plugins"], ["sample@acme"])
        self.assertEqual(
            json.loads(self.manifest.read_text())["plugins"],
            ["sample@acme"],
        )
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)

    def test_update_plugins_rejects_invalid_result_without_change(
        self,
    ) -> None:
        original = self.manifest.read_bytes()
        with self.assertRaises(PluginRegistryError):
            update_plugins(
                self.manifest,
                lambda document: {**document, "plugins": ["invalid"]},
            )
        self.assertEqual(self.manifest.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
