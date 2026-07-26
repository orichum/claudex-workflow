# Status line

Orichum configures a two-line status display inside the Claude Code sessions it
launches:

```text
ORICHUM │ my-app │ balanced
Claude · Opus 4.8 │ Work Claude [primary] │ context 41% │ 5h 63% │ 7d —
```

The first line identifies the project and selected model stack. The second
line shows the active model family and model, named provider account, route
state, conversation context consumption, and any quota windows Claude Code
provides.

## Dynamic values

- Model and context usage come from Claude Code's status-line input.
- Account and route state come from the shared Orichum route proxy.
- `[primary]` means the preferred account handled the latest request.
- `[fallback: rate limit]` means same-family recovery selected the configured
  fallback account.
- `[fallback: cooldown]` means Orichum skipped a primary account that is still
  cooling down.
- `—` means the provider did not publish that metric. It is not treated as
  zero.

The route snapshot is scoped to the current Orichum session. Concurrent
sessions cannot replace one another's displayed account.

## Scope and privacy

The status command runs locally and does not consume model tokens. It reads
only public session metadata, named-account labels, loopback route state, and
the metrics Claude Code supplies on standard input. Credential references,
tokens, routing prefixes, and authorization headers are never rendered.

Orichum installs the setting only in its isolated Claude configuration.
Launching `claude` normally does not enable or modify this status line.

If local state is temporarily unavailable, the display degrades to:

```text
ORICHUM │ status unavailable
```

Run `orichum doctor` if that fallback persists.

## Troubleshooting

If the status line does not appear:

1. Start a new session through `orichum`, not directly through `claude`.
2. Confirm `orichum doctor` passes.
3. Check that the installed Claude settings contain `statusLine`:

   ```bash
   jq '.statusLine' ~/.local/share/orichum/claude-config/settings.json
   ```

4. Restart the session after accepting Claude Code's workspace-trust prompt.

Claude Code may temporarily hide the status line during menus, autocomplete,
and permission prompts.
