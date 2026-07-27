# Status line

Orichum configures a two-line status display inside the Claude Code sessions it
launches:

```text
ORICHUM │ my-app │ balanced
Claude · Opus 4.8 │ Work Claude [primary] │ context 41% │ 5h 63% │ 7d —
```

The first line identifies the project and selected model stack. The second
line shows the active model family and model, named provider account, route
state, conversation context consumption, and any quota windows available for
that account.

## Dynamic values

- Model and context usage come from Claude Code's status-line input.
- Quota usage prefers Claude Code's native values. Because Claude Code omits
  them when Orichum's external auth source is active, Orichum falls back to the
  selected OpenAI or Anthropic account's usage endpoint.
- Provider quota lookups are cached locally for 60 seconds and scoped by
  Orichum account ID. Concurrent sessions using different accounts cannot
  replace one another's values.
- If a refresh fails because of a temporary credential, provider, or network
  problem, the last successful value may remain visible for up to 15 minutes.
  After that, an unavailable window renders as `—`. Orichum never invents a
  percentage when no usable provider value exists.
- Account and route state come from the shared Orichum route proxy.
- `[primary]` means the preferred account handled the latest request.
- `[fallback: rate limit]` means same-family recovery selected the configured
  fallback account.
- `[fallback: cooldown]` means Orichum skipped a primary account that is still
  cooling down.
- `—` means the active provider did not publish that window. It is not treated
  as zero. For example, an account that currently publishes only a weekly
  window correctly shows a 7-day value and `5h —`.

The route snapshot is scoped to the current Orichum session. Concurrent
sessions cannot replace one another's displayed account.

## Scope and privacy

The status command runs locally and does not consume model tokens. It reads
public session metadata, named-account labels, loopback route state, Claude
Code's standard-input metrics, and the active account's private OAuth
credential when a provider quota refresh is due. The credential is opened
through Orichum's existing ownership and permission checks, used only for the
provider request, and never written to the quota cache or rendered. Cached
files contain only percentages, account ID, and fetch time and use mode
`0600`.

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
