#!/usr/bin/env python3
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common.model_routing import ROLES
from integrations.common.orichum_sessions import RouteBinding
from integrations.common.route_proxy import (
    Cooldowns,
    MAX_REQUEST_BYTES,
    ProxyConfig,
    RouteProxyError,
    RequestTooLarge,
    RouteProxyServer,
    RouteIndex,
    _read_request_body,
)
from integrations.common.route_selection import Route


class StaticRouteIndex:
    def __init__(self, routes: dict[str, str]):
        self.routes = routes

    def fallback_for(
        self, _session_id: str | None, primary_model: str
    ) -> str | None:
        return self.routes.get(primary_model)


class RecordingUpstream:
    def __init__(self, responses: list[tuple[int, bytes]]):
        self.responses = list(responses)
        self.models: list[str | None] = []
        self.paths: list[str] = []
        self.session_headers: list[str | None] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                owner.paths.append(self.path)
                length = int(self.headers["Content-Length"])
                document = json.loads(self.rfile.read(length))
                owner.models.append(document.get("model"))
                owner.session_headers.append(
                    self.headers.get("X-Orichum-Session-ID")
                )
                status, body = owner.responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> RecordingUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class TruncatedUpstream:
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"partial")
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> TruncatedUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProxyHarness:
    def __init__(
        self,
        upstream_port: int,
        routes: dict[str, str],
        *,
        cooldowns: Cooldowns | None = None,
    ):
        self.server = RouteProxyServer(
            ("127.0.0.1", 0),
            ProxyConfig(upstream_port, Path("/unused")),
            route_index=StaticRouteIndex(routes),
            cooldowns=cooldowns,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def __enter__(self) -> ProxyHarness:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(
        self, model: str, path: str = "/v1/messages"
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        body = json.dumps({"model": model, "messages": []}).encode()
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "X-Orichum-Session-ID": "oc-s-0000000000000001",
            },
        )
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def get(self, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        connection.request("GET", path)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload


class RouteProxyTests(unittest.TestCase):
    primary = "oc-r-0000000000000001/gpt-5.6-sol"
    fallback = "oc-r-0000000000000002/gpt-5.6-sol"

    def test_health_is_local_and_does_not_require_upstream(self) -> None:
        with ProxyHarness(65535, {}) as proxy:
            status, body = proxy.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "pid": os.getpid(),
                "ready": True,
                "service": "orichum-route-proxy",
            },
        )

    def test_normalizes_claudex_profile_path_for_cliproxy(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(
                    self.primary,
                    "/proxy/gpt/v1/messages?beta=true",
                )
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.paths, ["/v1/messages?beta=true"])

    def test_normalizes_claudex_profile_root_with_query(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(
                    self.primary,
                    "/proxy/gpt?probe=1",
                )
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.paths, ["/?probe=1"])

    def test_rejects_unowned_claudex_profile_path(self) -> None:
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post(
                    self.primary,
                    "/proxy/unowned/v1/messages",
                )
        self.assertEqual(status, 502)
        self.assertEqual(upstream.paths, [])

    def test_rejects_oversized_content_length_before_reading(self) -> None:
        handler = SimpleNamespace(
            headers={"Content-Length": str(MAX_REQUEST_BYTES + 1)},
            rfile=io.BytesIO(b""),
        )
        with self.assertRaises(RequestTooLarge):
            _read_request_body(handler)

    def test_rejects_chunked_body_when_aggregate_exceeds_limit(self) -> None:
        handler = SimpleNamespace(
            headers={"Transfer-Encoding": "chunked"},
            rfile=io.BytesIO(
                b"4\r\n"
                + b"xxxx"
                + b"\r\n1\r\ny\r\n0\r\n\r\n"
            ),
        )
        with mock.patch(
            "integrations.common.route_proxy.MAX_REQUEST_BYTES", 4
        ):
            with self.assertRaises(RequestTooLarge):
                _read_request_body(handler)

    def binding(self, primary: str, fallback: str | None) -> RouteBinding:
        def route(upstream: str, suffix: str) -> Route:
            return Route(
                account_id=f"oc-a-{suffix}",
                provider="openai",
                family="gpt",
                logical_model="gpt-5.6-sol",
                upstream_model=upstream,
                claudex_profile=f"ocp-{suffix}",
                priority=100,
                pool="shared",
            )

        return RouteBinding(
            route(primary, "0000000000000001"),
            (
                (route(fallback, "0000000000000002"),)
                if fallback is not None
                else ()
            ),
        )

    def test_route_index_uses_only_the_calling_logical_session(self) -> None:
        bound = self.binding(self.primary, self.fallback)
        session = SimpleNamespace(
            controller=bound,
            agents={role: bound for role in ROLES},
        )
        with mock.patch(
            "integrations.common.orichum_sessions.load_logical_session",
            return_value=session,
        ) as load:
            selected = RouteIndex(Path("/state")).fallback_for(
                "oc-s-0000000000000001", self.primary
            )

        self.assertEqual(selected, self.fallback)
        load.assert_called_once_with(
            Path("/state"), "oc-s-0000000000000001"
        )
        self.assertIsNone(
            RouteIndex(Path("/state")).fallback_for(None, self.primary)
        )

    def test_successful_primary_is_not_retried_or_rewritten(self) -> None:
        with RecordingUpstream([(200, b"primary")]) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (200, b"primary"))
        self.assertEqual(upstream.models, [self.primary])
        self.assertEqual(upstream.session_headers, [None])

    def test_retryable_status_uses_one_fallback_and_preserves_result(self) -> None:
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (200, b"fallback"))
        self.assertEqual(upstream.models, [self.primary, self.fallback])

    def test_cooldown_skips_known_exhausted_primary(self) -> None:
        clock = [100.0]
        cooldowns = Cooldowns(60, clock=lambda: clock[0])
        with RecordingUpstream(
            [(429, b"limited"), (200, b"first"), (200, b"second")]
        ) as upstream:
            with ProxyHarness(
                upstream.port,
                {self.primary: self.fallback},
                cooldowns=cooldowns,
            ) as proxy:
                self.assertEqual(proxy.post(self.primary), (200, b"first"))
                self.assertEqual(proxy.post(self.primary), (200, b"second"))

        self.assertEqual(
            upstream.models,
            [self.primary, self.fallback, self.fallback],
        )

    def test_without_fallback_returns_the_original_failure(self) -> None:
        with RecordingUpstream([(429, b"limited")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual((status, body), (429, b"limited"))
        self.assertEqual(upstream.models, [self.primary])

    def test_connection_failure_does_not_attempt_a_fallback(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        unavailable_port = listener.getsockname()[1]
        listener.close()
        with ProxyHarness(
            unavailable_port, {self.primary: self.fallback}
        ) as proxy:
            status, _body = proxy.post(self.primary)

        self.assertEqual(status, 502)

    def test_truncated_response_closes_without_writing_a_second_response(self) -> None:
        with TruncatedUpstream() as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, body = proxy.post(self.primary)

        self.assertEqual(status, 200)
        self.assertNotIn(b"HTTP/1.1 502", body)
        self.assertNotIn(b"Orichum upstream connection failed", body)

    def test_non_loopback_listener_is_rejected(self) -> None:
        with self.assertRaisesRegex(RouteProxyError, "127.0.0.1"):
            RouteProxyServer(
                ("0.0.0.0", 0),
                ProxyConfig(8317, Path("/unused")),
                route_index=StaticRouteIndex({}),
            )


if __name__ == "__main__":
    unittest.main()
