#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
data_root="$(workflow_data_dir)"
home_root="$(orichum_home_dir)"
config_root="$(workflow_config_dir)"
failures=0
doctor_temp="$(mktemp -d "${TMPDIR:-/tmp}/orichum-doctor.XXXXXX")"
trap 'rm -rf -- "$doctor_temp"' EXIT

ok() { printf 'OK   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

orichum_python="$(orichum_python_entrypoint "$data_root")"
python_identity=
if python_identity="$(
    validate_orichum_python \
      "$data_root" "$orichum_python" 2>/dev/null
  )"; then
  IFS=$'\t' read -r python_version python_realpath <<<"$python_identity"
  ok "Private CPython 3.14 is active ($python_version; $python_realpath)"
else
  fail 'Private CPython 3.14 is missing, unsafe, or inactive'
fi

runtime_pointer="$home_root/runtime/current"
runtime_release_valid=false
if [[ -n "$python_identity" && -L "$runtime_pointer" ]] && \
   [[ "$(workflow_physical_path "$runtime_pointer" 2>/dev/null)" == \
      "$WORKFLOW_ROOT" ]] && \
   (
     cd "$WORKFLOW_ROOT"
     PYTHONDONTWRITEBYTECODE=1 "$orichum_python" -I -B - \
       "$WORKFLOW_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.runtime_bundle import validate

validate(root)
PY
   ) >/dev/null 2>&1; then
  runtime_release_valid=true
fi
if [[ "$runtime_release_valid" == true ]]; then
  ok "standalone runtime is active ($WORKFLOW_ROOT)"
else
  fail 'standalone runtime is missing, stale, or not content-verified'
fi

if ORICHUM_CONFIG_HOME="$config_root" ORICHUM_DATA_HOME="$data_root" \
    "$WORKFLOW_ROOT/bin/orichum" config validate >/dev/null 2>&1; then
  ok 'focused control plane is valid'
else
  fail 'focused control plane is invalid'
fi

provider_login_pending=false
if [[ -f "$config_root/accounts.json" ]] && \
   jq -e '.schemaVersion == 2 and (.accounts | length == 0)' \
     "$config_root/accounts.json" >/dev/null 2>&1; then
  provider_login_pending=true
fi

if "$WORKFLOW_ROOT/bin/orichum-runtime-ready" "$data_root" \
    >/dev/null 2>&1; then
  ok 'CLIProxyAPI, LeanCTX proxy, and route proxy are owned and ready'
elif [[ "$provider_login_pending" == true ]]; then
  fail 'provider login is pending; register an account, then re-run install.sh'
else
  fail 'one or more Orichum services are absent, foreign, or unhealthy'
fi

claudex_config="$data_root/model-config/current/claudex.toml"
ports_valid=false
if IFS=$'\t' read -r \
    _ claudex_port route_port _ \
    < <(read_service_ports "$data_root") && \
   [[ "$(rg -c '^proxy_port = [0-9]+$' "$claudex_config" \
       2>/dev/null || true)" == 1 ]] && \
   rg -Fxq "proxy_port = $claudex_port" "$claudex_config" && \
   rg -Fxq \
     "base_url = \"http://127.0.0.1:$route_port\"" \
     "$claudex_config"; then
  ports_valid=true
fi
if [[ "$ports_valid" == true ]]; then
  ok 'Claudex template separates per-session and recovery proxy ports'
elif [[ "$provider_login_pending" == true && \
        ! -e "$claudex_config" && ! -L "$claudex_config" ]]; then
  ok 'Claudex template is pending provider login'
else
  fail 'Claudex template conflates its listener with the recovery proxy'
fi

