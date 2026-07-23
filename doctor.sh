#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
WORKFLOW_DATA_ROOT="$(workflow_data_dir)"
CLAUDEX_CONFIG_FILE="$(model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml)"
ROUTING_FILE="$WORKFLOW_ROOT/controller/model-routing.json"

failures=0
doctor_fixture=""
doctor_temp_dir=""
models_response="/dev/null"
claudex_models_response="/dev/null"
check_ok() { printf 'OK   %s\n' "$1"; }
check_fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }
cleanup_doctor_models() {
  if [[ -n "$doctor_temp_dir" ]]; then
    case "$doctor_temp_dir" in
      /tmp/claudex-doctor.*|/private/tmp/claudex-doctor.*)
        rm -rf -- "$doctor_temp_dir"
        ;;
    esac
  fi
  if [[ -n "$doctor_fixture" ]]; then
    case "$doctor_fixture" in
      /tmp/claudex-doctor-session.*|/private/tmp/claudex-doctor-session.*)
        rm -rf -- "$doctor_fixture"
        ;;
    esac
  fi
}
trap cleanup_doctor_models EXIT
if doctor_temp_dir="$(umask 077; mktemp -d /tmp/claudex-doctor.XXXXXX \
    2>/dev/null)" && chmod 0700 "$doctor_temp_dir" 2>/dev/null; then
  models_response="$doctor_temp_dir/models.response"
  claudex_models_response="$doctor_temp_dir/claudex-models.response"
else
  check_fail 'doctor could not create private temporary state'
fi

service_ports_ok=true
if ! IFS=$'\t' read -r CLIPROXY_PORT HEADROOM_PORT CLAUDEX_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  service_ports_ok=false
  CLIPROXY_PORT=8317
  HEADROOM_PORT=8787
  CLAUDEX_PROXY_PORT=13456
fi
if [[ "$service_ports_ok" == true ]]; then
  check_ok "service ports are valid (CLIProxyAPI=$CLIPROXY_PORT, Headroom=$HEADROOM_PORT, Claudex=$CLAUDEX_PROXY_PORT)"
else
  check_fail 'service port configuration is invalid'
fi

case "$(uname -s)" in
  Darwin)
    claudex_proxy_platform=darwin
    if command -v lsof >/dev/null 2>&1; then
      check_ok 'Claudex proxy PID/listener inspection has lsof'
    else
      check_fail 'Claudex proxy PID/listener inspection requires lsof'
    fi
    ;;
  Linux)
    claudex_proxy_platform=systemd
    if command -v ss >/dev/null 2>&1; then
      check_ok 'Claudex proxy PID/listener inspection has ss'
    else
      check_fail 'Claudex proxy PID/listener inspection requires ss (install iproute2)'
    fi
    ;;
  *)
    claudex_proxy_platform=""
    check_fail 'Claudex proxy service management requires macOS, Linux, or WSL'
    ;;
esac

claudex_proxy_service_file=""
claudex_proxy_service_label=""
claudex_proxy_service_unit=""
claudex_proxy_definition_ok=false
if [[ -n "$claudex_proxy_platform" ]] && \
   IFS=$'\t' read -r \
     claudex_proxy_service_file claudex_proxy_service_label claudex_proxy_service_unit \
     < <(claudex_proxy_service_identity "$claudex_proxy_platform") && \
   claudex_proxy_service_is_owned \
     "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT"; then
  claudex_proxy_target_state="$(managed_service_target_state \
    "$claudex_proxy_platform" \
    "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null || true)"
  claudex_proxy_loaded_definition="$(managed_service_definition_path \
    "$claudex_proxy_platform" \
    "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null || true)"
  if [[ "$claudex_proxy_target_state" == loaded && \
        "$claudex_proxy_loaded_definition" == "$claudex_proxy_service_file" ]]; then
    claudex_proxy_definition_ok=true
  fi
fi
if [[ "$claudex_proxy_definition_ok" == true ]]; then
  check_ok 'Claudex proxy definition is workflow-owned'
else
  check_fail 'Claudex proxy definition is workflow-owned'
fi

claudex_proxy_pid=""
if [[ -n "$claudex_proxy_platform" ]]; then
  claudex_proxy_pid="$(managed_service_main_pid \
    "$claudex_proxy_platform" \
    "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null || true)"
fi
if [[ -n "$claudex_proxy_pid" ]] && \
   pid_owns_loopback_listener "$claudex_proxy_pid" "$CLAUDEX_PROXY_PORT"; then
  check_ok "Claudex proxy service PID owns 127.0.0.1:$CLAUDEX_PROXY_PORT"
