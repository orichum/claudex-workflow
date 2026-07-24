#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations.common.account_registry import Account
from integrations.common.route_selection import (
    RouteError,
    choose_new_session_route,
    eligible_routes,
    route_chain,
    validate_route_credential,
)


class RouteSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "models": {
                "claude-opus": {
                    "provider": "anthropic",
                    "family": "claude",
                    "upstream": "claude-opus",
                }
            },
            "providers": {
                "providers": {
                    "anthropic": {"authType": "claude"},
                    "antigravity": {"authType": "antigravity"},
                },
                "fallbackRoutes": {
                    "claude": ["anthropic", "antigravity"]
                },
                "accountPools": {
                    "work": {
                        "providers": ["anthropic", "antigravity"]
                    },
                    "shared": {
                        "providers": ["anthropic", "antigravity"]
                    },
                },
            },
        }

    def account(
        self,
        identifier: str,
        *,
        provider: str = "anthropic",
        pool: str = "work",
        priority: int = 100,
        state: str = "active",
    ) -> Account:
        return Account(
            id=identifier,
            name=identifier,
            provider=provider,
            credential_ref=f"{identifier}.json",
            pool=pool,
            routing_prefix=f"oc-{identifier}",
            priority=priority,
            state=state,
            original_prefix=None,
            original_priority=None,
        )

    def test_eligible_routes_restrict_pool_provider_family_and_enabled_state(self) -> None:
        accounts = (
            self.account("primary"),
            self.account("disabled", state="disabled"),
            self.account("shared", pool="shared"),
            self.account("wrong-provider", provider="kimi"),
        )

        routes = eligible_routes(
            accounts,
            pool="work",
            family="claude",
            logical_model="claude-opus",
            config=self.config,
        )

        self.assertEqual([route.account_id for route in routes], ["primary"])
        self.assertEqual(routes[0].upstream_model, "oc-primary/claude-opus")
        self.assertNotIn("primary", routes[0].claudex_profile)

    def test_highest_healthy_priority_wins_before_lower_tiers(self) -> None:
        accounts = (
            self.account("reserve", priority=10),
            self.account("secondary", priority=50),
            self.account("primary", priority=100),
        )

        selected = choose_new_session_route(
            accounts,
            pools=("work", "shared"),
            family="claude",
            logical_model="claude-opus",
            config=self.config,
            health={},
            selection_ordinal=0,
        )

        self.assertEqual(selected.account_id, "primary")

    def test_unhealthy_accounts_fall_through_tier_then_pool(self) -> None:
        accounts = (
            self.account("work-primary", priority=100),
            self.account("work-secondary", priority=50),
            self.account("shared-primary", pool="shared", priority=100),
        )
        selected = choose_new_session_route(
            accounts,
            pools=("work", "shared"),
            family="claude",
            logical_model="claude-opus",
            config=self.config,
            health={
                "work-primary": "cooldown",
                "work-secondary": "healthy",
                "shared-primary": "healthy",
            },
            selection_ordinal=0,
        )
        self.assertEqual(selected.account_id, "work-secondary")

        selected = choose_new_session_route(
            accounts,
            pools=("work", "shared"),
            family="claude",
            logical_model="claude-opus",
            config=self.config,
            health={
                "work-primary": "cooldown",
                "work-secondary": "unavailable",
                "shared-primary": "healthy",
            },
            selection_ordinal=0,
        )
        self.assertEqual(selected.account_id, "shared-primary")

    def test_equal_priority_distribution_is_deterministic_and_complete(self) -> None:
        accounts = (
            self.account("account-a"),
            self.account("account-b"),
        )
        selected = [
            choose_new_session_route(
                accounts,
                pools=("work",),
                family="claude",
                logical_model="claude-opus",
                config=self.config,
                health={},
                selection_ordinal=ordinal,
            ).account_id
            for ordinal in range(4)
        ]
        self.assertEqual(selected, ["account-a", "account-b"] * 2)

    def test_fallback_provider_order_breaks_equal_priority_ties(self) -> None:
        accounts = (
            self.account("anthropic", provider="anthropic"),
            self.account("antigravity", provider="antigravity"),
        )
        first = eligible_routes(
            accounts,
            pool="work",
            family="claude",
            logical_model="claude-opus",
            config=self.config,
        )
        self.assertEqual(
            [route.provider for route in first],
            ["anthropic", "antigravity"],
        )

        self.config["providers"]["fallbackRoutes"]["claude"].reverse()
        reversed_routes = eligible_routes(
            accounts,
            pool="work",
            family="claude",
            logical_model="claude-opus",
            config=self.config,
        )
        self.assertEqual(
            [route.provider for route in reversed_routes],
            ["antigravity", "anthropic"],
        )

    def test_no_healthy_route_fails_closed(self) -> None:
        with self.assertRaises(RouteError):
            choose_new_session_route(
                (self.account("primary"),),
                pools=("work",),
                family="claude",
                logical_model="claude-opus",
                config=self.config,
                health={"primary": "quota"},
                selection_ordinal=0,
            )

    def test_route_chain_is_same_model_bounded_and_pool_ordered(self) -> None:
        accounts = (
            self.account("work-primary", priority=100),
            self.account("work-secondary", priority=50),
            self.account("shared-primary", pool="shared", priority=100),
        )
        chain = route_chain(
            accounts,
            pools=("work", "shared"),
            family="claude",
            logical_model="claude-opus",
            config=self.config,
            health={},
            selection_ordinal=0,
        )
        self.assertEqual(
            [route.account_id for route in chain],
            ["work-primary", "work-secondary"],
        )
        self.assertEqual({route.family for route in chain}, {"claude"})
        self.assertEqual({route.logical_model for route in chain}, {"claude-opus"})
        with self.assertRaises(RouteError):
            route_chain(
                accounts,
                pools=("work",),
                family="claude",
                logical_model="claude-opus",
                config=self.config,
                health={},
                selection_ordinal=0,
                max_alternates=2,
            )

    def test_route_chain_skips_provider_without_the_exact_live_model(
        self,
    ) -> None:
        accounts = (
            self.account("anthropic", provider="anthropic", priority=100),
            self.account(
                "antigravity", provider="antigravity", priority=50
            ),
        )
        chain = route_chain(
            accounts,
            pools=("work",),
            family="claude",
            logical_model="claude-opus",
            config=self.config,
            health={},
            selection_ordinal=0,
            available_models={"oc-anthropic/claude-opus"},
        )

        self.assertEqual(
            [route.account_id for route in chain],
            ["anthropic"],
        )

    def test_activation_revalidates_live_provider_disabled_state_and_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            auth_dir = Path(temporary)
            auth_dir.chmod(0o700)
            account = self.account("primary")
            credential = auth_dir / account.credential_ref

            def write(**changes: object) -> None:
                document = {
                    "type": "claude",
                    "email": "work@example.com",
                    "prefix": account.routing_prefix,
                    "priority": account.priority,
                    "disabled": False,
                    **changes,
                }
                credential.write_text(json.dumps(document), encoding="utf-8")
                credential.chmod(0o600)

            route = eligible_routes(
                (account,),
                pool="work",
                family="claude",
                logical_model="claude-opus",
                config=self.config,
            )[0]
            write()
            validate_route_credential(
                route,
                (account,),
                auth_dir=auth_dir,
                provider_document=self.config["providers"],
            )

            for changes in (
                {"disabled": True},
                {"prefix": "oc-other"},
                {"priority": 99},
                {"type": "codex"},
            ):
                write(**changes)
                with self.subTest(changes=changes), self.assertRaises(
                    RouteError
                ):
                    validate_route_credential(
                        route,
                        (account,),
                        auth_dir=auth_dir,
                        provider_document=self.config["providers"],
                    )


if __name__ == "__main__":
    unittest.main()