status_renderer="$WORKFLOW_ROOT/bin/orichum-statusline"
isolated_claude_settings="$data_root/claude-config/settings.json"
status_line_valid=false
if [[ -n "$python_identity" ]] && \
   [[ -d "$data_root/claude-config" && \
      ! -L "$data_root/claude-config" ]] && \
   [[ "$(path_mode "$data_root/claude-config")" == 700 ]] && \
   [[ -f "$isolated_claude_settings" && \
      ! -L "$isolated_claude_settings" ]] && \
   [[ "$(path_mode "$isolated_claude_settings")" == 600 ]] && \
   [[ "$(path_uid "$isolated_claude_settings")" == "$(id -u)" ]] && \
   cmp -s "$WORKFLOW_ROOT/controller/settings.json" \
     "$isolated_claude_settings" && \
   [[ -f "$status_renderer" && ! -L "$status_renderer" && \
      -x "$status_renderer" ]] && \
   [[ "$(path_uid "$status_renderer")" == "$(id -u)" ]] && \
   [[ "$(path_mode "$status_renderer")" == 755 ]] && \
   [[ "$(
     printf '{}' | \
       ORICHUM_DATA_HOME="$data_root" \
       ORICHUM_CONFIG_HOME="$config_root" \
         "$status_renderer" 2>/dev/null
   )" == 'ORICHUM │ status unavailable' ]]; then
  status_line_valid=true
fi
if [[ "$status_line_valid" == true ]]; then
  ok 'Orichum status line is installed and isolated'
else
  fail 'Orichum status line is missing, unsafe, or misconfigured'
fi

route_status_private=false
route_status_file="$doctor_temp/route-status.json"
route_status_code=
if [[ "$ports_valid" == true ]]; then
  route_status_code="$(
    curl --silent --show-error \
      --connect-timeout 1 --max-time 2 \
      --output "$route_status_file" \
      --write-out '%{http_code}' \
      "http://127.0.0.1:${route_port}/status?session_id=oc-s-0000000000000000" \
      2>/dev/null || true
  )"
fi
if [[ "$route_status_code" == 404 ]] && \
   jq -e '
     type == "object" and
     keys == ["error"] and
     .error == "status not found"
   ' "$route_status_file" >/dev/null 2>&1; then
  route_status_private=true
fi
if [[ "$provider_login_pending" == true && \
      ( -z "$route_status_code" || "$route_status_code" == 000 ) ]]; then
  ok 'route telemetry is pending provider login'
elif [[ "$route_status_private" == true ]]; then
  ok 'route telemetry endpoint is private and redacted'
else
  fail 'route telemetry endpoint is unavailable or exposed route data'
fi

if [[ -x "$data_root/bin/claudex" ]] && \
   "$data_root/bin/claudex" --version >/dev/null 2>&1; then
  ok "Claudex runtime is executable ($data_root/bin/claudex)"
else
  fail "Claudex runtime is unavailable ($data_root/bin/claudex)"
fi

leanctx_binary="$data_root/bin/lean-ctx"
if managed_executable_is_safe "$leanctx_binary" && \
   "$leanctx_binary" --version >/dev/null 2>&1; then
  if leanctx_ort_dylib_path="$(
    verified_leanctx_ort_dylib_path \
      "$leanctx_binary" "$data_root" "$doctor_temp"
  )"; then
    ok "LeanCTX managed ONNX Runtime is available ($leanctx_ort_dylib_path)"
    if probe_leanctx_capabilities \
        "$leanctx_binary" "$orichum_python" "$WORKFLOW_ROOT" \
        "$doctor_temp" "$leanctx_ort_dylib_path" \
        "$data_root/leanctx/cache" >/dev/null 2>&1; then
      ok "LeanCTX exposes the bounded headless MCP surface ($leanctx_binary)"
    else
      fail "LeanCTX is unavailable or exposes tools outside Orichum policy"
    fi
  else
    fail "LeanCTX managed ONNX Runtime is unavailable or unsafe"
  fi
else
  fail "LeanCTX is unavailable or exposes tools outside Orichum policy"
fi

atlassian_binary="$data_root/tools/bin/mcp-atlassian"
if [[ -x "$atlassian_binary" ]] && \
   "$atlassian_binary" --version >/dev/null 2>&1; then
  ok "mcp-atlassian is installed for project-bound Jira sessions ($atlassian_binary)"
