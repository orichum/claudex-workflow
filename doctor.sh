#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
WORKFLOW_DATA_ROOT="$(workflow_data_dir)"
CLAUDEX_CONFIG_FILE="$(model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml)"

failures=0
models_response="$(mktemp /tmp/claudex-doctor-models.XXXXXX)"
doctor_fixture=""
cleanup_doctor_models() {
  rm -f "$models_response"
  if [[ -n "$doctor_fixture" ]]; then
    case "$doctor_fixture" in
      /tmp/claudex-doctor-session.*|/private/tmp/claudex-doctor-session.*)
        rm -rf -- "$doctor_fixture"
        ;;
    esac
  fi
}
trap cleanup_doctor_models EXIT

check_ok() { printf 'OK   %s\n' "$1"; }
check_fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

file_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

file_owner() {
  if stat -f '%u' "$1" >/dev/null 2>&1; then
    stat -f '%u' "$1"
  else
    stat -c '%u' "$1"
  fi
}

if (
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m integrations.common.project_context validate-config \
    --config "$WORKFLOW_ROOT/controller/project-context.json"
) >/dev/null 2>&1; then
  check_ok 'project context configuration is structurally valid'
else
  check_fail 'project context configuration is structurally invalid'
fi

frontend_root="$WORKFLOW_ROOT/controller/plugin/skills/frontend-design"
if [[ "$(sha256_file "$frontend_root/SKILL.md" 2>/dev/null || true)" == \
      "1608ea77fbb6fc30d13a97d12cfa8ebf31358d40f0dd97beed24829d6b3f45dd" ]] && \
   [[ "$(sha256_file "$frontend_root/PROVENANCE.md" 2>/dev/null || true)" == \
      "9dd3b8b2775d3a6b41eb0d4ecdca92ec01d30972b4a6c27b47a71049e152aef5" ]] && \
   [[ "$(sha256_file "$frontend_root/LICENSE.txt" 2>/dev/null || true)" == \
      "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30" ]] && \
   [[ "$(file_mode "$frontend_root/SKILL.md" 2>/dev/null || true)" == "644" ]] && \
   [[ "$(file_mode "$frontend_root/PROVENANCE.md" 2>/dev/null || true)" == "644" ]] && \
   [[ "$(file_mode "$frontend_root/LICENSE.txt" 2>/dev/null || true)" == "644" ]]; then
  check_ok 'Frontend Design skill, provenance, and license hashes match pinned artifacts; modes are 0644'
else
  check_fail 'Frontend Design pinned hashes or file modes differ'
fi

hooks_file="$WORKFLOW_ROOT/controller/plugin/hooks/hooks.json"
health_hook="$WORKFLOW_ROOT/controller/plugin/scripts/check-local-services.sh"
memory_hook="$WORKFLOW_ROOT/controller/plugin/scripts/route-mempalace-input.py"
graphify_hook="$WORKFLOW_ROOT/controller/plugin/scripts/ensure-graphify-hook.py"
if [[ -x "$health_hook" && -x "$memory_hook" && -x "$graphify_hook" ]] && \
   jq -e '.hooks.PreToolUse[] | select(.matcher == "mcp__mempalace__.*")' \
  "$hooks_file" >/dev/null 2>&1; then
  check_ok 'health, MemPalace routing, and Graphify maintenance hooks are present'
else
  check_fail 'automatic integration hook configuration is incomplete'
fi

state_ok=true
for state_directory in \
  "$WORKFLOW_DATA_ROOT" \
  "$WORKFLOW_DATA_ROOT/state" \
  "$WORKFLOW_DATA_ROOT/state/sessions"
do
  if [[ -L "$state_directory" || ! -d "$state_directory" ]] || \
     [[ "$(file_owner "$state_directory" 2>/dev/null || true)" != "$(id -u)" ]] || \
     [[ "$(file_mode "$state_directory" 2>/dev/null || true)" != "700" ]]; then
    state_ok=false
  fi
done
if [[ "$state_ok" == true ]]; then
  check_ok 'foundation session state directories are private'
