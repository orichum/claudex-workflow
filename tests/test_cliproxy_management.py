#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from integrations.common.cliproxy_management import (
    ManagementEndpoint,
    ManagementError,
    _open_attested_connection,
    attest_owned_connection,
    load_management_endpoint,
    patch_auth_fields,
)


class _Response:
    status = 200

    def read(self, _limit: int) -> bytes:
        return b'{"status":"ok"}'


class _Connection:
    def __init__(self) -> None:
        self.request_args = None
        self.closed = False

    def request(self, *args, **kwargs) -> None:
        self.request_args = (args, kwargs)

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        self.closed = True


class _Socket:
    def __init__(self) -> None:
        self.timeout = None
        self.closed = False

    def settimeout(self, value: int) -> None:
        self.timeout = value

    def getsockname(self):
        return ("127.0.0.1", 48123)

    def close(self) -> None:
        self.closed = True


class CliProxyManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.data.mkdir(mode=0o700)
        self.ports = self.data / "service-ports.json"
        self.key = self.data / "cliproxy-management.key"
        self.ports.write_text(
            json.dumps({"cliproxyPort": 18317}), encoding="utf-8"
        )
        self.key.write_text("a" * 48 + "\n", encoding="ascii")
        self.ports.chmod(0o600)
        self.key.chmod(0o600)

    def test_loads_only_private_loopback_endpoint_state(self) -> None:
        self.assertEqual(
            load_management_endpoint(self.data),
            ManagementEndpoint(
                port=18317, data_home=self.data.resolve(), key="a" * 48
            ),
        )
        self.assertNotIn("a" * 48, repr(load_management_endpoint(self.data)))
        for path in (self.ports, self.key):
            path.chmod(0o644)
            with self.subTest(path=path), self.assertRaises(ManagementError):
                load_management_endpoint(self.data)
            path.chmod(0o600)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)

    def test_patch_uses_bounded_authenticated_local_request(self) -> None:
        endpoint = load_management_endpoint(self.data)
        connection = _Connection()
        with mock.patch(
            "integrations.common.cliproxy_management._open_attested_connection",
            return_value=connection,
        ) as opened:
            patch_auth_fields(
                endpoint,
                "codex-e200239f-arvind9981@gmail.com-pro.json",
                {"prefix": "oc-r-0123456789abcdef", "priority": 100},
            )
        opened.assert_called_once_with(endpoint)
        self.assertTrue(connection.closed)
        arguments, keywords = connection.request_args
        self.assertEqual(
            arguments[:2],
            ("PATCH", "/v0/management/auth-files/fields"),
        )
        self.assertEqual(
            keywords["headers"]["X-Management-Key"], "a" * 48
        )
        self.assertEqual(
            json.loads(keywords["body"]),
            {
                "name": "codex-e200239f-arvind9981@gmail.com-pro.json",
                "prefix": "oc-r-0123456789abcdef",
                "priority": 100,
            },
        )

    def test_rejects_unbounded_targets_fields_and_values(self) -> None:
        endpoint = ManagementEndpoint(
            port=18317, data_home=self.data, key="a" * 48
        )
        cases = (
            ("../credential.json", {"priority": 1}),
            ("credential.json", {"token": "secret"}),
            ("credential.json", {"prefix": "../bad"}),
            ("credential.json", {"priority": 1001}),
            ("credential.json", {}),
        )
        for reference, fields in cases:
            with self.subTest(reference=reference, fields=fields):
                with self.assertRaises(ManagementError):
                    patch_auth_fields(endpoint, reference, fields)

    def test_connection_is_attested_after_connect_before_header_use(self) -> None:
        endpoint = ManagementEndpoint(
            port=18317, data_home=self.data, key="a" * 48
        )
        connected = _Socket()
        with (
            mock.patch(
                "integrations.common.cliproxy_management.socket.create_connection",
                return_value=connected,
            ),
            mock.patch(
                "integrations.common.cliproxy_management.attest_owned_connection"
            ) as attest,
        ):
            connection = _open_attested_connection(endpoint)
        attest.assert_called_once_with(endpoint, 48123)
        self.assertIs(connection.sock, connected)
        self.assertEqual(connected.timeout, 3)

    def test_attestation_uses_fixed_argv_and_rejects_nonzero_verifier(self) -> None:
        verifier = self.data / "verify"
        verifier.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        verifier.chmod(0o700)
        endpoint = ManagementEndpoint(
            port=18317, data_home=self.data, key="a" * 48
        )
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch(
                "integrations.common.cliproxy_management.subprocess.run",
                return_value=completed,
            ) as run:
            attest_owned_connection(endpoint, 48123, verifier=verifier)
        self.assertEqual(
            run.call_args.args[0],
            [str(verifier), str(self.data), "18317", "48123"],
        )

        with (
            mock.patch(
                "integrations.common.cliproxy_management.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1),
            ),
            self.assertRaises(ManagementError),
        ):
            attest_owned_connection(endpoint, 48123, verifier=verifier)


if __name__ == "__main__":
    unittest.main()
