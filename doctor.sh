#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
data_root="$(workflow_data_dir)"
config_root="${ORICHUM_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/orichum}"
failures=0

ok() { printf 'OK   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; failures=$((failures + 1)); }

python_identity=
if python_identity="$(
    validate_orichum_python \
      "$data_root" "$(orichum_python_entrypoint "$data_root")" 2>/dev/null
  )"; then
  IFS=$'\t' read -r python_version python_realpath <<<"$python_identity"
  ok "Private CPython 3.14 is active ($python_version; $python_realpath)"
else
  fail 'Private CPython 3.14 is missing, unsafe, or inactive'
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
  ok 'CLIProxyAPI, route proxy, and Headroom are owned and ready'
elif [[ "$provider_login_pending" == true ]]; then
  fail 'provider login is pending; register an account, then re-run install.sh'
else
  fail 'one or more Orichum services are absent, foreign, or unhealthy'
fi

claudex_config="$data_root/model-config/current/claudex.toml"
ports_valid=false
if IFS=$'\t' read -r \
    _ _ claudex_port route_port \
    < <(read_service_ports "$data_root") && \
   [[ "$(rg -c '^proxy_port = [0-9]+$' "$claudex_config" \
       2>/dev/null || true)" == 1 ]] && \
   rg -Fxq "proxy_port = $claudex_port" "$claudex_config" && \
   rg -Fxq \
     "X-Headroom-Base-Url = \"http://127.0.0.1:$route_port\"" \
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

if [[ -x "$data_root/bin/claudex" ]] && \
   "$data_root/bin/claudex" --version >/dev/null 2>&1; then
  ok "Claudex runtime is executable ($data_root/bin/claudex)"
else
  fail "Claudex runtime is unavailable ($data_root/bin/claudex)"
fi

if command -v claude >/dev/null 2>&1; then
  ok "Claude Code is available ($(command -v claude))"
else
  fail 'Claude Code is not on PATH'
fi

for tool in mempalace-mcp graphify-mcp; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool is available ($(command -v "$tool"))"
  else
    fail "$tool is not on PATH"
  fi
done

if [[ -f "$config_root/accounts.json" && \
      ! -L "$config_root/accounts.json" ]] && \
   [[ "$(path_mode "$config_root/accounts.json")" == 600 ]]; then
  ok 'named-account registry is private'
else
  fail 'named-account registry is missing or unsafe'
fi

if [[ -f "$data_root/cliproxy-management.key" && \
      ! -L "$data_root/cliproxy-management.key" ]] && \
   [[ "$(path_mode "$data_root/cliproxy-management.key")" == 600 ]]; then
  ok 'CLIProxyAPI management key is private'
else
  fail 'CLIProxyAPI management key is missing or unsafe'
fi

if [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/ensure-graphify-hook.py" ]] && \
   [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/check-local-services.sh" ]] && \
   [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/route-mempalace-input.py" ]]; then
  ok 'Graphify, service-health, and Mempalace hooks are executable'
else
  fail 'one or more Orichum integration hooks are unavailable'
fi

if (( failures > 0 )); then
  printf '\nDoctor found %d problem(s). Follow the specific remediation above.\n' \
    "$failures"
  exit 1
fi
printf '\nOrichum is ready.\n'
