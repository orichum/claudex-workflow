#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from integrations.common import orichum_cli
from integrations.common import orichum_completion


class OrichumCompletionTests(unittest.TestCase):
    def test_spec_preserves_nested_commands_choices_and_remainder(self) -> None:
        spec = orichum_completion.completion_spec(
            orichum_cli.build_parser()
        )

        stack = spec["commands"]["stack"]["commands"]["show"]
        self.assertEqual(stack["positionals"][0]["completion"], "stack")
        dashboard = spec["commands"]["leanctx"]["commands"]["dashboard"]
        self.assertEqual(
            dashboard["options"]["--open"]["choices"],
            ["browser", "none", "vscode"],
        )
        self.assertTrue(spec["commands"]["run"]["remainder"])
        configure = spec["commands"]["configure"]
        self.assertEqual(
            configure["options"]["--project"]["completion"],
            "context",
        )
        context_add = spec["commands"]["context"]["commands"]["add"]
        self.assertEqual(
            context_add["positionals"][0]["completion"],
            "directory",
        )
        self.assertEqual(
            context_add["options"]["--model-stack"]["completion"],
            "stack",
        )
        account_add = (
            spec["commands"]["provider"]["commands"]["account"]
            ["commands"]["add"]
        )
        self.assertEqual(
            [entry["completion"] for entry in account_add["positionals"]],
            [None, "provider", "file", "pool"],
        )
        self.assertEqual(
            spec["commands"]["status"]["positionals"][0]["completion"],
            "logical-session",
        )
        self.assertEqual(
            spec["commands"]["fork"]["options"]["--handoff-file"]
            ["completion"],
            "file",
        )
        provider_login = (
            spec["commands"]["provider"]["commands"]["login"]
        )
        self.assertEqual(
            [entry["completion"] for entry in provider_login["positionals"]],
            ["auth-type", None],
        )
        self.assertTrue(provider_login["remainder"])
        plugin_add = spec["commands"]["plugin"]["commands"]["add"]
        self.assertEqual(
            plugin_add["positionals"][0]["completion"],
            "plugin-add",
        )

    def test_renderers_emit_native_static_and_dynamic_completion(self) -> None:
        parser = orichum_cli.build_parser()
        rendered = {
            shell: orichum_completion.render_completion(parser, shell)
            for shell in ("zsh", "bash", "fish")
        }

        self.assertTrue(rendered["zsh"].startswith("#compdef orichum\n"))
        self.assertIn("complete -F _orichum_complete orichum", rendered["bash"])
        self.assertIn("complete -c orichum", rendered["fish"])
        for definition in rendered.values():
            self.assertIn("stack", definition)
            self.assertIn("configure", definition)
            self.assertIn("--leanctx-profile", definition)
            self.assertIn("lean", definition)
            self.assertIn("full", definition)
            self.assertIn("orichum __complete", definition)
            self.assertNotIn(str(Path.home()), definition)
        self.assertIn('option_prefix="${current%%=*}="', rendered["bash"])
        self.assertIn(
            '"${current#*=}" "$option_prefix"',
            rendered["zsh"],
        )
        self.assertIn("set option_prefix (string replace", rendered["fish"])
        self.assertIn(
            "string replace -r '^[^=]*=' ''",
            rendered["fish"],
        )
        self.assertNotIn(
            "string replace -r '^.*=' ''",
            rendered["fish"],
        )

    def test_zsh_entries_escape_value_and_description_separators(self) -> None:
        self.assertEqual(
            orichum_completion._zsh_entry("team:blue", "account: name"),
            "'team\\:blue:account\\: name'",
        )

    def test_completion_command_renders_without_loading_configuration(
        self,
    ) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = orichum_cli.main(["completion", "bash"])

        self.assertEqual(status, 0)
        self.assertIn(
            "complete -F _orichum_complete orichum",
            stdout.getvalue(),
        )

    def test_rendered_definitions_have_valid_shell_syntax(self) -> None:
        parser = orichum_cli.build_parser()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for shell in ("bash", "zsh", "fish"):
                executable = shutil.which(shell)
                if executable is None:
                    continue
                definition = root / f"orichum.{shell}"
                definition.write_text(
                    orichum_completion.render_completion(parser, shell),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [executable, "-n", str(definition)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"{shell}: {completed.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
