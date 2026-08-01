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
    cancel_oauth,
    _open_attested_connection,
    attest_owned_connection,
    load_management_endpoint,
    oauth_status,
    patch_auth_fields,
    start_oauth,
    submit_oauth_callback,
)


class _Response:
    def __init__(self, body: bytes = b'{"status":"ok"}', status: int = 200):
        self.body = body
        self.status = status

    def read(self, _limit: int) -> bytes:
        return self.body


class _Connection:
    def __init__(self, response: _Response | None = None) -> None:
        self.request_args = None
        self.closed = False
        self.response = response or _Response()

    def request(self, *args, **kwargs) -> None:
        self.request_args = (args, kwargs)

    def getresponse(self) -> _Response:
        return self.response

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

    def test_oauth_start_uses_structured_authenticated_management_api(
        self,
    ) -> None:
        endpoint = load_management_endpoint(self.data)
        url = (
            "https://auth.openai.com/oauth/authorize?state=secret-state"
        )
        connection = _Connection(
            _Response(
                json.dumps(
                    {
                        "status": "ok",
                        "url": url,
                        "state": "secret-state",
                    }
                ).encode()
            )
        )
        with mock.patch(
            "integrations.common.cliproxy_management._open_attested_connection",
            return_value=connection,
        ):
            session = start_oauth(endpoint, "codex")

        self.assertEqual(session.provider, "codex")
        self.assertEqual(session.url, url)
        self.assertEqual(session.state, "secret-state")
        self.assertNotIn(url, repr(session))
        self.assertNotIn("secret-state", repr(session))
        arguments, keywords = connection.request_args
        self.assertEqual(
            arguments,
            (
                "GET",
                "/v0/management/codex-auth-url?is_webui=true",
            ),
        )
        self.assertEqual(
            keywords["headers"]["X-Management-Key"], "a" * 48
        )
        self.assertTrue(connection.closed)

    def test_oauth_status_and_cancel_use_state_bound_requests(self) -> None:
        endpoint = load_management_endpoint(self.data)
        waiting = _Connection(_Response(b'{"status":"wait"}'))
        completed = _Connection(
            _Response(b'{"status":"ok","cancelled":true}')
        )
        with mock.patch(
            "integrations.common.cliproxy_management._open_attested_connection",
            side_effect=(waiting, completed),
        ):
            self.assertEqual(oauth_status(endpoint, "state-123"), "wait")
            self.assertTrue(cancel_oauth(endpoint, "state-123"))

        self.assertEqual(
            waiting.request_args[0],
            (
                "GET",
                "/v0/management/get-auth-status?state=state-123",
            ),
        )
        self.assertEqual(
            completed.request_args[0],
            (
                "DELETE",
                "/v0/management/oauth-session?state=state-123",
            ),
        )

    def test_oauth_callback_submission_is_loopback_and_state_bound(
        self,
    ) -> None:
        endpoint = load_management_endpoint(self.data)
        connection = _Connection()
        callback = (
            "http://localhost:1455/auth/callback?"
            "code=secret-code&state=state-123"
        )
        with mock.patch(
            "integrations.common.cliproxy_management._open_attested_connection",
            return_value=connection,
        ):
            submit_oauth_callback(endpoint, "state-123", callback)

        arguments, keywords = connection.request_args
        self.assertEqual(
            arguments[:2],
            ("POST", "/v0/management/oauth-callback"),
        )
        self.assertEqual(
            json.loads(keywords["body"]),
            {"redirect_url": callback, "state": "state-123"},
        )
        self.assertEqual(
            keywords["headers"]["X-Management-Key"],
            "a" * 48,
        )
        self.assertTrue(connection.closed)

    def test_oauth_callback_submission_rejects_unsafe_or_wrong_state_urls(
        self,
    ) -> None:
        endpoint = load_management_endpoint(self.data)
        callbacks = (
            "https://example.com/callback?code=x&state=state-123",
            "http://localhost:1455/auth/callback?code=x&state=other",
            "http://localhost:1455/auth/callback?state=state-123",
        )
        for callback in callbacks:
            with (
                self.subTest(callback=callback),
                mock.patch(
                    "integrations.common.cliproxy_management._open_attested_connection"
                ) as opened,
                self.assertRaises(ManagementError),
            ):
                submit_oauth_callback(endpoint, "state-123", callback)
            opened.assert_not_called()

    def test_oauth_management_rejects_malformed_or_failed_responses(
        self,
    ) -> None:
        endpoint = load_management_endpoint(self.data)
        responses = (
            _Response(b'{"status":"ok","url":"file:///tmp/x","state":"s"}'),
            _Response(b'{"status":"ok","url":"https://example.com","state":"../bad"}'),
            _Response(b'{"status":"error","error":"denied"}'),
            _Response(b"not-json"),
            _Response(b"{}", status=500),
        )
        for response in responses:
            with (
                self.subTest(response=response.body),
                mock.patch(
                    "integrations.common.cliproxy_management._open_attested_connection",
                    return_value=_Connection(response),
                ),
                self.assertRaises(ManagementError),
            ):
                start_oauth(endpoint, "codex")

        with self.assertRaises(ManagementError):
            start_oauth(endpoint, "unsupported")

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
