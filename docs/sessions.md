# Sessions

Orichum distinguishes a logical session from the physical Claude Code process.
The logical session records the project, stack, model family, account route,
and Claude session identity needed for a consistent resume.

## Start and inspect

```bash
cd ~/work/project
orichum
orichum --permission-mode acceptEdits

orichum sessions
orichum session routes SESSION_ID
orichum sessions routes SESSION_ID
```

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
copies, and Claudex translation ports. The three upstream resident services are
shared.
