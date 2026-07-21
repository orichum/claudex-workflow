#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="$ROOT/bin/claudex-gpt"

bash -n "$launcher"
jq empty \
  "$ROOT/controller/settings.json" \
  "$ROOT/controller/project-context.json" \
  "$ROOT/controller/plugin/hooks/hooks.json" \
  "$ROOT/controller/plugin/.claude-plugin/plugin.json"

rg -q -- '--effort high' "$launcher"
rg -q -- '--strict-mcp-config' "$launcher"
rg -q -- '--append-system-prompt-file' "$launcher"
rg -q -- '--plugin-dir' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=3$' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=24$' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_RETRIES=2$' "$launcher"
rg -q '^export ENABLE_TOOL_SEARCH=true$' "$launcher"
! rg -qi 'ultracode' "$launcher"

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-workflow-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"

render_claudex_config "$fixture/claudex.toml" \
  gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol \
  claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8 \
  /portable/bin/claude
rg -q '^claude_binary = "/portable/bin/claude"$' "$fixture/claudex.toml"
rg -q '^sonnet = "claude-sonnet-5"$' "$fixture/claudex.toml"
rg -q '^opus = "claude-opus-4-8"$' "$fixture/claudex.toml"
rg -q '^balanced = "gpt-5.6-terra"$' "$fixture/claudex.toml"

jq -n '{data:[
  {id:"gpt-5.6-luna"},{id:"gpt-5.6-terra"},{id:"gpt-5.6-sol"},
  {id:"claude-haiku-4-5-20251001"},{id:"claude-sonnet-5"},
  {id:"claude-opus-4-8"}
]}' >"$fixture/models.json"
render_discovered_claudex_config "$fixture/models.json" "$fixture/discovered.toml"
rg -q '^default_model = "gpt-5.6-sol"$' "$fixture/discovered.toml"
rg -q '^opus = "claude-opus-4-8"$' "$fixture/discovered.toml"

rg -q 'Graphify is present' "$ROOT/controller/controller-policy.md"
rg -q 'Use MemPalace automatically' "$ROOT/controller/controller-policy.md"
rg -q 'Docker MCP profile selected' "$ROOT/controller/controller-policy.md"
test ! -d "$ROOT/integrations/docker"

printf 'PASS: controller and mixed-model workflow\n'
