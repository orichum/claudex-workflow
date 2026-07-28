#!/usr/bin/env python3
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common.model_routing import ROLES
from integrations.common.orichum_sessions import RouteBinding
from integrations.common.route_proxy import (
    AttestationGate,
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


def client_tool(name: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


class StaticRouteIndex:
    def __init__(self, routes: dict[str, str]):
        self.routes = routes

    @staticmethod
    def _route(model: str, suffix: str) -> Route:
        return Route(
            account_id=f"oc-a-{suffix}",
            provider="openai",
            family="gpt",
            logical_model=model.rsplit("/", 1)[-1],
            upstream_model=model,
            claudex_profile=f"ocp-{suffix}",
            priority=100,
            pool="shared",
        )

    def routes_for(
        self, _session_id: str | None, primary_model: str
    ) -> tuple[Route, Route | None] | None:
        fallback = self.routes.get(primary_model)
        return (
            self._route(primary_model, "0000000000000001"),
            (
                self._route(fallback, "0000000000000002")
                if fallback is not None
                else None
            ),
        )

    def fallback_for(
        self, _session_id: str | None, primary_model: str
    ) -> str | None:
        return self.routes.get(primary_model)


class RecordingUpstream:
    def __init__(self, responses: list[tuple[int, bytes]]):
        self.responses = list(responses)
        self.documents: list[dict[str, object]] = []
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
                owner.documents.append(document)
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
        data_home: Path | None = None,
    ):
        self.server = RouteProxyServer(
            ("127.0.0.1", 0),
            ProxyConfig(upstream_port, Path("/unused"), data_home),
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
        return self.post_document(
            {"model": model, "messages": []},
            path,
        )

    def post_document(
        self,
        document: dict[str, object],
        path: str = "/v1/messages",
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=3
        )
        body = json.dumps(document).encode()
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

    def test_health_is_not_ready_when_upstream_is_unavailable(self) -> None:
        rejected = subprocess.CompletedProcess([], 1, stdout="")
        with RecordingUpstream([]) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                return_value=rejected,
            ):
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    status, body = proxy.get("/health")
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {
                "pid": os.getpid(),
                "ready": False,
                "service": "orichum-route-proxy",
            },
        )

    def test_health_is_ready_when_upstream_is_connectable(self) -> None:
        with RecordingUpstream([]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.get("/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ready"])

    def test_concurrent_attestations_share_one_successful_refresh(self) -> None:
        gate = AttestationGate(30)
        barrier = threading.Barrier(24)
        full_calls = 0
        calls_lock = threading.Lock()
        connection_barrier = threading.Barrier(23)
        attested_ports: list[int] = []
        failures: list[BaseException] = []

        def full_verifier(client_port: int) -> int:
            nonlocal full_calls
            with calls_lock:
                full_calls += 1
                attested_ports.append(client_port)
            time.sleep(0.05)
            return 48123

        def connection_verifier(service_pid: int, client_port: int) -> None:
            self.assertEqual(service_pid, 48123)
            connection_barrier.wait(timeout=2)
            with calls_lock:
                attested_ports.append(client_port)

        def attest(client_port: int) -> None:
            try:
                barrier.wait()
                gate.verify(
                    client_port, full_verifier, connection_verifier
                )
            except BaseException as failure:
                failures.append(failure)

        threads = [
            threading.Thread(target=attest, args=(port,))
            for port in range(48000, 48024)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(failures, [])
        self.assertEqual(full_calls, 1)
        self.assertEqual(sorted(attested_ports), list(range(48000, 48024)))

    def test_successful_attestation_expires(self) -> None:
        clock = [100.0]
        gate = AttestationGate(30, clock=lambda: clock[0])
        full_ports: list[int] = []
        connection_ports: list[int] = []

        def full_verifier(client_port: int) -> int:
            full_ports.append(client_port)
            return 48123

        def connection_verifier(
            service_pid: int, client_port: int
        ) -> None:
            self.assertEqual(service_pid, 48123)
            connection_ports.append(client_port)

        gate.verify(48000, full_verifier, connection_verifier)
        clock[0] = 129.9
        gate.verify(48001, full_verifier, connection_verifier)
        clock[0] = 130.0
        gate.verify(48002, full_verifier, connection_verifier)

        self.assertEqual(full_ports, [48000, 48002])
        self.assertEqual(connection_ports, [48001])

    def test_failed_attestation_is_not_cached(self) -> None:
        gate = AttestationGate(30)
        calls = 0

        def full_verifier(_client_port: int) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RouteProxyError("rejected")
            return 48123

        with self.assertRaises(RouteProxyError):
            gate.verify(48000, full_verifier, mock.Mock())
        gate.verify(48001, full_verifier, mock.Mock())

        self.assertEqual(calls, 2)

    def test_concurrent_attestation_failure_is_shared_by_waiters(self) -> None:
        gate = AttestationGate(30)
        barrier = threading.Barrier(24)
        full_calls = 0
        calls_lock = threading.Lock()
        failures: list[BaseException] = []

        def failing_full_verifier(_client_port: int) -> int:
            nonlocal full_calls
            with calls_lock:
                full_calls += 1
            time.sleep(0.05)
            raise RouteProxyError("rejected")

        def attest(client_port: int) -> None:
            try:
                barrier.wait()
                gate.verify(client_port, failing_full_verifier, mock.Mock())
            except BaseException as failure:
                failures.append(failure)

        threads = [
            threading.Thread(target=attest, args=(port,))
            for port in range(48000, 48024)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(full_calls, 1)
        self.assertEqual(len(failures), 24)

        gate.verify(49000, lambda _port: 48123, mock.Mock())

    def test_connection_failure_refreshes_service_identity(self) -> None:
        gate = AttestationGate(30)
        identities = iter((48123, 48124))
        full_ports: list[int] = []

        def full_verifier(client_port: int) -> int:
            full_ports.append(client_port)
            return next(identities)

        def connection_verifier(
            service_pid: int, _client_port: int
        ) -> None:
            if service_pid == 48123:
                raise RouteProxyError("stale service")

        gate.verify(48000, full_verifier, connection_verifier)
        gate.verify(48001, full_verifier, connection_verifier)

        self.assertEqual(full_ports, [48000, 48001])

    def test_each_production_socket_is_attested(self) -> None:
        completed = (
            subprocess.CompletedProcess([], 0, stdout="40541\n"),
            subprocess.CompletedProcess([], 0, stdout=""),
        )
        with RecordingUpstream(
            [(200, b"first"), (200, b"second")]
        ) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                side_effect=completed,
            ) as verifier:
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    self.assertEqual(
                        proxy.post(self.primary), (200, b"first")
                    )
                    self.assertEqual(
                        proxy.post(self.primary), (200, b"second")
                    )

        first_arguments = verifier.call_args_list[0].args[0]
        second_arguments = verifier.call_args_list[1].args[0]
        self.assertEqual(first_arguments[1:3], ["/data", str(upstream.port)])
        self.assertEqual(
            second_arguments[1:4],
            ["--connection", "40541", str(upstream.port)],
        )
        self.assertNotEqual(first_arguments[-1], second_arguments[-1])

    def test_invalid_service_identity_is_rejected(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="not-a-pid\n")
        with RecordingUpstream([]) as upstream:
            with mock.patch(
                "integrations.common.route_proxy.subprocess.run",
                return_value=completed,
            ):
                with ProxyHarness(
                    upstream.port, {}, data_home=Path("/data")
                ) as proxy:
                    status, _body = proxy.post(self.primary)

        self.assertEqual(status, 502)

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

    def test_verified_request_defers_tools_before_forwarding(self) -> None:
        tools = [
            client_tool("mcp__leanctx__ctx_shell"),
            *[
                client_tool(f"mcp__docker__tool_{index}")
                for index in range(11)
            ],
        ]
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(
                    {"model": self.primary, "messages": [], "tools": tools}
                )
        self.assertEqual(status, 200)
        forwarded = upstream.documents[0]
        self.assertEqual(
            forwarded["tools"][-1]["type"],
            "tool_search_tool_regex_20251119",
        )

    def test_unknown_model_request_is_forwarded_unchanged(self) -> None:
        document = {
            "model": "oc-r-0000000000000001/future-model",
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        self.assertEqual(upstream.documents, [document])

    def test_400_from_transformed_request_retries_original_once(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(400, b"unsupported"), (200, b"ok")]
        ) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(len(upstream.documents), 2)
        self.assertIn("defer_loading", upstream.documents[0]["tools"][1])
        self.assertEqual(upstream.documents[1], document)

    def test_422_from_transformed_request_retries_original_once(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(422, b"unsupported"), (200, b"ok")]
        ) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(upstream.documents[1], document)

    def test_untransformed_400_is_returned_without_retry(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [client_tool("Bash")],
        }
        with RecordingUpstream([(400, b"invalid")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, body = proxy.post_document(document)
        self.assertEqual((status, body), (400, b"invalid"))
        self.assertEqual(upstream.documents, [document])

    def test_429_does_not_use_tool_compatibility_retry(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream([(429, b"quota")]) as upstream:
            with ProxyHarness(upstream.port, {}) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 429)
        self.assertEqual(len(upstream.documents), 1)

    def test_cooldown_selected_fallback_uses_its_logical_model(self) -> None:
        primary = "oc-r-0000000000000001/future-model"
        fallback = "oc-r-0000000000000002/gpt-5.6-sol"
        document = {
            "model": primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        cooldowns = Cooldowns(60)
        cooldowns.trip(primary)
        with RecordingUpstream([(200, b"ok")]) as upstream:
            with ProxyHarness(
                upstream.port,
                {primary: fallback},
                cooldowns=cooldowns,
            ) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        self.assertEqual(upstream.models, [fallback])
        self.assertEqual(
            upstream.documents[0]["tools"][-1]["type"],
            "tool_search_tool_regex_20251119",
        )

    def test_account_failover_changes_only_model(self) -> None:
        document = {
            "model": self.primary,
            "messages": [{"role": "user", "content": "test"}],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                status, _ = proxy.post_document(document)
        self.assertEqual(status, 200)
        primary_document, fallback_document = upstream.documents
        self.assertEqual(primary_document["model"], self.primary)
        self.assertEqual(fallback_document["model"], self.fallback)
        self.assertEqual(
            {**primary_document, "model": self.fallback},
            fallback_document,
        )

    def test_400_compatibility_retry_does_not_trip_cooldown(self) -> None:
        document = {
            "model": self.primary,
            "messages": [],
            "tools": [
                client_tool("mcp__leanctx__ctx_shell"),
                *[client_tool(f"tool_{index}") for index in range(11)],
            ],
        }
        with RecordingUpstream(
            [
                (400, b"unsupported"),
                (200, b"original"),
                (200, b"next"),
            ]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                first = proxy.post_document(document)
                second = proxy.post_document(document)
        self.assertEqual(first, (200, b"original"))
        self.assertEqual(second, (200, b"next"))
        self.assertEqual(
            upstream.models,
            [self.primary, self.primary, self.primary],
        )

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

    def test_status_endpoint_reports_the_active_fallback_account(self) -> None:
        with RecordingUpstream(
            [(429, b"limited"), (200, b"fallback")]
        ) as upstream:
            with ProxyHarness(
                upstream.port, {self.primary: self.fallback}
            ) as proxy:
                self.assertEqual(
                    proxy.post(self.primary),
                    (200, b"fallback"),
                )
                status, body = proxy.get(
                    "/status?session_id=oc-s-0000000000000001"
                )

        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["accountId"], "oc-a-0000000000000002")
        self.assertEqual(document["routeState"], "fallback")
        self.assertEqual(document["reason"], "retry")
        self.assertEqual(document["lastHttpStatus"], 200)

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
