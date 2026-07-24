#!/usr/bin/env python3
from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