else
  check_fail "Claudex proxy service PID owns 127.0.0.1:$CLAUDEX_PROXY_PORT"
fi

claudex_controller_model="$(claudex_config_default_model \
  "$CLAUDEX_CONFIG_FILE" 2>/dev/null || true)"
if [[ -n "$claudex_controller_model" ]] && \
   curl -fsS --connect-timeout 1 --max-time 2 \
     "http://127.0.0.1:$CLAUDEX_PROXY_PORT/v1/models" \
     >"$claudex_models_response" 2>/dev/null && \
   claudex_proxy_models_response_is_ready \
     "$claudex_models_response" "$claudex_controller_model"; then
  check_ok 'Claudex proxy exposes the configured controller model'
else
  check_fail 'Claudex proxy exposes the configured controller model'
fi

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

if (
  cd "$WORKFLOW_ROOT"
  python3 -B - "$ROUTING_FILE" <<'PY'
import sys
from pathlib import Path
from integrations.common.model_routing import load_routing

load_routing(Path(sys.argv[1]))
PY
) >/dev/null 2>&1; then
  check_ok 'model routing schema is strictly valid'
else
  check_fail 'model routing schema is invalid'
fi

if (
  cd "$WORKFLOW_ROOT"
  python3 -B -m integrations.common.project_context context \
    --config "$WORKFLOW_ROOT/controller/project-context.json" \
    --routing-config "$ROUTING_FILE" list
) >/dev/null 2>&1; then
  check_ok 'every project context model stack reference is valid'
else
  check_fail 'a project context model stack reference is invalid'
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
git init -q "$fixture_project"
install -d -m 0755 "$fixture_project/graphify-out"
jq -n '{
  directed: false, multigraph: false, graph: {},
  nodes: [{id: "claudex-audit", label: "claudex-audit"}], links: []
}' >"$fixture_project/graphify-out/graph.json"
install -d -m 0755 "$fixture_workflow/integrations/common"
install -m 0644 \
  "$WORKFLOW_ROOT/integrations/__init__.py" \
  "$fixture_workflow/integrations/__init__.py"
install -m 0644 \
  "$WORKFLOW_ROOT/integrations/common/__init__.py" \
  "$WORKFLOW_ROOT/integrations/common/context_population.py" \
  "$WORKFLOW_ROOT/integrations/common/project_context.py" \
  "$WORKFLOW_ROOT/integrations/common/session_config.py" \
  "$fixture_workflow/integrations/common/"
jq -n \
  --arg palace "$fixture_palace" \
  --arg root "$fixture_project" \
  '{contexts:[{root:$root,dockerProfile:"fixture",memoryPalace:$palace,memoryWing:"fixture"}]}' \
  >"$doctor_fixture/project-context.json"
fixture_mcp=""
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
     ([.mcpServers | keys[]] - ["docker", "mempalace", "graphify"] | length == 0) and
     (.mcpServers.mempalace | type == "object") and
     (.mcpServers.graphify | type == "object")
   ' "$fixture_mcp" >/dev/null; then
  check_ok 'disposable strict MCP generation exposes only supported integrations'
else
  check_fail 'disposable strict MCP generation failed or exposed an unsupported server'
fi

if [[ -n "$fixture_mcp" ]] && \
   mempalace_command="$(jq -er '.mcpServers.mempalace.command' "$fixture_mcp" 2>/dev/null)" && \
   mempalace_palace="$(jq -er '.mcpServers.mempalace.args[1]' "$fixture_mcp" 2>/dev/null)" && \
   PYTHONDONTWRITEBYTECODE=1 python3 -B \
     "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
     --require-tool mempalace_get_taxonomy \
     --require-tool mempalace_search \
     --require-tool mempalace_checkpoint \
     -- "$mempalace_command" --palace "$mempalace_palace" >/dev/null 2>&1; then
  check_ok 'MemPalace completes MCP initialization and exposes required tools'
else
  check_fail 'MemPalace MCP protocol readiness failed'
fi

if [[ -n "$fixture_mcp" ]] && \
   graphify_command="$(jq -er '.mcpServers.graphify.command' "$fixture_mcp" 2>/dev/null)" && \
   graphify_graph="$(jq -er '.mcpServers.graphify.args[1]' "$fixture_mcp" 2>/dev/null)" && \
   PYTHONDONTWRITEBYTECODE=1 python3 -B \
     "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
     --require-tool query_graph \
     --require-tool graph_stats \
     -- "$graphify_command" --graph "$graphify_graph" >/dev/null 2>&1; then
  check_ok 'Graphify completes MCP initialization and exposes required tools'