else
  fail "mcp-atlassian is unavailable ($atlassian_binary)"
fi

atlassian_bindings_ready=true
atlassian_binding_count=0
while IFS= read -r atlassian_root; do
  [[ -n "$atlassian_root" ]] || continue
  atlassian_binding_count=$((atlassian_binding_count + 1))
  ORICHUM_DATA_HOME="$data_root" \
  ORICHUM_CONFIG_HOME="$config_root" \
    "$orichum_python" -I -B \
      "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
      --timeout 15 \
      --require-tool jira_get_issue \
      --require-tool jira_create_issue \
      -- "$WORKFLOW_ROOT/bin/orichum-atlassian-mcp" \
      "$atlassian_root" >/dev/null 2>&1 || {
        atlassian_bindings_ready=false
        break
      }
done < <(
  jq -r '
    .contexts[]
    | select(.atlassian != null)
    | .root
  ' "$config_root/projects.json"
)
if [[ "$atlassian_binding_count" -eq 0 ]]; then
  ok 'no project-bound Jira configurations are present'
elif [[ "$atlassian_bindings_ready" == true ]]; then
  ok 'project-bound Jira configurations initialize read/write tools'
else
  fail 'one or more project-bound Jira configurations failed MCP readiness'
fi

if command -v claude >/dev/null 2>&1; then
  ok "Claude Code is available ($(command -v claude))"
else
  fail 'Claude Code is not on PATH'
fi

if [[ -f "$config_root/accounts.json" && \
      ! -L "$config_root/accounts.json" ]] && \
   [[ "$(path_mode "$config_root/accounts.json")" == 600 ]]; then
  ok 'named-account registry is private'
else
  fail 'named-account registry is missing or unsafe'
fi

stack_bindings="$config_root/stack-bindings.json"
if [[ ! -e "$stack_bindings" && ! -L "$stack_bindings" ]]; then
  ok 'model-stack candidates use automatic provider account selection'
elif [[ -n "$python_identity" ]] && \
     [[ -f "$stack_bindings" && ! -L "$stack_bindings" ]] && \
     [[ "$(path_mode "$stack_bindings")" == 600 ]] && \
     PYTHONDONTWRITEBYTECODE=1 "$orichum_python" -I -B - \
       "$WORKFLOW_ROOT" "$config_root" >/dev/null 2>&1 <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
config_root = Path(sys.argv[2]).resolve(strict=True)
sys.path.insert(0, str(root))

from integrations.common.account_registry import load_accounts
from integrations.common.orichum_config import (
    default_config_paths,
    load_control_plane,
)
from integrations.common.stack_bindings import load_stack_bindings
from integrations.common.stack_definition import normalize_model_stacks
from integrations.common.stack_store import validate_stack_bindings

control = load_control_plane(default_config_paths(config_root))
validate_stack_bindings(
    normalize_model_stacks(control.documents["model-stacks"]),
    load_stack_bindings(config_root / "stack-bindings.json"),
    load_accounts(config_root / "accounts.json"),
)
PY
then
  ok 'model-stack account locks are valid and private'
else
  fail 'model-stack account locks are invalid or unsafe'
fi

if [[ -f "$data_root/cliproxy-management.key" && \
      ! -L "$data_root/cliproxy-management.key" ]] && \
   [[ "$(path_mode "$data_root/cliproxy-management.key")" == 600 ]]; then
  ok 'CLIProxyAPI management key is private'
else
  fail 'CLIProxyAPI management key is missing or unsafe'
fi

if [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/check-local-services.sh" ]]; then
  ok 'service-health hook is executable'
else
  fail 'service-health hook is unavailable'
fi

if (( failures > 0 )); then
  printf '\nDoctor found %d problem(s). Follow the specific remediation above.\n' \
    "$failures"
  exit 1
fi
printf '%s\n' \
  '' \
  'Orichum local components are ready.' \
  'Doctor does not launch a model session, contact a provider, or index your project.'
