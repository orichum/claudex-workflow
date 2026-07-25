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

if [[ -n "$python_identity" ]] && \
   [[ -f "$WORKFLOW_ROOT/integrations/common/graph_manager.py" && \
      ! -L "$WORKFLOW_ROOT/integrations/common/graph_manager.py" ]] && \
   [[ -f "$WORKFLOW_ROOT/integrations/common/graph_hooks.py" && \
      ! -L "$WORKFLOW_ROOT/integrations/common/graph_hooks.py" ]] && \
   [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/check-local-services.sh" ]] && \
   [[ -x "$WORKFLOW_ROOT/controller/plugin/scripts/route-mempalace-input.py" ]] && \
   PYTHONDONTWRITEBYTECODE=1 "$orichum_python" -I -B - \
     "$WORKFLOW_ROOT" >/dev/null 2>&1 <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from integrations.common.graph_hooks import (
    graph_hook_status,
    install_graph_hooks,
    remove_upstream_graphify_hooks,
)
from integrations.common.graph_manager import graph_main, sync_graph

for interface in (
    graph_hook_status,
    install_graph_hooks,
    remove_upstream_graphify_hooks,
    graph_main,
    sync_graph,
):
    if not callable(interface):
        raise SystemExit(1)
PY
then
  ok 'repository graph manager and hook contract are available'
else
  fail 'repository graph manager or integration hook contract is unavailable'
fi

if (( failures > 0 )); then
  printf '\nDoctor found %d problem(s). Follow the specific remediation above.\n' \
    "$failures"
  exit 1
fi
printf '\nOrichum is ready.\n'
