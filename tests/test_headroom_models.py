from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest

from integrations.common.headroom_models import (
    HeadroomModelsError,
    MAX_REGISTRY_BYTES,
    build_catalog,
    main,
    validate_catalog,
)


class HeadroomModelsTests(unittest.TestCase):
    def fixture(self) -> dict[str, object]:
        return {
            "kimi": [
                {
                    "id": "kimi-k2.7-code",
                    "context_length": 262144,
                    "pricing": {"input": 99},
                }
            ],
            "antigravity": [
                {"id": "gemini-3.1-pro-preview", "inputTokenLimit": 1048576},
                {"id": "shared-model", "context_length": 200000},
            ],
            "other-route": [
                {"id": "shared-model", "context_length": 114000},
                {"id": "no-upstream-limit"},
            ],
            "metadata": {"ignored": True},
        }

    def build(self, registry: object | None = None) -> dict[str, object]:
        return build_catalog(
            self.fixture() if registry is None else registry,
            repository="router-for-me/CLIProxyAPI",
            tag="v7.2.95",
            version="7.2.95",
            registry_sha256="a" * 64,
        )

    def test_generates_anthropic_wire_limits_without_pricing(self) -> None:
        catalog = self.build()
        self.assertEqual(
            catalog["anthropic"]["context_limits"],
            {
                "gemini-3.1-pro-preview": 1048576,
                "kimi-k2.7-code": 262144,
                "shared-model": 114000,
            },
        )
        self.assertNotIn("pricing", json.dumps(catalog))

    def test_duplicate_routes_use_the_smallest_limit(self) -> None:
        limits = validate_catalog(self.build())
        self.assertEqual(limits["shared-model"], 114000)

    def test_two_supported_fields_use_the_smaller_value(self) -> None:
        catalog = self.build(
            {
                "provider": [
                    {
                        "id": "dual-limit",
                        "context_length": 262144,
                        "inputTokenLimit": 131072,
                    }
                ]
            }
        )
        self.assertEqual(
            catalog["anthropic"]["context_limits"]["dual-limit"], 131072
        )

    def test_rejects_unsafe_ids_and_invalid_limits(self) -> None:
        invalid_records = (
            {"id": "bad\nmodel", "context_length": 1000},
            {"id": "model", "context_length": True},
            {"id": "model", "context_length": 0},
            {"id": "model", "context_length": 2_000_001},
            {"id": "model", "inputTokenLimit": "1048576"},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaises(HeadroomModelsError):
                    self.build({"provider": [record]})

    def test_rejects_bad_provenance_and_empty_registry(self) -> None:
        with self.assertRaises(HeadroomModelsError):
            self.build({})
        catalog = self.build()
        catalog["source"]["version"] = "7.2.94"
        with self.assertRaises(HeadroomModelsError):
            validate_catalog(
                catalog,
                expected_repository="router-for-me/CLIProxyAPI",
                expected_version="7.2.95",
            )

    def test_rejects_non_integer_schema_versions(self) -> None:
        for invalid_version in (True, 1.0):
            with self.subTest(schemaVersion=invalid_version):
                catalog = self.build()
                catalog["schemaVersion"] = invalid_version
                with self.assertRaises(HeadroomModelsError):
                    validate_catalog(catalog)

    def test_serialized_output_is_stable(self) -> None:
        first = json.dumps(self.build(), sort_keys=True, indent=2) + "\n"
        second = json.dumps(self.build(), sort_keys=True, indent=2) + "\n"
        self.assertEqual(first, second)

    def test_generate_cli_records_exact_sha_and_writes_mode_0600(self) -> None:
        registry_bytes = (
            b'{"provider":[{"id":"model","context_length":4096}]}\n'
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry.json"
            output = root / "models.json"
            registry.write_bytes(registry_bytes)
            self.assertEqual(
                main(
                    [
                        "generate",
                        "--registry",
                        str(registry),
                        "--repository",
                        "router-for-me/CLIProxyAPI",
                        "--tag",
                        "v7.2.95",
                        "--version",
                        "7.2.95",
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            catalog = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                catalog["source"]["registrySha256"],
                hashlib.sha256(registry_bytes).hexdigest(),
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(
                main(
                    [
                        "validate",
                        "--catalog",
                        str(output),
                        "--expected-repository",
                        "router-for-me/CLIProxyAPI",
                        "--expected-version",
                        "7.2.95",
                    ]
                ),
                0,
            )

    def test_generate_cli_rejects_oversized_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry.json"
            output = root / "models.json"
            with registry.open("wb") as handle:
                handle.truncate(MAX_REGISTRY_BYTES + 1)
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "generate",
                            "--registry",
                            str(registry),
                            "--repository",
                            "router-for-me/CLIProxyAPI",
                            "--tag",
                            "v7.2.95",
                            "--version",
                            "7.2.95",
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(raised.exception.code, 1)
            self.assertFalse(output.exists())

    def test_generate_cli_refuses_symlink_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "registry.json"
            target = root / "target.json"
            output = root / "models.json"
            registry.write_text(
                '{"provider":[{"id":"model","context_length":4096}]}',
                encoding="utf-8",
            )
            target.write_text("sentinel\n", encoding="utf-8")
            output.symlink_to(target)
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "generate",
                            "--registry",
                            str(registry),
                            "--repository",
                            "router-for-me/CLIProxyAPI",
                            "--tag",
                            "v7.2.95",
                            "--version",
                            "7.2.95",
                            "--output",
                            str(output),
                        ]
                    )
            self.assertEqual(raised.exception.code, 1)
            self.assertTrue(output.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")


if __name__ == "__main__":
    unittest.main()
