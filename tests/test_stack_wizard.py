#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import io
import os
from pathlib import Path
import pty
import struct
import termios
import threading
import unittest
from unittest import mock

from integrations.common import stack_wizard
from integrations.common.account_registry import Account
from integrations.common.orichum_config import ResolvedConfig
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_catalog import LiveCatalog, LiveModelChoice
from integrations.common.stack_definition import normalize_model_stacks
from integrations.common.stack_store import StackSnapshot
from integrations.common.stack_wizard import (
    BACK,
    Choice,
    StackWizard,
    TerminalWizardIO,
    WizardCancelled,
    WizardResult,
    run_stack_wizard,
)


def _account(
    identifier: str,
    name: str,
    provider: str,
    prefix: str,
) -> Account:
    return Account(
        id=identifier,
        name=name,
        provider=provider,
        credential_ref=f"{provider}.json",
        pool="shared",
        routing_prefix=prefix,
        priority=100,
        state="active",
        original_prefix=None,
        original_priority=None,
    )


class ScriptedIO:
    """Select named choices while accepting inherited defaults in between."""

    def __init__(
        self,
        *,
        choices: list[str] | None = None,
        confirmations: list[bool] | None = None,
        text: list[str] | None = None,
        cancel_at: int | None = None,
        disconnect_at: int | None = None,
        back_at: int | None = None,
    ) -> None:
        self.choices = list(choices or [])
        self.confirmations = list(confirmations or [])
        self.text_values = list(text or [])
        self.cancel_at = cancel_at
        self.disconnect_at = disconnect_at
        self.back_at = back_at
        self.calls = 0
        self.shown: list[str] = []
        self.titles: list[str] = []

    def _before(self) -> None:
        self.calls += 1
        if self.cancel_at == self.calls:
            raise KeyboardInterrupt
        if self.disconnect_at == self.calls:
            raise EOFError

    def choose(
        self,
        title: str,
        options: list[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int:
        self._before()
        self.titles.append(title)
        if self.back_at == self.calls:
            return BACK
        labels = [option.label for option in options]
        if self.choices and self.choices[0] in labels:
            return labels.index(self.choices.pop(0))
        return selected

    def confirm(self, prompt: str, default: bool = False) -> bool:
        self._before()
        if self.confirmations:
            return self.confirmations.pop(0)
        return default

    def text(self, prompt: str, initial: str = "") -> str:
        self._before()
        if self.text_values:
            return self.text_values.pop(0)
        return initial

    def show(self, summary: str) -> None:
        self.shown.append(summary)


class StackWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accounts = (
            _account(
                "oc-a-aaaaaaaaaaaaaaaa",
                "Work OpenAI",
                "openai",
                "oc-r-aaaaaaaaaaaaaaaa",
            ),
            _account(
                "oc-a-bbbbbbbbbbbbbbbb",
                "Work Claude",
                "anthropic",
                "oc-r-bbbbbbbbbbbbbbbb",
            ),
            _account(
                "oc-a-cccccccccccccccc",
                "Personal Antigravity",
                "antigravity",
                "oc-r-cccccccccccccccc",
            ),
        )
        self.stacks = normalize_model_stacks(
            {
                "schemaVersion": 2,
                "defaultStack": "balanced",
                "models": {
                    "gpt-5.6-sol": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-5.6-sol"},
                    },
                    "gpt-5.6-terra": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-5.6-terra"},
                    },
                    "claude-sonnet-5": {
                        "family": "claude",
                        "routes": {"anthropic": "claude-sonnet-5"},
                    },
                    "claude-opus-4-8": {
                        "family": "claude",
                        "routes": {"anthropic": "claude-opus-4-8"},
                    },
                },
                "stacks": {
                    "balanced": {
                        "controller": [
                            {
                                "id": "oc-c-c64159d152c2cf90",
                                "model": "gpt-5.6-sol",
                                "providers": ["openai"],
                            }
                        ],
                        "agents": {
                            "repository-explorer": [
                                {
                                    "id": "oc-c-1db0df6c362e02e9",
                                    "model": "gpt-5.6-terra",
                                    "providers": ["openai"],
                                }
                            ],
                            "repository-verifier": [
                                {
                                    "id": "oc-c-38e35c710e57f9ea",
                                    "model": "gpt-5.6-terra",
                                    "providers": ["openai"],
                                }
                            ],
                            "correctness-critic": [
                                {
                                    "id": "oc-c-a69e16d6ee83ad12",
                                    "model": "claude-sonnet-5",
                                    "providers": ["anthropic"],
                                }
                            ],
                            "architecture-advisor": [
                                {
                                    "id": "oc-c-15e855e1e22ff2c1",
                                    "model": "claude-opus-4-8",
                                    "providers": ["anthropic"],
                                }
                            ],
                            "implementation-worker": [
                                {
                                    "id": "oc-c-a24e82f9843f457c",
                                    "model": "gpt-5.6-sol",
                                    "providers": ["openai"],
                                }
                            ],
                        },
                    }
                },
            }
        )
        self.snapshot = StackSnapshot(
            self.stacks,
            StackBindings({}),
            "0" * 64,
            None,
        )
        self.catalog = LiveCatalog(
            choices=(
                LiveModelChoice(
                    "claude",
                    "antigravity",
                    "claude-opus-4-6-thinking",
                    ("oc-a-cccccccccccccccc",),
                    ("Personal Antigravity",),
                ),
                LiveModelChoice(
                    "claude",
                    "anthropic",
                    "claude-opus-4-8",
                    ("oc-a-bbbbbbbbbbbbbbbb",),
                    ("Work Claude",),
                ),
                LiveModelChoice(
                    "claude",
                    "anthropic",
                    "claude-sonnet-5",
                    ("oc-a-bbbbbbbbbbbbbbbb",),
                    ("Work Claude",),
                ),
                LiveModelChoice(
                    "gpt",
                    "openai",
                    "gpt-5.6-sol",
                    ("oc-a-aaaaaaaaaaaaaaaa",),
                    ("Work OpenAI",),
                ),
                LiveModelChoice(
                    "gpt",
                    "openai",
                    "gpt-5.6-terra",
                    ("oc-a-aaaaaaaaaaaaaaaa",),
                    ("Work OpenAI",),
                ),
            ),
            unclassified=(),
        )

    def test_clone_select_review_save_and_assign(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
            ],
            confirmations=[True, True],
            text=["heavy"],
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        self.assertEqual(result.stack_name, "heavy")
        self.assertTrue(result.save)
        self.assertTrue(result.assign_current_project)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-6-thinking")
        self.assertEqual(candidate.providers, ("antigravity",))
        self.assertEqual(
            result.bindings.candidate_accounts[candidate.id],
            "oc-a-cccccccccccccccc",
        )
        review = "\n".join(io_adapter.shown)
        self.assertIn("Step 4/5", review)
        self.assertIn("Personal Antigravity", review)
        self.assertNotIn("oc-a-", review)
        self.assertNotIn("oc-c-", review)
        self.assertNotIn("oc-r-", review)
        self.assertNotIn(".json", review)
        self.assertTrue(
            all(title.startswith("Step ") for title in io_adapter.titles)
        )

    def test_cancel_and_terminal_loss_return_no_mutation(self) -> None:
        for io_adapter in (
            ScriptedIO(cancel_at=2),
            ScriptedIO(disconnect_at=3),
        ):
            with self.subTest(adapter=type(io_adapter).__name__):
                result = StackWizard(
                    self.snapshot,
                    self.catalog,
                    self.accounts,
                    io_adapter,
                ).run(Path("/work/project"))
                self.assertFalse(result.save)
                self.assertEqual(result.stacks, self.snapshot.stacks)
                self.assertEqual(result.bindings, self.snapshot.bindings)

    def test_controller_picker_remains_in_step_two(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Create new",
                "openai",
                "gpt",
                "gpt-5.6-sol",
                "Automatic within provider",
            ],
            confirmations=[False],
            text=["fast"],
        )

        StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        controller_titles = [
            title
            for title in io_adapter.titles
            if "controller" in title.lower()
        ]
        self.assertTrue(controller_titles)
        self.assertTrue(
            all(
                title.startswith("Step 2/5")
                for title in controller_titles
            )
        )

    def test_back_keeps_inherited_draft_and_returns_to_stack_stage(self) -> None:
        io_adapter = ScriptedIO(
            choices=["Clone existing", "balanced"],
            text=["heavy"],
            back_at=4,
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        self.assertFalse(result.save)
        self.assertEqual(result.stacks, self.snapshot.stacks)

    def test_final_catalog_refresh_repicks_only_disappeared_candidate(self) -> None:
        refreshed = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.upstream != "claude-opus-4-6-thinking"
            ),
            unclassified=(),
        )
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "anthropic",
                "claude",
                "claude-opus-4-8",
                "Automatic within provider",
            ],
            confirmations=[True, True, False],
            text=["heavy"],
        )
        catalogues = iter((refreshed, refreshed))

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
            refresh_catalog=lambda: next(catalogues),
        ).run(Path("/work/project"))

        self.assertTrue(result.save)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertNotIn(candidate.id, result.bindings.candidate_accounts)
        self.assertIn(
            "availability changed for architecture-advisor candidate "
            "claude-opus-4-6-thinking",
            "\n".join(io_adapter.shown).lower(),
        )

    def test_safe_delete_rejects_default_and_referenced_stack(self) -> None:
        for projects, expected in (
            ({"contexts": []}, "cannot delete the default stack"),
            (
                {
                    "contexts": [
                        {
                            "root": "/work",
                            "modelStack": "heavy",
                        }
                    ]
                },
                "stack is referenced by /work",
            ),
        ):
            stacks = self.snapshot.stacks
            if expected.startswith("stack is referenced"):
                document = {
                    "schemaVersion": 2,
                    "defaultStack": stacks.default_stack,
                    "models": {
                        name: {
                            "family": model.family,
                            "routes": dict(model.routes),
                        }
                        for name, model in stacks.models.items()
                    },
                    "stacks": {
                        "balanced": {
                            "controller": [
                                {
                                    "id": candidate.id,
                                    "model": candidate.model,
                                    "providers": list(candidate.providers),
                                }
                                for candidate in stacks.stacks[
                                    "balanced"
                                ].controller
                            ],
                            "agents": {
                                role: [
                                    {
                                        "id": candidate.id,
                                        "model": candidate.model,
                                        "providers": list(
                                            candidate.providers
                                        ),
                                    }
                                    for candidate in stacks.stacks[
                                        "balanced"
                                    ].agents[role]
                                ]
                                for role in stacks.stacks[
                                    "balanced"
                                ].agents
                            },
                        },
                        "heavy": {
                            "controller": [
                                {
                                    "id": "oc-c-1111111111111111",
                                    "model": "gpt-5.6-sol",
                                    "providers": ["openai"],
                                }
                            ],
                            "agents": {
                                role: [
                                    {
                                        "id": f"oc-c-{index:016x}",
                                        "model": candidates[0].model,
                                        "providers": list(
                                            candidates[0].providers
                                        ),
                                    }
                                ]
                                for index, (role, candidates) in enumerate(
                                    stacks.stacks[
                                        "balanced"
                                    ].agents.items(),
                                    2,
                                )
                            },
                        },
                    },
                }
                stacks = normalize_model_stacks(document)
            snapshot = StackSnapshot(stacks, StackBindings({}), "0" * 64, None)
            target = "balanced" if projects["contexts"] == [] else "heavy"
            io_adapter = ScriptedIO(
                choices=["Delete existing", target],
                confirmations=[True],
            )
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(RuntimeError, expected):
                    StackWizard(
                        snapshot,
                        self.catalog,
                        self.accounts,
                        io_adapter,
                        projects=projects,
                    ).run(Path("/work/project"))

    def test_cli_runner_cancel_has_no_persistence(self) -> None:
        paths = {"config": Path("/private/config"), "data": Path("/data")}
        config = ResolvedConfig(
            documents={
                "model-stacks": {},
                "projects": {"schemaVersion": 1, "contexts": []},
                "providers": {},
            },
            sources={},
        )
        result = WizardResult(
            self.snapshot.stacks,
            self.snapshot.bindings,
            "",
            False,
            False,
        )
        terminal = _TTYStringIO()
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard,
                "load_accounts",
                return_value=self.accounts,
            ),
            mock.patch.object(
                stack_wizard, "validate_account_bindings"
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=self.catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                return_value=result,
            ),
            mock.patch.object(stack_wizard, "save_stack") as save,
            mock.patch.object(
                stack_wizard, "load_control_plane"
            ) as reload_config,
            mock.patch.object(
                stack_wizard, "assign_stack_to_context"
            ) as assign,
        ):
            status = run_stack_wizard(
                paths, config, Path("/work/project")
            )

        self.assertEqual(status, 0)
        save.assert_not_called()
        reload_config.assert_not_called()
        assign.assert_not_called()
        self.assertIn("No changes saved", terminal.getvalue())

    def test_cli_runner_saves_reloads_validates_then_assigns(self) -> None:
        paths = {"config": Path("/private/config"), "data": Path("/data")}
        projects = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": "/work/project",
                    "dockerProfile": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                    "memoryPalace": "/work/memory",
                    "memoryWing": "project",
                }
            ],
        }
        config = ResolvedConfig(
            documents={
                "model-stacks": stack_wizard.serialize_model_stacks(
                    self.snapshot.stacks
                ),
                "projects": projects,
                "providers": {"providers": {}},
            },
            sources={},
        )
        result = WizardResult(
            self.snapshot.stacks,
            self.snapshot.bindings,
            "balanced",
            True,
            True,
        )
        terminal = _TTYStringIO()
        calls: list[str] = []
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard,
                "load_accounts",
                return_value=self.accounts,
            ),
            mock.patch.object(
                stack_wizard, "validate_account_bindings"
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=self.catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                return_value=result,
            ),
            mock.patch.object(
                stack_wizard,
                "save_stack",
                side_effect=lambda *_args: calls.append("save"),
            ) as save,
            mock.patch.object(
                stack_wizard,
                "load_control_plane",
                side_effect=lambda *_args: (
                    calls.append("reload") or config
                ),
            ) as reload_config,
            mock.patch.object(
                stack_wizard,
                "resolve_control_plane_context",
                return_value={
                    "route": {
                        "contextRootReal": "/work/project",
                    }
                },
            ),
            mock.patch.object(
                stack_wizard,
                "validate_stack_assignment",
                side_effect=lambda *_args: calls.append("validate"),
            ) as validate,
            mock.patch.object(
                stack_wizard,
                "assign_stack_to_context",
                side_effect=lambda *_args: (
                    calls.append("assign") or Path("/work/project")
                ),
            ) as assign,
        ):
            status = run_stack_wizard(
                paths, config, Path("/work/project")
            )

        self.assertEqual(status, 0)
        self.assertEqual(calls, ["save", "reload", "validate", "assign"])
        save.assert_called_once_with(
            self.snapshot, result.stacks, result.bindings
        )
        reload_config.assert_called_once()
        validate.assert_called_once()
        assign.assert_called_once()
        output = terminal.getvalue()
        self.assertIn("Saved stack balanced", output)
        self.assertIn("/work/project", output)
        self.assertNotIn("oc-a-", output)
        self.assertNotIn("oc-c-", output)
        self.assertNotIn("oc-r-", output)
        self.assertNotIn(".json", output)


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TerminalWizardIOTests(unittest.TestCase):
    def _pty_choose(
        self,
        payload: bytes,
        *,
        options: list[Choice] | None = None,
        environment: dict[str, str] | None = None,
        width: int = 80,
    ) -> tuple[int, str]:
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, width, 0, 0),
        )
        reader = os.fdopen(os.dup(slave), "r", encoding="utf-8", newline="")
        writer = os.fdopen(
            os.dup(slave),
            "w",
            encoding="utf-8",
            newline="",
            buffering=1,
        )
        os.close(slave)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        adapter = TerminalWizardIO(
            stdin=reader,
            stdout=writer,
            environment=environment or {"TERM": "xterm-256color"},
        )
        primed = bytearray()
        failure: list[BaseException] = []

        def feed_after_render() -> None:
            try:
                readable, _, _ = __import__("select").select(
                    [master], [], [], 2
                )
                if not readable:
                    raise AssertionError("terminal adapter did not render")
                primed.extend(os.read(master, 65536))
                os.write(master, payload)
            except BaseException as error:
                failure.append(error)

        feeder = threading.Thread(target=feed_after_render)
        feeder.start()
        selected = adapter.choose(
            "Step 2/5 · Model",
            options
            or [
                Choice("first"),
                Choice("second", marker="inherited"),
                Choice("needle model"),
            ],
            searchable=True,
        )
        feeder.join()
        if failure:
            raise failure[0]
        output = bytearray(primed)
        while True:
            readable, _, _ = __import__("select").select([master], [], [], 0)
            if not readable:
                break
            chunk = os.read(master, 65536)
            if not chunk:
                break
            output.extend(chunk)
        return selected, output.decode("utf-8", errors="replace")

    def test_raw_adapter_supports_arrows_numbers_search_and_back(self) -> None:
        selected, _ = self._pty_choose(b"\x1b[B\r")
        self.assertEqual(selected, 1)
        selected, _ = self._pty_choose(b"3")
        self.assertEqual(selected, 2)
        selected, _ = self._pty_choose(b"/needle\r")
        self.assertEqual(selected, 2)
        selected, _ = self._pty_choose(b"\x1b")
        self.assertEqual(selected, BACK)

    def test_raw_adapter_ctrl_c_cancels_without_waiting(self) -> None:
        with self.assertRaises(WizardCancelled):
            self._pty_choose(b"\x03")

    def test_no_color_narrow_render_has_progress_and_full_review_model(
        self,
    ) -> None:
        selected, output = self._pty_choose(
            b"\r",
            options=[
                Choice(
                    "very-long-provider-label",
                    detail="claude-opus-4-6-thinking",
                    marker="current",
                )
            ],
            environment={"TERM": "xterm-256color", "NO_COLOR": "1"},
            width=28,
        )
        self.assertEqual(selected, 0)
        self.assertIn("Step 2/5", output)
        self.assertIn("current", output)
        self.assertNotIn("claude-opus-4-6-thinking", output)
        self.assertNotRegex(output, r"\x1b\[[0-9;]*m")

        visible = io.StringIO()
        adapter = TerminalWizardIO(
            stdin=io.StringIO(),
            stdout=visible,
            environment={"TERM": "dumb", "NO_COLOR": "1"},
        )
        adapter.show(
            "Step 4/5 · Review\n"
            "architecture-advisor: claude-opus-4-6-thinking"
        )
        self.assertIn("claude-opus-4-6-thinking", visible.getvalue())

    def test_numbered_line_fallback_supports_default_and_search(self) -> None:
        visible = io.StringIO()
        adapter = TerminalWizardIO(
            stdin=io.StringIO("/needle\n1\n"),
            stdout=visible,
            environment={"TERM": "dumb"},
        )
        selected = adapter.choose(
            "Step 3/5 · Agents",
            [Choice("first"), Choice("Needle model")],
            selected=0,
            searchable=True,
        )
        self.assertEqual(selected, 1)
        self.assertIn("1)", visible.getvalue())


if __name__ == "__main__":
    unittest.main()
