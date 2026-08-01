#!/usr/bin/env python3
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import threading
import time
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest import mock

from integrations.common.account_registry import Account
from integrations.common.stack_catalog import (
    CatalogError,
    MAX_MODEL_CATALOG_BYTES,
    classify_model,
    fetch_live_catalog,
    project_live_catalog,
)
from integrations.common.stack_definition import ModelDefinition


class StackCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.known_models = {
            "claude-sonnet-5": ModelDefinition(
                family="claude",
                routes=MappingProxyType(
                    {"anthropic": "claude-sonnet-5"}
                ),
            ),
            "cross-provider": ModelDefinition(
                family="google",
                routes=MappingProxyType(
                    {"antigravity": "claude-exact-route"}
                ),
            ),
        }
        self.providers = {
            "schemaVersion": 1,
            "providers": {
                "anthropic": {
                    "type": "anthropic",
                    "transport": "cliproxy",
                    "authType": "claude",
                    "families": ["claude"],
                    "familyPrefixes": {"claude": ["claude-"]},
                },
                "antigravity": {
                    "type": "openai-compatible",
                    "transport": "cliproxy",
                    "authType": "antigravity",
                    "families": ["claude", "google"],
                    "familyPrefixes": {
                        "claude": ["claude-"],
                        "google": ["gemini-"],
                    },
                },
            },
            "accountPools": {
                "shared": {
                    "providers": ["anthropic", "antigravity"]
                }
            },
            "fallbackRoutes": {
                "claude": ["anthropic", "antigravity"],
                "google": ["antigravity"],
            },
        }
        self.accounts = (
            self.account(
                "0000000000000001",
                "Claude One",
                "anthropic",
                "active",
            ),
            self.account(
                "0000000000000002",
                "Claude Two",
                "anthropic",
                "active",
            ),
            self.account(
                "0000000000000003",
                "Disabled Claude",
                "anthropic",
                "disabled",
            ),
            self.account(
                "0000000000000004",
                "Antigravity",
                "antigravity",
                "active",
            ),
        )

    @staticmethod
    def account(
        suffix: str, name: str, provider: str, state: str
    ) -> Account:
        return Account(
            id=f"oc-a-{suffix}",
            name=name,
            provider=provider,
            credential_ref=f"{name.lower().replace(' ', '-')}.json",
            pool="shared",
            routing_prefix=f"oc-r-{suffix}",
            priority=100,
            state=state,
            original_prefix=None,
            original_priority=None,
        )

    def test_groups_accounts_without_exposing_prefixes(self) -> None:
        catalog = project_live_catalog(
            {
                "object": "list",
                "data": [
                    {"id": "oc-r-0000000000000001/claude-sonnet-5"},
                    {"id": "oc-r-0000000000000002/claude-sonnet-5"},
                ],
            },
            self.accounts,
            self.known_models,
            self.providers,
        )

        self.assertEqual(len(catalog.choices), 1)
        self.assertEqual(catalog.choices[0].provider, "anthropic")
        self.assertEqual(catalog.choices[0].upstream, "claude-sonnet-5")
        self.assertEqual(
            catalog.choices[0].account_names,
            ("Claude One", "Claude Two"),
        )
        self.assertNotIn("oc-r-", repr(catalog.choices[0]))

    def test_ignores_unknown_disabled_and_unregistered_prefixes(self) -> None:
        catalog = project_live_catalog(
            {
                "object": "list",
                "data": [
                    {"id": "oc-r-0000000000000001/claude-sonnet-5"},
                    {"id": "oc-r-0000000000000003/claude-opus-4-8"},
                    {"id": "oc-r-ffffffffffffffff/claude-future"},
                    {"id": "claude-unprefixed"},
                ],
            },
            self.accounts,
            self.known_models,
            self.providers,
        )

        self.assertEqual(
            [choice.upstream for choice in catalog.choices],
            ["claude-sonnet-5"],
        )

    def test_unclassified_model_is_visible_but_not_selectable(self) -> None:
        catalog = project_live_catalog(
            {
                "object": "list",
                "data": [
                    {"id": "oc-r-0000000000000004/future-model"},
                ],
            },
            self.accounts,
            self.known_models,
            self.providers,
        )

        self.assertEqual(catalog.unclassified[0].upstream, "future-model")
        self.assertEqual(
            catalog.unclassified[0].account_names, ("Antigravity",)
        )
        self.assertEqual(catalog.choices, ())

    def test_exact_known_route_precedes_provider_prefix(self) -> None:
        self.assertEqual(
            classify_model(
                "antigravity",
                "claude-exact-route",
                self.known_models,
                self.providers,
            ),
            "google",
        )

    def test_sorts_choices_and_accounts_deterministically(self) -> None:
        catalog = project_live_catalog(
            {
                "object": "list",
                "data": [
                    {"id": "oc-r-0000000000000004/gemini-2.5-pro"},
                    {"id": "oc-r-0000000000000002/claude-z"},
                    {"id": "oc-r-0000000000000001/claude-z"},
                    {"id": "oc-r-0000000000000001/claude-a"},
                ],
            },
            tuple(reversed(self.accounts)),
            self.known_models,
            self.providers,
        )

        self.assertEqual(
            [
                (choice.provider, choice.family, choice.upstream)
                for choice in catalog.choices
            ],
            [
                ("anthropic", "claude", "claude-a"),
                ("anthropic", "claude", "claude-z"),
                ("antigravity", "google", "gemini-2.5-pro"),
            ],
        )
        self.assertEqual(
            catalog.choices[1].account_names,
            ("Claude One", "Claude Two"),
        )

    def test_rejects_invalid_catalog_shape(self) -> None:
        for raw in (
            None,
            {},
            {"object": "list", "data": {}},
            {"object": "list", "data": [{"id": "unsafe\nmodel"}]},
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(CatalogError):
                    project_live_catalog(
                        raw,
                        self.accounts,
                        self.known_models,
                        self.providers,
                    )

    def test_fetch_uses_bounded_loopback_request_and_unique_json(self) -> None:
        payload = json.dumps(
            {"object": "list", "data": [{"id": "gpt-5.6-sol"}]}
        ).encode("utf-8")
        response = SimpleNamespace(
            status=200,
            read=mock.Mock(return_value=payload),
        )
        connection = mock.MagicMock()
        connection.getresponse.return_value = response

        with mock.patch(
            "integrations.common.stack_catalog.http.client.HTTPConnection",
            return_value=connection,
        ) as connect:
            document = fetch_live_catalog(8317)

        connect.assert_called_once_with("127.0.0.1", 8317, timeout=4.0)
        connection.request.assert_called_once_with("GET", "/v1/models")
        response.read.assert_called_once_with(MAX_MODEL_CATALOG_BYTES + 1)
        connection.close.assert_called_once_with()
        self.assertEqual(document["data"][0]["id"], "gpt-5.6-sol")

        response.read.return_value = b'{"data":[],"data":[]}'
        with mock.patch(
            "integrations.common.stack_catalog.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(CatalogError):
                fetch_live_catalog(8317)

    def test_fetch_can_attest_the_exact_loopback_connection(self) -> None:
        payload = b'{"object":"list","data":[]}'
        response = SimpleNamespace(
            status=200,
            read=mock.Mock(return_value=payload),
        )
        connection = mock.MagicMock()
        connection.sock.getsockname.return_value = (
            "127.0.0.1",
            45678,
        )
        connection.getresponse.return_value = response
        attest = mock.Mock()

        with mock.patch(
            "integrations.common.stack_catalog.http.client.HTTPConnection",
            return_value=connection,
        ):
            document = fetch_live_catalog(8317, attest=attest)

        attest.assert_called_once_with(45678)
        connection.request.assert_called_once_with("GET", "/v1/models")
        self.assertEqual(document, {"object": "list", "data": []})

    def test_fetch_rejects_redirects_oversize_and_invalid_ports(self) -> None:
        response = SimpleNamespace(
            status=302,
            read=mock.Mock(return_value=b"{}"),
        )
        connection = mock.MagicMock()
        connection.getresponse.return_value = response
        with mock.patch(
            "integrations.common.stack_catalog.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(CatalogError):
                fetch_live_catalog(8317)
        response.read.assert_not_called()

        response.status = 200
        response.read.return_value = b"x" * (MAX_MODEL_CATALOG_BYTES + 1)
        with mock.patch(
            "integrations.common.stack_catalog.http.client.HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaises(CatalogError):
                fetch_live_catalog(8317)

        for port in (True, 0, 1023, 65536):
            with self.subTest(port=port):
                with self.assertRaises(CatalogError):
                    fetch_live_catalog(port)

    def test_fetch_rejects_invalid_timeout_before_opening_socket(self) -> None:
        invalid = (
            None,
            "1",
            True,
            False,
            0,
            -1,
            math.nan,
            math.inf,
            -math.inf,
        )
        for timeout in invalid:
            with self.subTest(timeout=timeout):
                with mock.patch(
                    "integrations.common.stack_catalog.http.client.HTTPConnection"
                ) as connect:
                    with self.assertRaises(CatalogError):
                        fetch_live_catalog(8317, timeout=timeout)
                connect.assert_not_called()

    def test_fetch_reads_http_10_body_after_connection_socket_closes(self) -> None:
        payload = b'{"object":"list","data":[]}'

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.flush()
                time.sleep(0.05)
                self.wfile.write(payload)

            def log_message(self, _format: str, *_arguments: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            document = fetch_live_catalog(server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=1)

        self.assertEqual(document, {"object": "list", "data": []})

    def test_fetch_enforces_total_deadline_during_slow_body(self) -> None:
        class Clock:
            value = 0.0

            def monotonic(self) -> float:
                return self.value

        clock = Clock()
        body = bytearray(b'{"data":[]}')
        header = bytearray(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
        )

        class SlowSocket:
            def __init__(self) -> None:
                self.timeout = None

            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout

            def sendall(self, _payload: bytes) -> None:
                return

            def dup(self):
                return self

            def recv_into(self, target: bytearray) -> int:
                source = header if header else body
                if not source:
                    return 0
                count = len(source) if header else 1
                target[:count] = source[:count]
                del source[:count]
                if source is body:
                    clock.value += 0.3
                return count

            def makefile(self, _mode: str):
                outer = self

                class SlowFile:
                    def readline(self, limit: int = -1) -> bytes:
                        if not header:
                            return b""
                        newline = header.index(b"\n") + 1
                        count = (
                            newline
                            if limit < 0
                            else min(newline, limit)
                        )
                        result = bytes(header[:count])
                        del header[:count]
                        return result

                    def read(self, count: int = -1) -> bytes:
                        amount = (
                            len(body)
                            if count < 0
                            else min(count, len(body))
                        )
                        result = bytes(body[:amount])
                        del body[:amount]
                        clock.value += 0.3 * amount
                        return result

                    def close(self) -> None:
                        outer.close()

                return SlowFile()

            def close(self) -> None:
                return

        class SlowConnection:
            def __init__(self) -> None:
                self.sock = None

            def connect(self) -> None:
                self.sock = SlowSocket()

            def request(self, _method: str, _path: str) -> None:
                if self.sock is None:
                    self.connect()
                self.sock.sendall(b"request")

            def getresponse(self) -> http.client.HTTPResponse:
                response = http.client.HTTPResponse(
                    self.sock, method="GET"
                )
                response.begin()
                return response

            def close(self) -> None:
                if self.sock is not None:
                    self.sock.close()

        connection = SlowConnection()
        with (
            mock.patch(
                "integrations.common.stack_catalog.http.client.HTTPConnection",
                return_value=connection,
            ),
            mock.patch(
                "time.monotonic",
                side_effect=clock.monotonic,
            ),
        ):
            with self.assertRaisesRegex(CatalogError, "deadline"):
                fetch_live_catalog(8317, timeout=1.0)


if __name__ == "__main__":
    unittest.main()
