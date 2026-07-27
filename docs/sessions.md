# Sessions

Orichum distinguishes a logical session from the physical Claude Code process.
The logical session records the project, stack, model family, account route,
and Claude session identity needed for a consistent resume.

## Start and inspect

```bash
cd ~/work/project
orichum
orichum run -- -p "Summarize this repository"

orichum sessions
orichum sessions --limit 50
orichum sessions --all
orichum session routes SESSION_ID
orichum sessions routes SESSION_ID
```

The session list shows the newest 20 logical sessions by default. Use `--limit`
for a different bound or `--all` when the complete history is needed.

Use `--` after `orichum run` when forwarding Claude Code arguments. Orichum
rejects runtime options it owns, including model, session, workspace, MCP,
plugin, effort, tool-approval, and permission-mode settings.

Every launch re-resolves and validates the project context and live services.
A newly created logical session then freezes its selected primary route and at
most one compatible fallback.

## Resume

```bash
orichum resume SESSION_ID
```

Resume loads the stored Orichum context again, verifies its integrity, and
preserves the original model/account binding and Claude session identity. It
does not silently move to another family after configuration changes.

## Fork

Use a fork to change stack or model family while carrying only an explicit,
bounded handoff:

```bash
orichum models stacks
orichum fork SESSION_ID \
  --stack TARGET_STACK \
  --handoff-file ./bounded-handoff.md
```

The parent remains resumable. The child does not receive hidden provider state
or the full parent transcript.

Concurrent sessions use separate physical run directories, MCP files, plugin
copies, and Claudex translation ports. CLIProxyAPI and the Orichum route proxy
are shared, while each physical session owns its Claudex translator.

## Clean old physical runs

Logical sessions remain resumable, but each launch also creates a disposable
physical snapshot. Preview inactive snapshots older than seven days:

```bash
orichum sessions cleanup
```

Remove only the runs shown by that preview:

```bash
orichum sessions cleanup --yes
```

Use `--older-than DAYS` to change the minimum age. Cleanup never removes
logical session records and skips a run while its Claudex translator port is
live.