else
  check_fail 'foundation session state directories are missing or unsafe'
fi

doctor_fixture="$(mktemp -d /tmp/claudex-doctor-session.XXXXXX)"
chmod 0700 "$doctor_fixture"
doctor_fixture="$(cd -P "$doctor_fixture" && pwd)"
fixture_workflow="$doctor_fixture/workflow"
fixture_data="$doctor_fixture/data"
fixture_home="$doctor_fixture/home"
fixture_project="$fixture_home/project"
fixture_palace="$fixture_home/palace"
install -d -m 0755 "$fixture_workflow"
install -d -m 0700 "$fixture_data"
install -d -m 0700 "$fixture_home" "$fixture_project" "$fixture_palace"
install -d -m 0755 "$fixture_workflow/integrations/common"
install -m 0644 \
  "$WORKFLOW_ROOT/integrations/__init__.py" \
  "$fixture_workflow/integrations/__init__.py"
install -m 0644 \
  "$WORKFLOW_ROOT/integrations/common/__init__.py" \
  "$WORKFLOW_ROOT/integrations/common/project_context.py" \
  "$WORKFLOW_ROOT/integrations/common/session_config.py" \
  "$fixture_workflow/integrations/common/"
jq -n \
  --arg palace "$fixture_palace" \
  --arg root "$fixture_project" \
  '{contexts:[{root:$root,dockerProfile:"fixture",memoryPalace:$palace,memoryWing:"fixture"}]}' \
  >"$doctor_fixture/project-context.json"
if fixture_session_json="$({
  cd "$fixture_workflow"
  HOME="$fixture_home" PYTHONDONTWRITEBYTECODE=1 \
    python3 -B -m integrations.common.session_config create \
    --workflow-root "$fixture_workflow" \
    --data-root "$fixture_data" \
    --launch-dir "$fixture_project" \
    --config "$doctor_fixture/project-context.json"
} 2>/dev/null)" && \
   fixture_mcp="$(jq -er '.mcpFile' <<<"$fixture_session_json" 2>/dev/null)" && \
   [[ "$fixture_mcp" == "$fixture_data/state/sessions/"run.*/mcp.json ]] && \
   jq -e '
     (.mcpServers | type == "object") and
     ([.mcpServers | keys[]] - ["docker", "mempalace", "graphify"] | length == 0)
   ' "$fixture_mcp" >/dev/null; then
  check_ok 'disposable strict MCP generation exposes only supported integrations'
else
  check_fail 'disposable strict MCP generation failed or exposed an unsupported server'
fi
fixture_to_remove="$doctor_fixture"
rm -rf -- "$fixture_to_remove"
if [[ -e "$fixture_to_remove" ]]; then
  check_fail 'disposable strict MCP fixture was not removed'
else
  doctor_fixture=""
fi

for binary in cli-proxy-api claudex; do
  if [[ -x "$WORKFLOW_DATA_ROOT/bin/$binary" ]]; then
    check_ok "executable: $binary"
  else
    check_fail "executable: $binary"
  fi
done
for binary in claudex-gpt claude-headroom claudex-login; do
  if [[ -x "$WORKFLOW_ROOT/bin/$binary" ]]; then
    check_ok "executable: $binary"
  else
    check_fail "executable: $binary"
  fi
done

if CLAUDE_BIN="$(command -v claude 2>/dev/null)"; then
  claude_version="$(extract_semver "$("$CLAUDE_BIN" --version 2>&1)" || true)"
else
  claude_version=""
fi
if [[ -n "$claude_version" ]]; then
  check_ok "Claude Code is available (version $claude_version)"
else
  check_fail 'Claude Code is unavailable or its version cannot be read'
fi

controller_settings="$WORKFLOW_ROOT/controller/settings.json"
runtime_settings="$WORKFLOW_DATA_ROOT/claude-config/settings.json"
if cmp -s "$controller_settings" "$runtime_settings"; then
  check_ok 'isolated Claude settings match controller settings'
else
  check_fail 'isolated Claude settings are missing or differ from controller settings'
