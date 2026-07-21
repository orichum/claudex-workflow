#!/usr/bin/env bash
set -euo pipefail

input="$(cat)"

deny() {
  jq -cn --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
}

if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$input"; then
  deny "Malformed orchestration hook input is denied"
  exit 0
fi

tool_name="$(jq -r '.tool_name // empty' <<<"$input")"

case "$tool_name" in
  Agent)
    agent_type="$(jq -r '.tool_input.subagent_type // empty' <<<"$input")"
    case "$agent_type" in
      claudex-controller:terra-explorer|\
      claudex-controller:terra-verifier|\
      claudex-controller:sonnet-critic|\
      claudex-controller:sonnet-synthesizer|\
      claudex-controller:opus-architect|\
      claudex-controller:sol-builder)
        exit 0
        ;;
      *)
        deny "Agent type is not in the Claudex controller allowlist: $agent_type"
        ;;
    esac
    ;;
  Workflow)
    script_path="$(jq -r '.tool_input.scriptPath // empty' <<<"$input")"
    case "$script_path" in
      "$CLAUDE_PLUGIN_ROOT/workflows/investigate.js"|\
      "$CLAUDE_PLUGIN_ROOT/workflows/review.js"|\
      '${CLAUDE_PLUGIN_ROOT}/workflows/investigate.js'|\
      '${CLAUDE_PLUGIN_ROOT}/workflows/review.js')
        exit 0
        ;;
      *)
        deny "Workflow must use an audited Claudex scriptPath; inline, named, external, and arbitrary workflows are denied"
        ;;
    esac
    ;;
  *)
    deny "Unexpected tool passed to the Claudex orchestration guard: $tool_name"
    ;;
esac