else
  check_fail 'Graphify MCP protocol readiness failed'
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
for binary in claudex-gpt claude-headroom claudex-login claudex-models; do
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
  "$plugin_root/agents/repository-explorer.md"
  "$plugin_root/agents/repository-verifier.md"
  "$plugin_root/agents/correctness-critic.md"
  "$plugin_root/agents/architecture-advisor.md"
  "$plugin_root/agents/implementation-worker.md"
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
workflow_role_surface_is_exact "$WORKFLOW_ROOT" "$plugin_root" || \
  agent_contracts_ok=false
for agent_name in \
  repository-explorer repository-verifier correctness-critic \
  architecture-advisor implementation-worker
do
  agent_file="$plugin_root/agents/$agent_name.md"
  rg -q "^name: $agent_name$" "$agent_file" || agent_contracts_ok=false
  rg -q '^model: inherit$' "$agent_file" || agent_contracts_ok=false
  rg -q '^effort: high$' "$agent_file" || agent_contracts_ok=false
done
rg -q '^maxTurns: 9$' \
  "$plugin_root/agents/correctness-critic.md" || agent_contracts_ok=false
if [[ "$agent_contracts_ok" == true ]]; then
  check_ok 'workflow source roles and executable guard behavior are consistent'
else
  check_fail 'workflow source roles or executable guard behavior are inconsistent'
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

for port in "$HEADROOM_PORT" "$CLIPROXY_PORT"; do
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
  "$WORKFLOW_DATA_ROOT/headroom/bin/headroom" 2>/dev/null || true)"
running_headroom_version="$(curl -fsS \
  "http://127.0.0.1:$HEADROOM_PORT/health" 2>/dev/null | \
  jq -r '.version // empty' || true)"
if [[ -n "$installed_headroom_version" && "$installed_headroom_version" == "$running_headroom_version" ]]; then
  check_ok "Headroom runtime matches installed version $installed_headroom_version"
else
  check_fail "Headroom version drift (installed=${installed_headroom_version:-unknown}, running=${running_headroom_version:-unknown})"
fi

if curl -fsS "http://127.0.0.1:$HEADROOM_PORT/health" 2>/dev/null | jq -e '
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

installed_cliproxy_help="$(
  (
    cd "$WORKFLOW_DATA_ROOT/bin"
    ./cli-proxy-api --help 2>&1
  )
)" || installed_cliproxy_help=""
installed_cliproxy_version="$(
  extract_semver "$installed_cliproxy_help" 2>/dev/null || true
)"
headroom_models_file="$WORKFLOW_DATA_ROOT/headroom/config/models.json"
if [[ -n "$installed_cliproxy_version" ]] && \
   [[ -f "$headroom_models_file" && ! -L "$headroom_models_file" ]] && \
   [[ "$(file_mode "$headroom_models_file")" == 600 ]] && \
   (
     cd "$WORKFLOW_ROOT"
     PYTHONDONTWRITEBYTECODE=1 python3 -B \
       -m integrations.common.headroom_models validate \
       --catalog "$headroom_models_file" \
       --expected-repository router-for-me/CLIProxyAPI \
       --expected-version "$installed_cliproxy_version"
   ); then
  check_ok "Headroom model metadata matches CLIProxyAPI $installed_cliproxy_version"
else
  check_fail "Headroom model metadata is missing, unsafe, or version-drifted"
fi

headroom_audit_payload='{"model":"claudex-audit-model-does-not-exist","max_tokens":0,"messages":[]}'
direct_audit_response="$(curl -sS --connect-timeout 1 --max-time 8 \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: claudex-passthrough' \
  --data "$headroom_audit_payload" \
  "http://127.0.0.1:$CLIPROXY_PORT/v1/messages" 2>/dev/null || true)"
headroom_audit_response="$(curl -sS --connect-timeout 1 --max-time 8 \
  -H 'content-type: application/json' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'x-api-key: claudex-passthrough' \
  -H "X-Headroom-Base-Url: http://127.0.0.1:$CLIPROXY_PORT" \
  --data "$headroom_audit_payload" \
  "http://127.0.0.1:$HEADROOM_PORT/v1/messages" 2>/dev/null || true)"
if [[ -n "$direct_audit_response" && -n "$headroom_audit_response" ]] && \
   [[ "$(jq -c '.error // empty' <<<"$direct_audit_response" 2>/dev/null)" == \
      "$(jq -c '.error // empty' <<<"$headroom_audit_response" 2>/dev/null)" ]] && \
   jq -e '.error.message == "unknown provider for model claudex-audit-model-does-not-exist"' \
     <<<"$headroom_audit_response" >/dev/null 2>&1; then
  check_ok 'Headroom forwards the Anthropic message route to CLIProxyAPI'