fi
if jq -e '
  .disableWorkflows == false and
  .disableBundledSkills == true and
  .effortLevel == "high" and
  .workflowKeywordTriggerEnabled == false
' "$runtime_settings" >/dev/null 2>&1; then
  check_ok 'isolated Claude settings enable high-effort bounded workflows without bundled skills or Ultracode'
else
  check_fail 'isolated Claude settings do not enforce the controller policy'
fi

plugin_root="$WORKFLOW_ROOT/controller/plugin"
controller_files=(
  "$plugin_root/.claude-plugin/plugin.json"
  "$plugin_root/workflows/investigate.js"
  "$plugin_root/workflows/review.js"
  "$plugin_root/skills/heavy-orchestration/SKILL.md"
  "$plugin_root/hooks/hooks.json"
  "$plugin_root/agents/terra-explorer.md"
  "$plugin_root/agents/terra-verifier.md"
  "$plugin_root/agents/sonnet-critic.md"
  "$plugin_root/agents/opus-architect.md"
  "$plugin_root/agents/sol-builder.md"
)
controller_files_ok=true
for controller_file in "${controller_files[@]}"; do
  [[ -f "$controller_file" ]] || controller_files_ok=false
done
if [[ "$controller_files_ok" == true ]] && \
   [[ -x "$plugin_root/scripts/guard-orchestration.sh" ]]; then
  check_ok 'controller plugin files and executable guard are present'
else
  check_fail 'controller plugin is incomplete or its guard is not executable'
fi

agent_contracts_ok=true
for agent_name in \
  terra-explorer terra-verifier sonnet-critic \
  opus-architect sol-builder
do
  rg -q '^effort: high$' "$plugin_root/agents/$agent_name.md" || agent_contracts_ok=false
done
rg -q '^maxTurns: 9$' "$plugin_root/agents/sonnet-critic.md" || agent_contracts_ok=false
if [[ "$agent_contracts_ok" == true ]]; then
  check_ok 'all controller agents enforce high effort and bounded specialist contracts'
else
  check_fail 'controller agent effort or turn-bound contracts are invalid'
fi

if [[ -n "${CLAUDE_BIN:-}" ]] && \
   CLAUDE_CONFIG_DIR="$WORKFLOW_DATA_ROOT/claude-config" \
     "$CLAUDE_BIN" plugin validate --strict "$plugin_root" >/dev/null 2>&1; then
  check_ok 'controller plugin validates strictly'
else
  check_fail 'controller plugin strict validation failed'
fi

launcher="$WORKFLOW_ROOT/bin/claudex-gpt"
if rg -q -- '--effort high' "$launcher" && \
   rg -q -- '--append-system-prompt-file' "$launcher" && \
   rg -q -- '--plugin-dir' "$launcher" && \
   rg -q '^export CLAUDE_CODE_ALWAYS_ENABLE_EFFORT=1$' "$launcher" && \
   rg -q '^export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false$' "$launcher" && \
   rg -q '^export CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1$' "$launcher" && \
   rg -q '^unset CLAUDE_CODE_EFFORT_LEVEL$' "$launcher" && \
   ! rg -q -- 'ultracode|export[[:space:]]+CLAUDE_CODE_DISABLE_WORKFLOWS' "$launcher"; then
  check_ok 'claudex-gpt owns the high-effort isolated controller launch'
else
  check_fail 'claudex-gpt controller flags are missing or unsafe'
fi

native_launcher="$WORKFLOW_ROOT/bin/claude-headroom"
if rg -q -- '--exclude-dynamic-system-prompt-sections' "$native_launcher" && \
   ! rg -q -- '--exclude-dynamic-system-prompt-sections' "$launcher"; then
  check_ok 'prompt-cache optimization is restricted to native Claude routing'
else
  check_fail 'prompt-cache optimization is missing or leaks into Claudex GPT routing'
fi

