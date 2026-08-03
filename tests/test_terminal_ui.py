#!/usr/bin/env python3
from __future__ import annotations

import io
import unittest

from integrations.common.terminal_ui import (
    Choice,
    TerminalUI,
    UiCancelled,
)


class TerminalUiTests(unittest.TestCase):
    def test_narrow_section_stacks_values_without_overflow(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=32,
            environment={"NO_COLOR": "1"},
        )

        ui.section(
            "Models",
            (("Architecture advisor", "claude-opus-5"),),
        )

        self.assertEqual(
            output.getvalue(),
            "Models\n  Architecture advisor\n    claude-opus-5\n",
        )

    def test_wide_section_aligns_values(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=80,
            environment={"NO_COLOR": "1"},
        )

        ui.section(
            "Accounts",
            (("OpenAI primary", "Personal"), ("OpenAI backup", "Backup")),
        )

        self.assertEqual(
            output.getvalue(),
            "Accounts\n  OpenAI primary  Personal\n  OpenAI backup   Backup\n",
        )

    def test_search_selects_from_filtered_numbered_choices(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdin=io.StringIO("/terra\n1\n"),
            stdout=output,
            environment={"NO_COLOR": "1"},
            width=80,
        )

        selected = ui.choose(
            "Choose a model",
            (Choice("gpt-5.6-sol"), Choice("gpt-5.6-terra")),
            searchable=True,
        )

        self.assertEqual(selected, 1)
        self.assertIn("1. gpt-5.6-terra", output.getvalue())

    def test_no_color_suppresses_ansi(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=80,
            environment={"NO_COLOR": "1"},
        )

        ui.show("Configured")

        self.assertEqual(output.getvalue(), "Configured\n")
        self.assertNotIn("\x1b[", output.getvalue())

    def test_end_of_input_cancels_on_a_clean_line(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdin=io.StringIO(""),
            stdout=output,
            environment={"NO_COLOR": "1"},
            width=80,
        )

        with self.assertRaises(UiCancelled):
            ui.confirm("Apply changes?", default=False)

        self.assertTrue(output.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()
