#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_DATA_ROOT="${CLAUDEX_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/claudex-workflow}"
provider="${1:-gpt}"
smoke_output="$(mktemp /tmp/claudex-smoke.XXXXXX)"
trap 'rm -f "$smoke_output"' EXIT

if [[ "$provider" == "controller" ]]; then
  controller_prompt="$(cat <<EOF
Perform a read-only cross-check across all eight controller surfaces listed below. The task requires two independent review angles. Return a concise synthesis with file evidence and do not modify any file.
1. $WORKFLOW_ROOT/controller/controller-policy.md
2. $WORKFLOW_ROOT/controller/settings.json
3. $WORKFLOW_ROOT/controller/plugin/skills/heavy-orchestration/SKILL.md
4. $WORKFLOW_ROOT/controller/plugin/workflows/investigate.js
5. $WORKFLOW_ROOT/controller/plugin/workflows/review.js
6. $WORKFLOW_ROOT/controller/plugin/hooks/hooks.json
7. $WORKFLOW_ROOT/bin/claudex-gpt
8. $WORKFLOW_ROOT/README.md
EOF
)"

  if ! "$WORKFLOW_ROOT/bin/claudex-gpt" \
    -p "$controller_prompt" \
    --output-format stream-json \
    --verbose \
    --max-turns 6 \
    --max-budget-usd 2 \
    --allowedTools Workflow >"$smoke_output" 2>&1; then
    sed -n '1,80p' "$smoke_output" >&2
    exit 1
  fi

  if ! jq -R -s -e \
    --arg expanded_review "$WORKFLOW_ROOT/controller/plugin/workflows/review.js" \
    --arg literal_review '${CLAUDE_PLUGIN_ROOT}/workflows/review.js' '
    def nonempty_string:
      type == "string" and ((gsub("[[:space:]]"; "") | length) > 0);
    [
      split("\n")[] |
      fromjson? |
      select(.type == "assistant") |
      .message.content[]? |
      select(.type == "tool_use" and .name == "Workflow")
    ] as $workflow_calls |
    ($workflow_calls | length) == 1 and
    ($workflow_calls[0] |
      (.input | type == "object") and
      (.input.scriptPath == $expanded_review or .input.scriptPath == $literal_review) and
      (.input.args | type == "object") and
      (.input.args.subject | nonempty_string) and
      (.input.args.scope | nonempty_string) and
      .input.args.highRisk == false)
  ' "$smoke_output" >/dev/null 2>&1; then
    sed -n '1,80p' "$smoke_output" >&2
    exit 1
  fi

  if ! jq -R -e '
    def positive_output($model):
      .modelUsage[$model] as $usage |
      ($usage | type == "object") and
      ($usage.outputTokens | type == "number" and . > 0);
    fromjson? |
    select(
      .type == "result" and
      .subtype == "success" and
      .is_error == false and
      (.modelUsage | type == "object") and
      positive_output("gpt-5.6-sol") and
      positive_output("gpt-5.6-terra") and
      positive_output("claude-sonnet-5")
    )
  ' "$smoke_output" >/dev/null 2>&1; then
    sed -n '1,80p' "$smoke_output" >&2
    exit 1
  fi

  controller_session_id="$(jq -R -r '
    fromjson? |
    select(.type == "result") |
    .session_id // empty
  ' "$smoke_output" | tail -1)"
  case "$controller_session_id" in
    ''|*[!a-zA-Z0-9-]*)
      sed -n '1,80p' "$smoke_output" >&2
      exit 1
      ;;
  esac
  workflow_records=()
  while IFS= read -r record; do
    workflow_records+=("$record")
  done < <(find "$WORKFLOW_DATA_ROOT/claude-config/projects" \
    -type f -path "*/$controller_session_id/workflows/*.json" \
    -print 2>/dev/null)
  if [[ "${#workflow_records[@]}" -ne 1 ]]; then
    sed -n '1,80p' "$smoke_output" >&2
    printf 'Expected exactly one workflow record for session %s; found %d.\n' \
      "$controller_session_id" "${#workflow_records[@]}" >&2
    exit 1
  fi
  workflow_record="${workflow_records[0]}"
  if ! jq -e '
    .status == "completed" and
    .result.status == "complete" and
    (.result.missingAgents | type == "array" and length == 0)
  ' "$workflow_record" >/dev/null 2>&1; then
    sed -n '1,80p' "$smoke_output" >&2
    jq -c '{status, resultStatus: .result.status, missingAgents: .result.missingAgents}' \
      "$workflow_record" >&2
    exit 1
  fi

  printf 'PASS: automatic controller selected the audited Workflow\n'
  exit 0
fi

case "$provider" in
  gpt)
    expected_reply='CLAUDEX_GPT_OK'
    expected_model='gpt-5.6-sol'
    model_args=()
    ;;
  claude)
    expected_reply='CLAUDEX_CLAUDE_OK'
    expected_model='claude-opus-4-8'
    model_args=(--model opus)
    ;;
  *)
    printf 'Usage: %s [gpt|claude|controller]\n' "$0" >&2
    exit 2
    ;;
esac

if ! "$WORKFLOW_ROOT/bin/claudex-gpt" \
  "${model_args[@]}" -p "Reply with exactly $expected_reply" \
  --output-format json --max-turns 1 >"$smoke_output" 2>&1; then
  sed -n '1,80p' "$smoke_output" >&2
  exit 1
fi

result_json="$(rg '^\{"type":"result"' "$smoke_output" | tail -1)"
if jq -e --arg reply "$expected_reply" --arg model "$expected_model" \
  '.subtype == "success" and
   .is_error == false and
   .result == $reply and
   (.modelUsage[$model].outputTokens | type == "number" and . > 0)' \
  <<<"$result_json" >/dev/null; then
  printf 'PASS: Claude Code completed through Claudex on %s\n' "$expected_model"
else
  printf 'Unexpected Claude Code result.\n' >&2
  exit 1
fi
