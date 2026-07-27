#!/usr/bin/env python3
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import integrations.common.install_state as install_state
from integrations.common.install_state import (
    InstallStateError,
    fingerprint_paths,
    load_manifest,
    main,
    write_manifest,
)


def _component(
    *,
    version: str = "0.2.4",
    source: str = "github:StringKe/claudex@v0.2.4",
) -> dict[str, str]:
    return {
        "version": version,
        "sourceIdentity": source,
        "artifactSha256": "a" * 64,
        "inputSha256": "b" * 64,
        "probeSha256": "c" * 64,
    }


class InstallStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state_root = self.root / "state"
        self.state_root.mkdir(mode=0o700)
        self.state = self.state_root / "install-state.json"
        self.components = {"claudex": _component()}

    def _write_raw(self, payload: str, mode: int = 0o600) -> None:
        self.state.write_text(payload, encoding="utf-8")
        self.state.chmod(mode)

    def test_round_trip_private_manifest(self) -> None:
        write_manifest(self.state, "darwin:aarch64", self.components)

        document = load_manifest(self.state, "darwin:aarch64")

        self.assertIsNotNone(document)
        self.assertEqual(document["components"], self.components)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state_root.stat().st_mode), 0o700)
        self.assertEqual(
            self.state.read_text(encoding="utf-8"),
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )

    def test_missing_malformed_wrong_mode_or_platform_is_unverified(self) -> None:
        self.assertIsNone(load_manifest(self.state, "darwin:aarch64"))

        self._write_raw("{")
        self.assertIsNone(load_manifest(self.state, "darwin:aarch64"))

        write_manifest(self.state, "darwin:aarch64", self.components)
        self.assertIsNone(load_manifest(self.state, "systemd:x86_64"))

        self.state.chmod(0o644)
        self.assertIsNone(load_manifest(self.state, "darwin:aarch64"))

    def test_symlink_nonregular_and_foreign_owner_are_unsafe(self) -> None:
        external = self.root / "external.json"
        external.write_text("preserve\n", encoding="utf-8")
        self.state.symlink_to(external)
        with self.assertRaisesRegex(InstallStateError, "unsafe"):
            load_manifest(self.state, "darwin:aarch64")
        self.assertEqual(external.read_text(encoding="utf-8"), "preserve\n")
        self.state.unlink()

        self.state.mkdir(mode=0o700)
        with self.assertRaisesRegex(InstallStateError, "unsafe"):
            load_manifest(self.state, "darwin:aarch64")
        self.state.rmdir()

        write_manifest(self.state, "darwin:aarch64", self.components)
        real_lstat = install_state.os.lstat

        def foreign_lstat(path: object, *args: object, **kwargs: object):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == self.state:
                values = list(result)
                values[stat.ST_UID] = os.getuid() + 1
                return os.stat_result(values)
            return result

        with mock.patch.object(
            install_state.os,
            "lstat",
            side_effect=foreign_lstat,
        ):
            with self.assertRaisesRegex(InstallStateError, "unsafe"):
                load_manifest(self.state, "darwin:aarch64")

    def test_unsafe_parent_is_rejected_for_read_and_write(self) -> None:
        self.state_root.chmod(0o755)
        with self.assertRaisesRegex(InstallStateError, "parent"):
            load_manifest(self.state, "darwin:aarch64")
        with self.assertRaisesRegex(InstallStateError, "parent"):
            write_manifest(self.state, "darwin:aarch64", self.components)

        self.state_root.chmod(0o700)
        linked_root = self.root / "linked-state"
        linked_root.symlink_to(self.state_root, target_is_directory=True)
        linked_state = linked_root / self.state.name
        with self.assertRaisesRegex(InstallStateError, "parent"):
            load_manifest(linked_state, "darwin:aarch64")
        with self.assertRaisesRegex(InstallStateError, "parent"):
            write_manifest(
                linked_state,
                "darwin:aarch64",
                self.components,
            )

    def test_invalid_schema_never_authorizes_reuse(self) -> None:
        invalid_documents = (
            {
                "schemaVersion": 1,
                "platform": "darwin:aarch64",
                "components": {"unknown": _component()},
            },
            {
                "schemaVersion": 1,
                "platform": "darwin:aarch64",
                "components": {
                    "claudex": {
                        **_component(),
                        "unexpected": "value",
                    }
                },
            },
            {
                "schemaVersion": 1,
                "platform": "darwin:aarch64",
                "components": {
                    "claudex": {
                        **_component(),
                        "artifactSha256": "A" * 64,
                    }
                },
            },
            {
                "schemaVersion": True,
                "platform": "darwin:aarch64",
                "components": self.components,
            },
            {
                "schemaVersion": 1,
                "platform": "darwin:aarch64",
                "components": {
                    "claudex": {
                        **_component(),
                        "sourceIdentity": "token\nvalue",
                    }
                },
            },
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                self._write_raw(json.dumps(document))
                self.assertIsNone(
                    load_manifest(self.state, "darwin:aarch64")
                )

        self._write_raw(
            '{"schemaVersion":1,"schemaVersion":1,'
            '"platform":"darwin:aarch64","components":{}}'
        )
        self.assertIsNone(load_manifest(self.state, "darwin:aarch64"))
        self._write_raw(
            '{"schemaVersion":1,"platform":"darwin:aarch64",'
            '"components":{},"number":NaN}'
        )
        self.assertIsNone(load_manifest(self.state, "darwin:aarch64"))

    def test_write_rejects_invalid_components(self) -> None:
        invalid = {"unknown": _component()}
        with self.assertRaisesRegex(InstallStateError, "invalid"):
            write_manifest(self.state, "darwin:aarch64", invalid)
        self.assertFalse(self.state.exists())

    def test_atomic_replace_failure_preserves_prior_manifest(self) -> None:
        write_manifest(self.state, "darwin:aarch64", self.components)
        before = self.state.read_bytes()
        replacement = {
            "python": _component(
                version="3.14.6",
                source="python:3.14.6",
            )
        }

        with mock.patch.object(
            install_state.os,
            "replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaisesRegex(
                InstallStateError,
                "could not be replaced",
            ):
                write_manifest(
                    self.state,
                    "darwin:aarch64",
                    replacement,
                )

        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(
            list(self.state_root.glob(".install-state.*")),
            [],
        )

    def test_fingerprint_is_order_independent_and_content_sensitive(self) -> None:
        first = self.root / "a"
        second = self.root / "b"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        before = fingerprint_paths(
            self.root,
            [Path("a"), Path("b")],
        )

        self.assertEqual(
            before,
            fingerprint_paths(
                self.root,
                [Path("b"), Path("a")],
            ),
        )
        second.write_bytes(b"changed")
        self.assertNotEqual(
            before,
            fingerprint_paths(
                self.root,
                [Path("a"), Path("b")],
            ),
        )
        second.write_bytes(b"two")
        second.chmod(0o600)
        self.assertNotEqual(
            before,
            fingerprint_paths(
                self.root,
                [Path("a"), Path("b")],
            ),
        )

    def test_fingerprint_rejects_unsafe_inputs(self) -> None:
        regular = self.root / "regular"
        regular.write_text("data", encoding="utf-8")
        directory = self.root / "directory"
        directory.mkdir()
        linked = self.root / "linked"
        linked.symlink_to(regular)
        outside = self.root.parent / "outside-install-state-test"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink)

        invalid = (
            [Path("regular"), Path("regular")],
            [Path("missing")],
            [Path("directory")],
            [Path("linked")],
            [Path("../outside-install-state-test")],
            [regular],
        )
        for paths in invalid:
            with self.subTest(paths=paths):
                with self.assertRaisesRegex(
                    InstallStateError,
                    "fingerprint",
                ):
                    fingerprint_paths(self.root, paths)

    def test_cli_distinguishes_unverified_and_unsafe_state(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(
                ["read", str(self.state), "darwin:aarch64"]
            )
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

        self.state.symlink_to(self.root / "missing-target")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(
                ["read", str(self.state), "darwin:aarch64"]
            )
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("unsafe", stderr.getvalue())

    def test_cli_write_read_and_fingerprint_round_trip(self) -> None:
        candidate = self.root / "components.json"
        candidate.write_text(
            json.dumps(self.components),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                main(
                    [
                        "write",
                        str(self.state),
                        "darwin:aarch64",
                        str(candidate),
                    ]
                ),
                0,
            )
        self.assertEqual(stdout.getvalue(), "")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                main(
                    ["read", str(self.state), "darwin:aarch64"]
                ),
                0,
            )
        self.assertEqual(
            json.loads(stdout.getvalue())["components"],
            self.components,
        )

        source = self.root / "source"
        source.write_text("content", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                main(
                    [
                        "fingerprint",
                        str(self.root),
                        "source",
                    ]
                ),
                0,
            )
        digest = stdout.getvalue().strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