else
  check_fail 'Headroom-to-CLIProxyAPI message routing failed'
fi

if curl --fail --silent --show-error \
    --connect-timeout 1 --max-time 4 \
    "http://127.0.0.1:$CLIPROXY_PORT/v1/models" >"$models_response"; then
  model_count="$(
    cd "$WORKFLOW_ROOT"
    python3 -B - "$models_response" 2>/dev/null <<'PY'
import sys
from pathlib import Path
from integrations.common.model_routing import load_catalog

print(len(load_catalog(Path(sys.argv[1]))))
PY
  )" || model_count=""
  if [[ "$model_count" =~ ^[1-9][0-9]*$ ]]; then
    check_ok "CLIProxyAPI exposes $model_count client-visible model(s)"
  else
    check_fail 'CLIProxyAPI returned an invalid or empty model catalogue'
  fi
  default_stack="$(jq -er '.defaultStack' "$ROUTING_FILE" 2>/dev/null || true)"
  if [[ -n "$default_stack" ]] && (
    cd "$WORKFLOW_ROOT"
    python3 -B -m integrations.common.model_routing validate \
      --routing-config "$ROUTING_FILE" \
      --models-file "$models_response"
  ) >/dev/null 2>&1; then
    check_ok "default model stack $default_stack resolves against the live catalogue"
  else
    check_fail 'default model stack does not resolve against the live catalogue'
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
if [[ "$auth_count" -gt 0 && "$auth_permissions_ok" == true ]]; then
  check_ok "$auth_count OAuth credential files exist with mode 0600 (content not inspected)"
elif [[ "$auth_permissions_ok" == true ]]; then
  check_ok 'no OAuth credential files exist yet'
else
  check_fail 'an OAuth credential file does not have mode 0600'
fi

latest_session_status=0
latest_session="$(
  python3 - "$WORKFLOW_DATA_ROOT/state/sessions" <<'PY'
import os
import stat
import sys
from pathlib import Path

sessions = Path(sys.argv[1])
owned = []
try:
    entries = list(os.scandir(sessions))
except OSError:
    raise SystemExit(1)
for entry in entries:
    if not entry.name.startswith("run.") or entry.is_symlink():
        continue
    try:
        observed = entry.stat(follow_symlinks=False)
    except OSError:
        continue
    if (
        stat.S_ISDIR(observed.st_mode)
        and observed.st_uid == os.getuid()
    ):
        owned.append((observed.st_mtime_ns, entry.name))
if owned:
    print(sessions / max(owned)[1])
PY
)" || latest_session_status=$?
if [[ "$latest_session_status" -ne 0 ]]; then
  check_fail 'latest owned session could not be identified'
elif [[ -z "$latest_session" ]]; then
  check_ok 'no prior effective session to inspect'
else
  context_digest="$(sha256_file "$latest_session/context.json" 2>/dev/null || true)"
  effective_digest="$(
    sha256_file "$latest_session/effective-models.json" 2>/dev/null || true
  )"
  if [[ -n "$context_digest" && -n "$effective_digest" ]] && \
     assert_owned_session \
       "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" "$latest_session" \
       "$context_digest" "$effective_digest" >/dev/null 2>&1; then
    check_ok 'latest session effective mapping is internally consistent'
    headroom_model_coverage="$(
      cd "$WORKFLOW_ROOT"
      PYTHONDONTWRITEBYTECODE=1 python3 -B - \
        "$headroom_models_file" \
        "$latest_session/effective-models.json" <<'PY'
import json
import sys
from pathlib import Path

from integrations.common.headroom_models import validate_catalog

catalog = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
effective = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
limits = validate_catalog(catalog)
selected = {effective["controller"], *effective["agents"].values()}
print(f"{len(selected & limits.keys())}\t{len(selected)}")
PY
    )" || headroom_model_coverage=""
    if IFS=$'\t' read -r covered_models selected_models \
        <<<"$headroom_model_coverage" && \
       [[ "$covered_models" =~ ^[0-9]+$ ]] && \
       [[ "$selected_models" =~ ^[1-9][0-9]*$ ]]; then
      check_ok "Headroom has exact-release context limits for $covered_models/$selected_models effective model(s); uncovered models use Headroom fallback"
    else
      check_fail 'Headroom effective model coverage could not be inspected'
    fi
  else
    check_fail 'latest session effective mapping is internally inconsistent'
  fi
fi

if [[ "$failures" -gt 0 ]]; then
  printf '%s check(s) failed.\n' "$failures" >&2
  exit 1
fi
printf 'All checks passed.\n'