for port in 8787 8317; do
  if command -v lsof >/dev/null 2>&1 && \
     lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    listener="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -Fn | rg '^n' | head -1 | cut -c2-)"
    if [[ "$listener" == "127.0.0.1:$port" ]]; then
      check_ok "loopback listener: $port"
    else
      check_fail "non-loopback listener on $port: $listener"
    fi
  elif command -v ss >/dev/null 2>&1 && \
       ss -ltn | rg -q "127\\.0\\.0\\.1:$port([[:space:]]|$)"; then
    check_ok "loopback listener: $port"
  else
    check_fail "no listener on $port"
  fi
done

installed_headroom_version="$(headroom_distribution_version \
  "$(command -v headroom 2>/dev/null)" 2>/dev/null || true)"
running_headroom_version="$(curl -fsS http://127.0.0.1:8787/health 2>/dev/null | jq -r '.version // empty' || true)"
if [[ -n "$installed_headroom_version" && "$installed_headroom_version" == "$running_headroom_version" ]]; then
  check_ok "Headroom runtime matches installed version $installed_headroom_version"
else
  check_fail "Headroom version drift (installed=${installed_headroom_version:-unknown}, running=${running_headroom_version:-unknown})"
fi

if curl -fsS http://127.0.0.1:8787/health 2>/dev/null | jq -e '
  .ready == true and .config.optimize == true and
  .config.cache == false and .config.memory == false and
  .config.code_graph == false and
  .config.runtime_env.HEADROOM_OUTPUT_SHAPER == "0" and
  .config.runtime_env.HEADROOM_VERBOSITY_AUTOTUNE == "0" and
  .config.runtime_env.HEADROOM_EFFORT_ROUTER == "0"
' >/dev/null; then
  check_ok 'Headroom does not limit worker output or effort'
else
  check_fail 'Headroom effective optimization policy has drifted'
fi

if curl --fail --silent --show-error http://127.0.0.1:8317/v1/models >"$models_response"; then
  model_count="$(jq '[.data[]? | select(.id | startswith("gpt-"))] | length' "$models_response")"
  claude_model_count="$(jq '[.data[]? | select(.id | startswith("claude-"))] | length' "$models_response")"
  if [[ "$model_count" -gt 0 ]]; then
    check_ok "CLIProxyAPI exposes $model_count GPT model(s)"
  else
    check_fail 'CLIProxyAPI is running but Codex OAuth/model discovery is incomplete'
  fi
  if [[ "$claude_model_count" -gt 0 ]] && \
     jq -e '.data[]? | select(.id == "claude-opus-4-8")' "$models_response" >/dev/null; then
    check_ok "CLIProxyAPI exposes $claude_model_count Claude model(s), including claude-opus-4-8"
  else
    check_fail 'CLIProxyAPI is running but Claude OAuth or claude-opus-4-8 discovery is incomplete'
  fi
else
  check_fail 'CLIProxyAPI /v1/models is unavailable'
fi

if "$WORKFLOW_DATA_ROOT/bin/claudex" --config "$CLAUDEX_CONFIG_FILE" config validate >/dev/null 2>&1; then
  check_ok 'Claudex config validates'
else
  check_fail 'Claudex config validation failed'
fi

auth_count="$(find "$WORKFLOW_DATA_ROOT/auth" -type f -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')"
auth_permissions_ok=true
while IFS= read -r auth_file; do
  [[ "$(file_mode "$auth_file")" == "600" ]] || auth_permissions_ok=false
done < <(find "$WORKFLOW_DATA_ROOT/auth" -type f -maxdepth 1 2>/dev/null)
if [[ "$auth_count" -ge 2 && "$auth_permissions_ok" == true ]]; then
  check_ok "$auth_count OAuth credential files exist with mode 0600 (content not inspected)"
elif [[ "$auth_count" -gt 0 ]]; then
  check_fail 'OAuth provider count is incomplete or credential permissions are not 0600'
else
  check_fail 'no OAuth credential files'
fi

if [[ "$failures" -gt 0 ]]; then
  printf '%s check(s) failed.\n' "$failures" >&2
  exit 1
fi
printf 'All checks passed.\n'
