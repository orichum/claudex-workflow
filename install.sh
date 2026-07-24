#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true

USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"
WORKFLOW_DATA_ROOT="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || \
  workflow_die "refusing unsafe ORICHUM_DATA_HOME"
ORICHUM_CONFIG_ROOT="${ORICHUM_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/orichum}"
case "$ORICHUM_CONFIG_ROOT" in
  /*) ;;
  *) workflow_die "ORICHUM_CONFIG_HOME must be an absolute path" ;;
esac
SERVICE_LABEL="io.orichum.cliproxy"

workflow_cleanup_init
trap 'workflow_cleanup "$?"' EXIT
trap 'workflow_cleanup 129' HUP
trap 'workflow_cleanup 130' INT
trap 'workflow_cleanup 143' TERM

for command_name in curl gh jq tar install python3 git rg uv; do
  command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
done
command -v claude >/dev/null || workflow_die "Claude Code is not installed or not on PATH"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
  workflow_die "Python 3.10 or newer is required"
[[ -x "$WORKFLOW_ROOT/bin/orichum" ]] || \
  workflow_die "required launcher is missing or not executable: orichum"
for helper in \
    orichum-context orichum-doctor orichum-headroom orichum-login \
    orichum-plugin orichum-route-proxy orichum-runtime-ready \
    orichum-verify-cliproxy; do
  [[ -x "$WORKFLOW_ROOT/bin/$helper" ]] || \
    workflow_die "required Orichum helper is missing or not executable: $helper"
done
for managed_launcher in orichum; do
  if [[ -d "$USER_BIN_DIR/$managed_launcher" && \
        ! -L "$USER_BIN_DIR/$managed_launcher" ]]; then
    workflow_die \
      "refusing to replace real launcher directory: $USER_BIN_DIR/$managed_launcher"
  fi
done

install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT" "$WORKFLOW_DATA_ROOT/state" "$ORICHUM_CONFIG_ROOT"
acquire_workflow_lock "$WORKFLOW_DATA_ROOT/state/install.lock"

case "$(uname -s)" in
  Darwin)
    platform=darwin
    cliproxy_os=darwin
    claudex_os=apple-darwin
    ;;
  Linux)
    platform=systemd
    cliproxy_os=linux
    claudex_os=unknown-linux-gnu
    if [[ "$(linux_environment_kind)" == wsl1 ]]; then
      workflow_die "WSL1 is unsupported; use WSL2 with systemd enabled"
    fi
    systemctl --user show-environment >/dev/null 2>&1 || \
      workflow_die "a working systemd user manager is required"
    command -v ss >/dev/null 2>&1 || \
      workflow_die "missing required command: ss (install iproute2)"
    ;;
  *) workflow_die "supported platforms are macOS, Linux, and WSL2" ;;
esac

case "$(uname -m)" in
  arm64|aarch64)
    cliproxy_arch=aarch64
    claudex_arch=aarch64
    ;;
  x86_64|amd64)
    cliproxy_arch=amd64
    claudex_arch=x86_64
    ;;
  *) workflow_die "unsupported CPU architecture: $(uname -m)" ;;
esac

if [[ "$platform" == darwin ]]; then
  for command_name in launchctl plutil lsof; do
    command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
  done
fi

(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 python3 -B - \
    "$WORKFLOW_ROOT/config" <<'PY'
import sys
from pathlib import Path
from integrations.common.orichum_config import (
    default_config_paths,
    load_control_plane,
)

load_control_plane(default_config_paths(Path(sys.argv[1])))
PY
) || workflow_die "source Orichum control plane is invalid"

while IFS= read -r configured_palace; do
  case "$configured_palace" in
    "~/"*) resolved_palace="$HOME/${configured_palace#\~/}" ;;
    /*) resolved_palace="$configured_palace" ;;
    *) workflow_die "memoryPalace must be absolute or use ~/ syntax" ;;
  esac
  install -d -m 0700 "$resolved_palace"
done < <(jq -er '.contexts[].memoryPalace' "$WORKFLOW_ROOT/config/projects.json")

validation_config="$(mktemp -d "${TMPDIR:-/tmp}/orichum-plugin.XXXXXX")"
register_cleanup_path "$validation_config"
chmod 0700 "$validation_config"
CLAUDE_CONFIG_DIR="$validation_config" \
  claude plugin validate --strict "$WORKFLOW_ROOT/controller/plugin" >/dev/null || \
  workflow_die "controller plugin validation failed"
rm -rf -- "$validation_config"

install -d -m 0755 "$USER_BIN_DIR"
install -d -m 0700 "$WORKFLOW_DATA_ROOT"
install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT/bin" \
  "$WORKFLOW_DATA_ROOT/auth" \
  "$WORKFLOW_DATA_ROOT/claude-config" \
  "$WORKFLOW_DATA_ROOT/state" \
  "$WORKFLOW_DATA_ROOT/state/sessions" \
  "$WORKFLOW_DATA_ROOT/logs" \
  "$WORKFLOW_DATA_ROOT/headroom/bin" \
  "$WORKFLOW_DATA_ROOT/headroom/config" \
  "$WORKFLOW_DATA_ROOT/headroom/state" \
  "$WORKFLOW_DATA_ROOT/headroom/tools"

installer_temp="$(mktemp -d "${TMPDIR:-/tmp}/orichum-install.XXXXXX")"
register_cleanup_path "$installer_temp"
snapshot_dir="$installer_temp/snapshots"
install -d -m 0700 "$snapshot_dir"
python_entrypoint="$(orichum_python_entrypoint "$WORKFLOW_DATA_ROOT")"
snapshot_path "$python_entrypoint" "$snapshot_dir" orichum-python
python_transaction_active=false
python_candidate_generation=
rollback_python_activation() {
  [[ "${python_transaction_active:-false}" == true ]] || return 0
  restore_snapshot "$python_entrypoint" \
    "$snapshot_dir" orichum-python || return 1
  snapshot_path_matches "$python_entrypoint" \
    "$snapshot_dir" orichum-python || return 1
  remove_orichum_python_generation \
    "$WORKFLOW_DATA_ROOT" "${python_candidate_generation:-}"
}
python_transaction_active=true
WORKFLOW_ROLLBACK_HANDLER=rollback_python_activation
WORKFLOW_TRANSACTION_ACTIVE=true
IFS=$'\t' read -r \
  orichum_python_action orichum_python_version orichum_python_candidate \
  python_candidate_generation < <(
    install_or_reuse_orichum_python "$WORKFLOW_DATA_ROOT"
  ) || workflow_die "private Orichum Python could not be provisioned"
(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 "$orichum_python_candidate" -I -B - \
    "$WORKFLOW_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
import integrations.common.orichum_cli  # noqa: F401
import integrations.common.route_proxy  # noqa: F401

for source in sorted((root / "integrations" / "common").glob("*.py")):
    compile(source.read_text(encoding="utf-8"), str(source), "exec")
PY
) || workflow_die "private Orichum Python failed module validation"
preflight_orichum_python_runtime \
  "$orichum_python_candidate" "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" || \
  workflow_die "private Orichum Python failed recovery-proxy preflight"
activate_orichum_python \
  "$WORKFLOW_DATA_ROOT" "$orichum_python_candidate" || \
  workflow_die "private Orichum Python could not be activated"
ORICHUM_PYTHON="$(resolve_orichum_python "$WORKFLOW_DATA_ROOT")"
export ORICHUM_PYTHON
ORICHUM_PYTHON_VALIDATED="$ORICHUM_PYTHON"
export ORICHUM_PYTHON_VALIDATED
export ORICHUM_INSTALL_BOOTSTRAP=false

for control_file in \
    model-stacks.json projects.json providers.json plugins.json runtime.json \
    controller-policy.md; do
  if [[ ! -e "$ORICHUM_CONFIG_ROOT/$control_file" ]]; then
    install -m 0600 "$WORKFLOW_ROOT/config/$control_file" \
      "$ORICHUM_CONFIG_ROOT/$control_file"
  fi
done
if [[ ! -e "$ORICHUM_CONFIG_ROOT/accounts.json" ]]; then
  printf '{"schemaVersion":2,"accounts":[]}\n' \
    >"$ORICHUM_CONFIG_ROOT/accounts.json"
  chmod 0600 "$ORICHUM_CONFIG_ROOT/accounts.json"
fi
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
  "$WORKFLOW_ROOT/bin/orichum" config validate >/dev/null || \
  workflow_die "installed Orichum control plane is invalid"
(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B - \
    "$WORKFLOW_DATA_ROOT" "$ORICHUM_CONFIG_ROOT/projects.json" <<'PY'
import json
import sys
from pathlib import Path

from integrations.common.github_identity import ensure_github_identity

data_home = Path(sys.argv[1])
projects = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
accounts = {
    context.get("githubAccount")
    for context in projects["contexts"]
    if context.get("githubAccount") is not None
}
for account in sorted(accounts):
    ensure_github_identity(data_home, account)
PY
) || workflow_die \
  "one or more project GitHub identities could not be isolated; verify gh auth"

management_key_file="$WORKFLOW_DATA_ROOT/cliproxy-management.key"
if [[ ! -e "$management_key_file" ]]; then
  umask 077
  "$ORICHUM_PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))' \
    >"$management_key_file"
  chmod 0600 "$management_key_file"
fi
[[ -f "$management_key_file" && ! -L "$management_key_file" ]] || \
  workflow_die "CLIProxyAPI management key is unsafe"
"$ORICHUM_PYTHON" - "$management_key_file" <<'PY' || \
  workflow_die "CLIProxyAPI management key is unsafe"
import os
import stat
import sys

observed = os.stat(sys.argv[1], follow_symlinks=False)
if observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) != 0o600:
    raise SystemExit(1)
PY
management_key="$(tr -d '\r\n' <"$management_key_file")"
if (( ${#management_key} < 32 || ${#management_key} > 256 )) || \
   [[ ! "$management_key" =~ ^[A-Za-z0-9._~-]+$ ]]; then
  workflow_die "CLIProxyAPI management key is invalid"
fi
ln -sfn "$WORKFLOW_ROOT/bin/orichum-route-proxy" \
  "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy"

UV_TOOL_DIR="$WORKFLOW_DATA_ROOT/headroom/tools"
UV_TOOL_BIN_DIR="$WORKFLOW_DATA_ROOT/headroom/bin"

migrate_legacy_model_config "$WORKFLOW_DATA_ROOT"
find "$WORKFLOW_DATA_ROOT/auth" -maxdepth 1 -type f -exec chmod 0600 {} \;
chmod 0755 "$WORKFLOW_ROOT/controller/plugin/scripts/"*.sh

export PATH="$UV_TOOL_BIN_DIR:$HOME/.local/bin:$PATH"
headroom_prior_version=
headroom_prior_binary="$UV_TOOL_BIN_DIR/headroom"
if [[ -x "$headroom_prior_binary" ]]; then
  headroom_prior_version="$(headroom_distribution_version "$headroom_prior_binary")" || \
    workflow_die "installed Headroom distribution version could not be read"
fi

cliproxy_state="$(stage_latest_github_binary \
  router-for-me/CLIProxyAPI 'CLIProxyAPI_' "_${cliproxy_os}_${cliproxy_arch}.tar.gz" \
  cli-proxy-api "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$installer_temp/cliproxy")"
cliproxy_tag="$(jq -r '.tag' <<<"$cliproxy_state")"
cliproxy_version="$(jq -r '.version' <<<"$cliproxy_state")"
headroom_models_registry="$installer_temp/cliproxy-models.registry.json"
desired_headroom_models="$installer_temp/headroom-models.json"
curl --fail --location --silent --show-error \
  "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI/$cliproxy_tag/internal/registry/models/models.json" \
  --output "$headroom_models_registry"
(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B \
    -m integrations.common.headroom_models generate \
    --registry "$headroom_models_registry" \
    --repository router-for-me/CLIProxyAPI \
    --tag "$cliproxy_tag" \
    --version "$cliproxy_version" \
    --output "$desired_headroom_models"
) || workflow_die "exact-release Headroom model metadata could not be generated"
claudex_state="$(stage_latest_github_binary \
  StringKe/claudex 'claudex-v' "-${claudex_arch}-${claudex_os}.tar.gz" \
  claudex "$WORKFLOW_DATA_ROOT/bin/claudex" "$installer_temp/claudex")"
claudex_version="$(jq -r '.version' <<<"$claudex_state")"
cliproxy_binary_changed="$(jq -r '.changed' <<<"$cliproxy_state")"
claudex_binary_changed="$(jq -r '.changed' <<<"$claudex_state")"
headroom_models_file="$WORKFLOW_DATA_ROOT/headroom/config/models.json"

desired_cliproxy_config="$installer_temp/cliproxy.yaml"

if [[ "$platform" == darwin ]]; then
  service_file="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  desired_service_file="$installer_temp/$SERVICE_LABEL.plist"
  headroom_service_file="$HOME/Library/LaunchAgents/io.orichum.headroom.plist"
  headroom_desired_service_file="$installer_temp/io.orichum.headroom.plist"
  previous_headroom_service_file="$HOME/Library/LaunchAgents/com.user.claudex-headroom.plist"
  oldest_headroom_service_file="$HOME/Library/LaunchAgents/com.user.headroom-proxy.plist"
  service_mode=0644
  headroom_service_mode=0600
  claudex_proxy_service_mode=0644
  cliproxy_service_label="$SERVICE_LABEL"
  cliproxy_service_unit=-
  headroom_service_label=io.orichum.headroom
  headroom_service_unit=-
  previous_headroom_service_label=com.user.claudex-headroom
  oldest_headroom_service_label=com.user.headroom-proxy
  legacy_headroom_service_unit=-
else
  service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service"
  desired_service_file="$installer_temp/orichum-cliproxy.service"
  headroom_service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-headroom.service"
  headroom_desired_service_file="$installer_temp/orichum-headroom.service"
  previous_headroom_service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claudex-headroom.service"
  oldest_headroom_service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/headroom-proxy.service"
  service_mode=0600
  headroom_service_mode=0600
  claudex_proxy_service_mode=0600
  cliproxy_service_label=-
  cliproxy_service_unit=orichum-cliproxy.service
  headroom_service_label=-
  headroom_service_unit=orichum-headroom.service
  legacy_headroom_service_label=-
  previous_headroom_service_unit=claudex-headroom.service
  oldest_headroom_service_unit=headroom-proxy.service
fi
if [[ -e "$previous_headroom_service_file" || \
      -L "$previous_headroom_service_file" ]]; then
  legacy_headroom_service_file="$previous_headroom_service_file"
  if [[ "$platform" == darwin ]]; then
    legacy_headroom_service_label="$previous_headroom_service_label"
    legacy_headroom_service_unit=-
  else
    legacy_headroom_service_label=-
    legacy_headroom_service_unit="$previous_headroom_service_unit"
  fi
else
  legacy_headroom_service_file="$oldest_headroom_service_file"
  if [[ "$platform" == darwin ]]; then
    legacy_headroom_service_label="$oldest_headroom_service_label"
    legacy_headroom_service_unit=-
  else
    legacy_headroom_service_label=-
    legacy_headroom_service_unit="$oldest_headroom_service_unit"
  fi
fi
if ! IFS=$'\t' read -r \
    claudex_proxy_service_file claudex_proxy_service_label \
    claudex_proxy_service_unit \
    < <(claudex_proxy_service_identity "$platform"); then
  workflow_die "Orichum route proxy service identity could not be resolved"
fi
if [[ "$platform" == darwin ]]; then
  claudex_proxy_desired_service_file="$installer_temp/io.orichum.route-proxy.plist"
else
  claudex_proxy_desired_service_file="$installer_temp/orichum-route-proxy.service"
fi
install -d -m 0755 \
  "$(dirname "$service_file")" \
  "$(dirname "$headroom_service_file")" \
  "$(dirname "$claudex_proxy_service_file")"

cliproxy_service_was_present=false
cliproxy_service_owned=false
if [[ -e "$service_file" || -L "$service_file" ]]; then
  cliproxy_service_was_present=true
  cliproxy_service_is_owned "$service_file" "$WORKFLOW_DATA_ROOT" || \
    workflow_die "refusing to overwrite unknown service file: $service_file"
  cliproxy_service_owned=true
fi
headroom_service_was_present=false
headroom_service_owned=false
if [[ -e "$headroom_service_file" || -L "$headroom_service_file" ]]; then
  headroom_service_was_present=true
  headroom_service_is_owned \
    "$headroom_service_file" "$WORKFLOW_DATA_ROOT" new || \
    workflow_die "refusing to overwrite unknown service file: $headroom_service_file"
  headroom_service_owned=true
fi
legacy_headroom_service_owned=false
if [[ -e "$legacy_headroom_service_file" || -L "$legacy_headroom_service_file" ]]; then
  if headroom_service_is_owned \
      "$legacy_headroom_service_file" "$WORKFLOW_DATA_ROOT" legacy; then
    legacy_headroom_service_owned=true
  else
    printf 'NOTICE: leaving unrelated Headroom service untouched: %s\n' \
      "$legacy_headroom_service_file" >&2
  fi
fi
claudex_proxy_service_was_present=false
claudex_proxy_service_owned=false
if [[ -e "$claudex_proxy_service_file" || \
      -L "$claudex_proxy_service_file" ]]; then
  claudex_proxy_service_was_present=true
  claudex_proxy_service_is_owned \
    "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" || \
    workflow_die \
      "refusing to overwrite unknown service file: $claudex_proxy_service_file"
  claudex_proxy_service_owned=true
fi
claudex_proxy_manager_target_state="$(managed_service_target_state \
  "$platform" "$claudex_proxy_service_label" \
  "$claudex_proxy_service_unit")" || workflow_die \
  "Orichum route proxy manager target could not be inspected safely"
if [[ "$claudex_proxy_manager_target_state" == loaded ]]; then
  claudex_proxy_loaded_definition="$(managed_service_definition_path \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null || true)"
  if [[ "$claudex_proxy_service_owned" != true ]] || \
     [[ "$claudex_proxy_loaded_definition" != \
        "$claudex_proxy_service_file" ]]; then
    workflow_die \
      "refusing to replace loaded unknown Orichum route proxy target"
  fi
fi

if ! IFS=$'\t' read -r \
    CLIPROXY_PORT HEADROOM_PORT PERSISTED_CLAUDEX_PROXY_PORT \
    PERSISTED_ROUTE_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  workflow_die "service port configuration is invalid"
fi
PRIOR_CLIPROXY_PORT="$CLIPROXY_PORT"
PRIOR_HEADROOM_PORT="$HEADROOM_PORT"
PRIOR_ROUTE_PROXY_PORT="$PERSISTED_ROUTE_PROXY_PORT"
CLIPROXY_PORT="${ORICHUM_CLIPROXY_PORT:-$CLIPROXY_PORT}"
HEADROOM_PORT="${ORICHUM_HEADROOM_PORT:-$HEADROOM_PORT}"
CLAUDEX_PROXY_PORT="${ORICHUM_CLAUDEX_PROXY_PORT:-$PERSISTED_CLAUDEX_PROXY_PORT}"
ROUTE_PROXY_LISTEN_PORT="${ORICHUM_ROUTE_PROXY_PORT:-$PERSISTED_ROUTE_PROXY_PORT}"
valid_service_port "$CLIPROXY_PORT" || workflow_die "invalid CLIProxyAPI port"
valid_service_port "$HEADROOM_PORT" || workflow_die "invalid Headroom port"
valid_service_port "$CLAUDEX_PROXY_PORT" || \
  workflow_die "invalid Claudex proxy port"
valid_service_port "$ROUTE_PROXY_LISTEN_PORT" || \
  workflow_die "invalid Orichum route proxy port"
[[ "$CLIPROXY_PORT" != "$HEADROOM_PORT" && \
   "$CLIPROXY_PORT" != "$CLAUDEX_PROXY_PORT" && \
   "$CLIPROXY_PORT" != "$ROUTE_PROXY_LISTEN_PORT" && \
   "$HEADROOM_PORT" != "$CLAUDEX_PROXY_PORT" && \
   "$HEADROOM_PORT" != "$ROUTE_PROXY_LISTEN_PORT" && \
   "$CLAUDEX_PROXY_PORT" != "$ROUTE_PROXY_LISTEN_PORT" ]] || \
  workflow_die \
    "CLIProxyAPI, Headroom, Claudex, and route proxy ports must differ"

cliproxy_endpoint_ready_at() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$1/v1/models" 2>/dev/null | \
    cliproxy_models_response_is_ready /dev/stdin
}

headroom_endpoint_ready_at() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$1/health" 2>/dev/null | jq -e '
      .service == "headroom-proxy" and .status == "healthy" and .ready == true
    ' >/dev/null 2>&1
}

claudex_proxy_endpoint_ready_at() {
  local port="$1"
  local expected_model="$2"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/v1/models" 2>/dev/null | \
    claudex_proxy_models_response_is_ready /dev/stdin "$expected_model"
}

claudex_proxy_health_is_ready_at() {
  local port="$1"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/health" 2>/dev/null | \
    jq -e '
      .service == "orichum-route-proxy" and
      .ready == true
    ' >/dev/null 2>&1
}

claudex_proxy_runtime_is_owned() {
  local port="$1"
  local expected_model="$2"
  local service_pid
  service_pid="$(managed_service_main_pid \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  claudex_proxy_health_is_ready_at "$port" || return 1
  claudex_proxy_endpoint_ready_at "$port" "$expected_model"
}

wait_for_claudex_proxy() {
  local port="$1"
  local expected_model="$2"
  for _ in {1..30}; do
    if claudex_proxy_runtime_is_owned "$port" "$expected_model"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

claudex_proxy_loaded_target_is_expected() {
  local loaded_definition target_state
  claudex_proxy_service_is_owned \
    "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" || return 1
  target_state="$(managed_service_target_state \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  if [[ "$target_state" == loaded ]]; then
    loaded_definition="$(managed_service_definition_path \
      "$platform" "$claudex_proxy_service_label" \
      "$claudex_proxy_service_unit" 2>/dev/null)" || return 1
    [[ "$loaded_definition" == "$claudex_proxy_service_file" ]] || return 1
  fi
}

claudex_proxy_prior_runtime_safe_to_stop() {
  local current_pid target_state
  target_state="$(managed_service_target_state \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit")" || return 1
  claudex_proxy_loaded_target_is_expected || return 1
  [[ "$target_state" == absent ]] && return 0
  current_pid="$(managed_service_main_pid_value \
    "$platform" "$claudex_proxy_service_label" \
    "$claudex_proxy_service_unit" 2>/dev/null)" || return 1
  [[ "$current_pid" == 0 ]] && return 0
  pid_owns_loopback_listener \
    "$current_pid" "$PRIOR_ROUTE_PROXY_PORT"
}

managed_target_matches_definition_or_absent() {
  local service_file="$1"
  local service_label="$2"
  local service_unit="$3"
  local target_state loaded_definition
  target_state="$(managed_service_target_state \
    "$platform" "$service_label" "$service_unit")" || return 1
  [[ "$target_state" == absent ]] && return 0
  loaded_definition="$(managed_service_definition_path \
    "$platform" "$service_label" "$service_unit" 2>/dev/null)" || return 1
  [[ "$loaded_definition" == "$service_file" ]]
}

managed_listener_is_owned() {
  local service_file="$1"
  local service_label="$2"
  local service_unit="$3"
  local port="$4"
  local service_pid
  managed_service_target_is_loaded \
    "$platform" "$service_label" "$service_unit" || return 1
  [[ "$(managed_service_definition_path \
    "$platform" "$service_label" "$service_unit" 2>/dev/null)" == \
    "$service_file" ]] || return 1
  service_pid="$(managed_service_main_pid \
    "$platform" "$service_label" "$service_unit")" || return 1
  pid_owns_loopback_listener "$service_pid" "$port"
}

managed_target_matches_definition_or_absent \
  "$service_file" "$cliproxy_service_label" "$cliproxy_service_unit" || \
  workflow_die "refusing to replace loaded unknown CLIProxyAPI target"
managed_target_matches_definition_or_absent \
  "$headroom_service_file" "$headroom_service_label" \
  "$headroom_service_unit" || \
  workflow_die "refusing to replace loaded unknown Headroom target"
if [[ "$legacy_headroom_service_owned" == true ]]; then
  managed_target_matches_definition_or_absent \
    "$legacy_headroom_service_file" "$legacy_headroom_service_label" \
    "$legacy_headroom_service_unit" || \
    workflow_die "refusing to stop ownership-drifted legacy Headroom target"
fi

cliproxy_listener_owned=false
if [[ "$CLIPROXY_PORT" == "$PRIOR_CLIPROXY_PORT" ]] && \
   [[ "$cliproxy_service_owned" == true ]] && \
   managed_listener_is_owned \
     "$service_file" "$cliproxy_service_label" "$cliproxy_service_unit" \
     "$CLIPROXY_PORT"; then
  cliproxy_listener_owned=true
fi
headroom_listener_owned=false
if [[ "$HEADROOM_PORT" == "$PRIOR_HEADROOM_PORT" ]]; then
  if [[ "$headroom_service_owned" == true ]] && \
     managed_listener_is_owned \
       "$headroom_service_file" "$headroom_service_label" \
       "$headroom_service_unit" "$HEADROOM_PORT"; then
    headroom_listener_owned=true
  elif [[ "$legacy_headroom_service_owned" == true ]] && \
       managed_listener_is_owned \
         "$legacy_headroom_service_file" "$legacy_headroom_service_label" \
         "$legacy_headroom_service_unit" "$HEADROOM_PORT"; then
    headroom_listener_owned=true
  fi
fi
prior_claudex_config="$(model_config_file \
  "$WORKFLOW_DATA_ROOT" claudex.toml)"
prior_controller_model=
if [[ -f "$prior_claudex_config" ]]; then
  prior_controller_model="$(claudex_config_default_model \
    "$prior_claudex_config" 2>/dev/null || true)"
fi
claudex_proxy_listener_owned=false
claudex_proxy_port_owned=false
claudex_proxy_prior_manager_pid=
if [[ "$claudex_proxy_service_owned" == true ]]; then
  if [[ "$claudex_proxy_manager_target_state" == loaded ]]; then
    claudex_proxy_prior_manager_pid="$(managed_service_main_pid_value \
      "$platform" "$claudex_proxy_service_label" \
      "$claudex_proxy_service_unit" 2>/dev/null)" || workflow_die \
      "Orichum route proxy manager PID could not be inspected safely"
  fi
  if [[ "$claudex_proxy_prior_manager_pid" =~ ^[1-9][0-9]*$ ]]; then
    if pid_owns_loopback_listener \
        "$claudex_proxy_prior_manager_pid" \
        "$PRIOR_ROUTE_PROXY_PORT"; then
      if [[ "$ROUTE_PROXY_LISTEN_PORT" == \
            "$PRIOR_ROUTE_PROXY_PORT" ]]; then
        claudex_proxy_port_owned=true
      fi
      if [[ "$claudex_proxy_port_owned" == true ]] && \
         [[ -n "$prior_controller_model" ]] && \
         claudex_proxy_endpoint_ready_at \
           "$PRIOR_ROUTE_PROXY_PORT" "$prior_controller_model"; then
        claudex_proxy_listener_owned=true
      fi
    else
      workflow_die \
        "refusing to stop ownership-drifted Orichum route proxy runtime PID $claudex_proxy_prior_manager_pid"
    fi
  fi
fi
legacy_headroom_running_version=
if [[ "$legacy_headroom_service_owned" == true ]] && \
   headroom_endpoint_ready_at "$PRIOR_HEADROOM_PORT"; then
  legacy_headroom_running_version="$(curl -fsS \
    "http://127.0.0.1:$PRIOR_HEADROOM_PORT/health" 2>/dev/null | \
    jq -r '.version // empty' || true)"
fi
interactive_install=false
if [[ -t 0 && -t 1 ]]; then
  interactive_install=true
fi
CLIPROXY_PORT="$(select_service_port \
  CLIProxyAPI ORICHUM_CLIPROXY_PORT "$CLIPROXY_PORT" \
  "$cliproxy_listener_owned" "$interactive_install")" || exit 1
HEADROOM_PORT="$(select_service_port \
  Headroom ORICHUM_HEADROOM_PORT "$HEADROOM_PORT" \
  "$headroom_listener_owned" "$interactive_install" \
  "$CLIPROXY_PORT")" || exit 1
ROUTE_PROXY_LISTEN_PORT="$(select_service_port \
  'Orichum route proxy' ORICHUM_ROUTE_PROXY_PORT "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_port_owned" "$interactive_install" \
  "$CLIPROXY_PORT" "$HEADROOM_PORT" "$CLAUDEX_PROXY_PORT")" || exit 1
ports_changed=false
if [[ "$CLIPROXY_PORT" != "$PRIOR_CLIPROXY_PORT" ]] || \
   [[ "$HEADROOM_PORT" != "$PRIOR_HEADROOM_PORT" ]] || \
   [[ "$CLAUDEX_PROXY_PORT" != "$PERSISTED_CLAUDEX_PROXY_PORT" ]] || \
   [[ "$ROUTE_PROXY_LISTEN_PORT" != "$PRIOR_ROUTE_PROXY_PORT" ]]; then
  ports_changed=true
fi
service_ports_path="$(service_ports_file "$WORKFLOW_DATA_ROOT")"

configured_management_secret="$management_key"
if [[ -f "$WORKFLOW_DATA_ROOT/cliproxy.yaml" && \
      ! -L "$WORKFLOW_DATA_ROOT/cliproxy.yaml" ]]; then
  observed_management_secret="$(sed -n \
    's/^[[:space:]]*secret-key:[[:space:]]*"\([^"]*\)"[[:space:]]*$/\1/p' \
    "$WORKFLOW_DATA_ROOT/cliproxy.yaml" | head -1)"
  if [[ "$observed_management_secret" =~ ^\$2[a-z]\$[0-9]{2}\$.{53}$ ]]; then
    configured_management_secret="$observed_management_secret"
  fi
fi
render_cliproxy_config \
  "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/auth" "$CLIPROXY_PORT" \
  "$configured_management_secret"
chmod 0600 "$desired_cliproxy_config"

probe_cliproxy_management() (
  local probe_root probe_port probe_pid= probe_binary
  probe_root="$installer_temp/cliproxy-management-probe"
  probe_port="$(next_available_port \
    "$CLIPROXY_PORT" "$HEADROOM_PORT" "$CLAUDEX_PROXY_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT")" || \
    return 1
  install -d -m 0700 "$probe_root/auth"
  printf '{"type":"codex","disabled":true}\n' \
    >"$probe_root/auth/orichum-capability-probe.json"
  chmod 0600 "$probe_root/auth/orichum-capability-probe.json"
  render_cliproxy_config \
    "$probe_root/config.yaml" "$probe_root/auth" "$probe_port" \
    "$management_key"
  chmod 0600 "$probe_root/config.yaml"
  probe_binary="$(jq -r '.staged_path' <<<"$cliproxy_state")"
  if [[ "$probe_binary" == null ]]; then
    probe_binary="$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
  fi
  cleanup_management_probe() {
    if [[ -n "$probe_pid" ]] && kill -0 "$probe_pid" 2>/dev/null; then
      kill "$probe_pid" 2>/dev/null || true
      wait "$probe_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_management_probe EXIT
  "$probe_binary" --config "$probe_root/config.yaml" \
    >"$probe_root/probe.log" 2>&1 &
  probe_pid=$!
  "$ORICHUM_PYTHON" - "$probe_port" "$management_key" \
    "$probe_root/auth/orichum-capability-probe.json" <<'PY'
import http.client
import json
from pathlib import Path
import sys
import time

port = int(sys.argv[1])
key = sys.argv[2]
credential = Path(sys.argv[3])
headers = {"X-Management-Key": key}
deadline = time.monotonic() + 15
while True:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        connection.request(
            "GET", "/v0/management/auth-files", headers=headers
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        if response.status == 200:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit("management API did not become ready")
    time.sleep(0.1)

payload = json.dumps(
    {
        "name": credential.name,
        "prefix": "orichum-capability",
        "priority": 7,
    },
    separators=(",", ":"),
).encode()
connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
connection.request(
    "PATCH",
    "/v0/management/auth-files/fields",
    body=payload,
    headers={
        **headers,
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    },
)
response = connection.getresponse()
response.read()
connection.close()
if response.status != 200:
    raise SystemExit(f"management PATCH returned {response.status}")
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    document = json.loads(credential.read_text(encoding="utf-8"))
    if (
        document.get("prefix") == "orichum-capability"
        and document.get("priority") == 7
    ):
        raise SystemExit(0)
    time.sleep(0.05)
raise SystemExit("management PATCH did not persist exact fields")
PY
)
probe_cliproxy_management || workflow_die \
  "CLIProxyAPI failed the required management PATCH/readback capability probe"
route_proxy_runtime_digest="$(
  "$ORICHUM_PYTHON" -I -B - \
    "$orichum_python_candidate" "$orichum_python_version" \
    "$WORKFLOW_ROOT/integrations/common" <<'PY'
import hashlib
from pathlib import Path
import sys

digest = hashlib.sha256()
for value in sys.argv[1:3]:
    digest.update(value.encode("utf-8"))
    digest.update(b"\0")
root = Path(sys.argv[3])
for path in sorted(root.glob("*.py")):
    digest.update(path.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
)" || workflow_die "Orichum route runtime could not be fingerprinted"
[[ "$route_proxy_runtime_digest" =~ ^[a-f0-9]{64}$ ]] || \
  workflow_die "Orichum route runtime fingerprint is invalid"
if [[ "$platform" == darwin ]]; then
  render_launch_agent "$desired_service_file" "$WORKFLOW_DATA_ROOT"
  plutil -lint "$desired_service_file" >/dev/null
  render_claudex_proxy_launch_agent \
    "$claudex_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$CLIPROXY_PORT" \
    "$route_proxy_runtime_digest"
  plutil -lint "$claudex_proxy_desired_service_file" >/dev/null
else
  render_systemd_user_unit "$desired_service_file" "$WORKFLOW_DATA_ROOT"
  render_claudex_proxy_systemd_user_unit \
    "$claudex_proxy_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT" \
    "$ROUTE_PROXY_LISTEN_PORT" "$CLIPROXY_PORT" \
    "$route_proxy_runtime_digest"
fi

cliproxy_config_changed="$(file_change_state \
  "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/cliproxy.yaml")"
cliproxy_service_changed="$(file_change_state "$desired_service_file" "$service_file")"
claudex_proxy_service_changed="$(file_change_state \
  "$claudex_proxy_desired_service_file" "$claudex_proxy_service_file")"
headroom_models_changed="$(private_file_change_state \
  "$desired_headroom_models" "$headroom_models_file" 600)"
claudex_proxy_port_changed=false
if [[ "$ROUTE_PROXY_LISTEN_PORT" != "$PRIOR_ROUTE_PROXY_PORT" ]]; then
  claudex_proxy_port_changed=true
fi

cliproxy_is_ready() {
  local port="${1:-$CLIPROXY_PORT}"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/v1/models" 2>/dev/null | \
    cliproxy_models_response_is_ready /dev/stdin
}

wait_for_cliproxy() {
  local port="${1:-$CLIPROXY_PORT}"
  for _ in {1..30}; do
    if cliproxy_is_ready "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

cliproxy_ready_before=true
cliproxy_is_ready || cliproxy_ready_before=false
cliproxy_restart_required=false
if [[ "$cliproxy_binary_changed" == true ]] || \
   [[ "$cliproxy_config_changed" == changed ]] || \
   [[ "$cliproxy_service_changed" == changed ]] || \
   [[ "$cliproxy_ready_before" == false ]]; then
  cliproxy_restart_required=true
fi

model_config_root_path="$(model_config_root "$WORKFLOW_DATA_ROOT")"
prior_model_generation=
prior_model_generation_snapshot=
endpoint_lock_owned=false
endpoint_lock_token=
snapshot_path "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$snapshot_dir" cliproxy-binary
snapshot_path "$WORKFLOW_DATA_ROOT/bin/claudex" "$snapshot_dir" claudex-binary
snapshot_path "$WORKFLOW_DATA_ROOT/cliproxy.yaml" "$snapshot_dir" cliproxy-config
snapshot_path "$service_file" "$snapshot_dir" cliproxy-service
snapshot_path "$headroom_service_file" "$snapshot_dir" headroom-service
snapshot_path "$headroom_models_file" "$snapshot_dir" headroom-models
snapshot_path "$claudex_proxy_service_file" \
  "$snapshot_dir" claudex-proxy-service
snapshot_path "$service_ports_path" "$snapshot_dir" service-ports
snapshot_path "$USER_BIN_DIR/orichum" \
  "$snapshot_dir" orichum-launcher
snapshot_private_tool_state \
  "$WORKFLOW_DATA_ROOT" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
  "$snapshot_dir/private-tools"
if [[ "$legacy_headroom_service_owned" == true ]]; then
  snapshot_path "$legacy_headroom_service_file" \
    "$snapshot_dir" legacy-headroom-service
fi

cliproxy_transaction_active=false
headroom_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=true
orichum_launcher_mutated=false
private_tools_transaction_active=false
legacy_headroom_stopped=false
headroom_health_is_ready() {
  local expected_version="$1"
  local port="${2:-$HEADROOM_PORT}"
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$port/health" 2>/dev/null | \
    jq -e --arg version "$expected_version" \
    '.service == "headroom-proxy" and .status == "healthy" and
     .ready == true and ($version == "" or .version == $version)' \
    >/dev/null 2>&1
}

wait_for_headroom_health() {
  local expected_version="$1"
  local port="${2:-$HEADROOM_PORT}"
  for _ in {1..30}; do
    if headroom_health_is_ready "$expected_version" "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_headroom_service() {
  local recovery_ready=true

  [[ "$headroom_transaction_active" == true ]] || return 0
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$headroom_service_file" \
      >/dev/null 2>&1 || true
  else
    systemctl --user stop orichum-headroom.service >/dev/null 2>&1 || true
  fi

  restore_snapshot "$headroom_models_file" \
    "$snapshot_dir" headroom-models || recovery_ready=false
  snapshot_path_matches "$headroom_models_file" \
    "$snapshot_dir" headroom-models || recovery_ready=false
  restore_snapshot "$headroom_service_file" \
    "$snapshot_dir" headroom-service || recovery_ready=false
  snapshot_path_matches "$headroom_service_file" \
    "$snapshot_dir" headroom-service || recovery_ready=false
  if [[ "$legacy_headroom_service_owned" == true ]]; then
    restore_snapshot "$legacy_headroom_service_file" \
      "$snapshot_dir" legacy-headroom-service || recovery_ready=false
    snapshot_path_matches "$legacy_headroom_service_file" \
      "$snapshot_dir" legacy-headroom-service || recovery_ready=false
  fi

  if [[ -n "$headroom_prior_version" ]]; then
    printf 'WARNING: Headroom rollback restores only top-level version %s; dependency versions may differ.\n' \
      "$headroom_prior_version" >&2
    if ! restore_headroom_distribution \
        "$headroom_prior_version" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"; then
      recovery_ready=false
    fi
  elif ! UV_TOOL_DIR="$UV_TOOL_DIR" UV_TOOL_BIN_DIR="$UV_TOOL_BIN_DIR" \
      uv tool uninstall headroom-ai >/dev/null 2>&1; then
    recovery_ready=false
  fi

  if [[ -f "$snapshot_dir/headroom-service.present" ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootstrap "gui/$(id -u)" "$headroom_service_file" \
        >/dev/null 2>&1 || recovery_ready=false
    else
      systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
      systemctl --user enable orichum-headroom.service >/dev/null 2>&1 || \
        recovery_ready=false
      systemctl --user restart orichum-headroom.service >/dev/null 2>&1 || \
        recovery_ready=false
    fi
    wait_for_headroom_health \
      "$headroom_prior_version" "$PRIOR_HEADROOM_PORT" || recovery_ready=false
  elif [[ "$legacy_headroom_stopped" == true ]] && \
       [[ -f "$snapshot_dir/legacy-headroom-service.present" ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootstrap "gui/$(id -u)" "$legacy_headroom_service_file" \
        >/dev/null 2>&1 || recovery_ready=false
    else
      systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
      systemctl --user enable "$legacy_headroom_service_unit" >/dev/null 2>&1 || \
        recovery_ready=false
      systemctl --user restart "$legacy_headroom_service_unit" >/dev/null 2>&1 || \
        recovery_ready=false
    fi
    wait_for_headroom_health \
      "$legacy_headroom_running_version" "$PRIOR_HEADROOM_PORT" || \
      recovery_ready=false
  elif [[ "$platform" == systemd ]]; then
    systemctl --user disable orichum-headroom.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
  fi

  [[ "$recovery_ready" == true ]]
}

restore_claudex_proxy_service() {
  local recovery_ready=true restored_model
  [[ "$claudex_proxy_transaction_active" == true ]] || return 0

  restore_snapshot "$claudex_proxy_service_file" \
    "$snapshot_dir" claudex-proxy-service || recovery_ready=false
  snapshot_path_matches "$claudex_proxy_service_file" \
    "$snapshot_dir" claudex-proxy-service || recovery_ready=false

  if [[ "$recovery_ready" != true ]] || \
     [[ "${claudex_proxy_recovery_prerequisites_ready:-false}" != true ]]; then
    return 1
  fi

  if [[ -f "$snapshot_dir/claudex-proxy-service.present" ]]; then
    restored_model="$(claudex_config_default_model \
      "$(model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml)" \
      2>/dev/null || true)"
    [[ -n "$restored_model" ]] || return 1
    if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]; then
      claudex_proxy_loaded_target_is_expected || return 1
      if [[ "$platform" == darwin ]]; then
        launchctl enable \
          "gui/$(id -u)/$claudex_proxy_service_label" \
          >/dev/null 2>&1 || recovery_ready=false
        if [[ "$recovery_ready" == true ]]; then
          launchctl bootstrap "gui/$(id -u)" \
            "$claudex_proxy_service_file" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
      else
        systemctl --user daemon-reload \
          >/dev/null 2>&1 || recovery_ready=false
        if [[ "$recovery_ready" == true ]]; then
          claudex_proxy_loaded_target_is_expected || recovery_ready=false
        fi
        if [[ "$recovery_ready" == true ]]; then
          systemctl --user enable "$claudex_proxy_service_unit" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
        if [[ "$recovery_ready" == true ]]; then
          systemctl --user restart "$claudex_proxy_service_unit" \
            >/dev/null 2>&1 || recovery_ready=false
        fi
      fi
    fi
    if [[ "$recovery_ready" == true ]]; then
      wait_for_claudex_proxy \
        "$PRIOR_ROUTE_PROXY_PORT" "$restored_model" || recovery_ready=false
    fi
  elif [[ "$platform" == systemd ]]; then
    systemctl --user disable "$claudex_proxy_service_unit" \
      >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
  fi

  [[ "$recovery_ready" == true ]]
}

rollback_install_transaction() {
  local rollback_ready=true

  if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]; then
    if claudex_proxy_loaded_target_is_expected; then
      if [[ "$platform" == darwin ]]; then
        launchctl bootout "gui/$(id -u)" "$claudex_proxy_service_file" \
          >/dev/null 2>&1 || true
      else
        systemctl --user stop "$claudex_proxy_service_unit" \
          >/dev/null 2>&1 || true
      fi
    else
      rollback_ready=false
    fi
  fi

  if [[ "${python_transaction_active:-false}" == true ]]; then
    rollback_python_activation || rollback_ready=false
  fi

  if [[ "${private_tools_transaction_active:-false}" == true ]]; then
    restore_private_tool_state \
      "$WORKFLOW_DATA_ROOT" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
      "$snapshot_dir/private-tools" || rollback_ready=false
    private_tool_state_matches \
      "$WORKFLOW_DATA_ROOT" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
      "$snapshot_dir/private-tools" || rollback_ready=false
  fi

  if [[ "$cliproxy_transaction_active" == true ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
    else
      systemctl --user stop orichum-cliproxy.service >/dev/null 2>&1 || true
    fi

    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
      "$snapshot_dir" cliproxy-binary || rollback_ready=false
    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/claudex" \
      "$snapshot_dir" claudex-binary || rollback_ready=false
    restore_snapshot "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
      "$snapshot_dir" cliproxy-config || rollback_ready=false
    restore_snapshot "$service_file" \
      "$snapshot_dir" cliproxy-service || rollback_ready=false

    if [[ -f "$snapshot_dir/cliproxy-service.present" ]]; then
      if [[ "$platform" == darwin ]]; then
        launchctl bootstrap "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || \
          rollback_ready=false
      else
        systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
        systemctl --user enable orichum-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
        systemctl --user restart orichum-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
      fi
      wait_for_cliproxy "$PRIOR_CLIPROXY_PORT" || rollback_ready=false
    elif [[ "$platform" == systemd ]]; then
      systemctl --user disable orichum-cliproxy.service >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
    fi
  fi

  if [[ "${endpoint_transaction_active:-false}" == true ]]; then
    if [[ "${endpoint_lock_owned:-false}" == true ]]; then
      restore_model_config_generation \
        "$WORKFLOW_DATA_ROOT" "$prior_model_generation" \
        "$prior_model_generation_snapshot" || rollback_ready=false
    fi
    restore_snapshot "$service_ports_path" \
      "$snapshot_dir" service-ports || rollback_ready=false
    snapshot_path_matches "$service_ports_path" \
      "$snapshot_dir" service-ports || rollback_ready=false
  fi

  claudex_proxy_recovery_prerequisites_ready="$rollback_ready"
  run_rollback_if_active \
    "${claudex_proxy_transaction_active:-false}" \
    restore_claudex_proxy_service || rollback_ready=false

  run_rollback_if_active "${headroom_transaction_active:-false}" \
    restore_headroom_service || rollback_ready=false

  if [[ "${endpoint_lock_owned:-false}" == true ]]; then
    release_endpoint_config_lock \
      "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || rollback_ready=false
    endpoint_lock_owned=false
  fi

  if [[ "${orichum_launcher_mutated:-false}" == true ]]; then
    restore_snapshot "$USER_BIN_DIR/orichum" \
      "$snapshot_dir" orichum-launcher || rollback_ready=false
    snapshot_path_matches "$USER_BIN_DIR/orichum" \
      "$snapshot_dir" orichum-launcher || rollback_ready=false
  fi

  [[ "$rollback_ready" == true ]]
}

WORKFLOW_ROLLBACK_HANDLER=rollback_install_transaction
WORKFLOW_TRANSACTION_ACTIVE=true

endpoint_lock_token="$$:$RANDOM:$RANDOM"
acquire_endpoint_config_lock \
  "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || \
  workflow_die "could not serialize endpoint model publication"
endpoint_lock_owned=true
prior_model_generation="$(readlink \
  "$model_config_root_path/current" 2>/dev/null || true)"
if [[ -n "$prior_model_generation" ]]; then
  prior_model_generation_snapshot="$snapshot_dir/prior-model-generation"
  cp -pPR "$model_config_root_path/$prior_model_generation" \
    "$prior_model_generation_snapshot" || \
    workflow_die "prior model configuration could not be snapshotted"
fi

private_tools_transaction_active=true
uv tool install --upgrade mempalace
if ! mempalace_mcp="$(command -v mempalace-mcp)"; then
  workflow_die "Mempalace installation did not provide mempalace-mcp"
fi
install -d -m 0700 "$installer_temp/mempalace-probe"
if ! PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B \
  "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
  --require-tool mempalace_get_taxonomy \
  --require-tool mempalace_search \
  --require-tool mempalace_checkpoint \
  -- "$mempalace_mcp" --palace "$installer_temp/mempalace-probe"; then
  workflow_die "Mempalace MCP failed protocol readiness checks"
fi

uv tool install --upgrade 'graphifyy[mcp,terraform]'
if ! graphify_mcp="$(command -v graphify-mcp)"; then
  workflow_die "Graphify installation did not provide graphify-mcp"
fi
graphify_probe_graph="$installer_temp/graphify-probe.json"
jq -n '{
  directed: false, multigraph: false, graph: {},
  nodes: [{id: "claudex-audit", label: "claudex-audit"}], links: []
}' >"$graphify_probe_graph"
if ! PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B \
  "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
  --require-tool query_graph \
  --require-tool graph_stats \
  -- "$graphify_mcp" --graph "$graphify_probe_graph"; then
  workflow_die "Graphify MCP failed protocol readiness checks"
fi
upgrade_headroom_distribution "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR"
headroom_binary="$UV_TOOL_BIN_DIR/headroom"
if [[ ! -x "$headroom_binary" ]] || ! "$headroom_binary" --version >/dev/null; then
  workflow_die "Headroom installation did not provide a working headroom --version"
fi
headroom_current_version="$(headroom_distribution_version "$headroom_binary")" || \
  workflow_die "Headroom distribution version could not be read"
headroom_version_changed=false
if distribution_version_changed "$headroom_prior_version" \
  "$headroom_current_version"; then
  headroom_version_changed=true
fi

headroom_python="$(sed -n '1s/^#!//p' "$headroom_binary")"
[[ -x "$headroom_python" ]] || workflow_die "Headroom Python runtime could not be resolved"
headroom_ca_bundle="$($headroom_python -c 'import certifi; print(certifi.where())')"
[[ -f "$headroom_ca_bundle" ]] || workflow_die "Headroom CA bundle could not be resolved"

headroom_is_ready() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$HEADROOM_PORT/health" 2>/dev/null | jq -e \
    --arg version "$headroom_current_version" \
    '.service == "headroom-proxy" and .status == "healthy" and
     .ready == true and .version == $version and .config.optimize == true and
     .config.cache == false and .config.memory == false and
     .config.code_graph == false and
     .config.disable_kompress == true and
     .checks.kompress.enabled == false and .checks.kompress.ready == true and
     .config.runtime_env.HEADROOM_OUTPUT_SHAPER == "0" and
     .config.runtime_env.HEADROOM_VERBOSITY_AUTOTUNE == "0" and
     .config.runtime_env.HEADROOM_EFFORT_ROUTER == "0"' >/dev/null 2>&1
}

headroom_service_is_ready() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$HEADROOM_PORT/health" 2>/dev/null | jq -e \
    --arg version "$headroom_current_version" \
    '.service == "headroom-proxy" and .version == $version and
     .checks.startup.ready == true and .checks.http_client.ready == true and
     .config.optimize == true and .config.cache == false and
     .config.memory == false and .config.code_graph == false and
     .config.disable_kompress == true and
     .checks.kompress.enabled == false and .checks.kompress.ready == true and
     .config.runtime_env.HEADROOM_OUTPUT_SHAPER == "0" and
     .config.runtime_env.HEADROOM_VERBOSITY_AUTOTUNE == "0" and
     .config.runtime_env.HEADROOM_EFFORT_ROUTER == "0"' >/dev/null 2>&1
}

preflight_headroom_binary() (
  local preflight_port preflight_pid= preflight_ready=false
  local preflight_root="$installer_temp/headroom-preflight"
  preflight_port="$(next_available_port \
    "$HEADROOM_PORT" "$CLIPROXY_PORT")" || return 1
  install -d -m 0700 "$preflight_root/config" "$preflight_root/state"
  install -m 0600 "$desired_headroom_models" \
    "$preflight_root/config/models.json"
  cleanup_preflight() {
    if [[ -n "$preflight_pid" ]] && kill -0 "$preflight_pid" 2>/dev/null; then
      kill "$preflight_pid" 2>/dev/null || true
      wait "$preflight_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  HEADROOM_CONFIG_DIR="$preflight_root/config" \
  HEADROOM_WORKSPACE_DIR="$preflight_root/state" \
  SSL_CERT_FILE="$headroom_ca_bundle" \
  HEADROOM_CACHE_ENABLED=0 \
  HEADROOM_MEMORY_ENABLED=0 \
  HEADROOM_OUTPUT_SHAPER=0 \
  HEADROOM_VERBOSITY_AUTOTUNE=0 \
  HEADROOM_EFFORT_ROUTER=0 \
  HEADROOM_LOG_MESSAGES=0 \
    "$headroom_binary" proxy \
      --host 127.0.0.1 --port "$preflight_port" --mode token \
      --no-cache --intercept-tool-results --lossless --code-aware \
      --disable-kompress \
      >"$installer_temp/headroom-preflight.log" 2>&1 &
  preflight_pid=$!
  for _ in {1..90}; do
    kill -0 "$preflight_pid" 2>/dev/null || break
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/health" 2>/dev/null | \
        jq -e --arg version "$headroom_current_version" '
          .service == "headroom-proxy" and .status == "healthy" and
          .ready == true and .version == $version
        ' >/dev/null 2>&1; then
      preflight_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$preflight_ready" != true ]]; then
    sed -n '1,160p' "$installer_temp/headroom-preflight.log" >&2 || true
    return 1
  fi
)

preflight_claudex_proxy() (
  local preflight_port preflight_pid= preflight_ready=false
  local response_file="$installer_temp/claudex-proxy-preflight-models.json"
  preflight_port="$(next_available_port \
    "$ROUTE_PROXY_LISTEN_PORT" "$CLAUDEX_PROXY_PORT" \
    "$CLIPROXY_PORT" "$HEADROOM_PORT")" || \
    return 1
  cleanup_claudex_preflight() {
    if [[ -n "$preflight_pid" ]] && \
       kill -0 "$preflight_pid" 2>/dev/null; then
      kill "$preflight_pid" 2>/dev/null || true
      wait "$preflight_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_claudex_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  "$WORKFLOW_DATA_ROOT/bin/orichum-route-proxy" \
    --port "$preflight_port" \
    --upstream-port "$CLIPROXY_PORT" \
    --state-home "$WORKFLOW_DATA_ROOT/state" \
    --data-home "$WORKFLOW_DATA_ROOT" \
    >"$installer_temp/route-proxy-preflight.log" 2>&1 &
  preflight_pid=$!
  for _ in {1..30}; do
    kill -0 "$preflight_pid" 2>/dev/null || break
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/v1/models" \
        >"$response_file" 2>/dev/null && \
       claudex_proxy_models_response_is_ready \
         "$response_file" "$active_controller_model"; then
      preflight_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$preflight_ready" != true ]]; then
    sed -n '1,160p' "$installer_temp/route-proxy-preflight.log" \
      >&2 || true
    return 1
  fi
)

preflight_claudex_translation_proxy() (
  local config_file="$1"
  local probe_home="$installer_temp/claudex-translation-home"
  local preflight_port preflight_pid='' preflight_ready=false
  local response_file="$installer_temp/claudex-translation-models.json"
  install -d -m 0700 \
    "$probe_home" "$probe_home/cache" "$probe_home/runtime"
  preflight_port="$(next_available_port \
    "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" \
    "$CLIPROXY_PORT" "$HEADROOM_PORT")" || \
    return 1
  cleanup_claudex_translation_preflight() {
    if [[ -n "$preflight_pid" ]] && \
       kill -0 "$preflight_pid" 2>/dev/null; then
      kill "$preflight_pid" 2>/dev/null || true
      wait "$preflight_pid" 2>/dev/null || true
    fi
  }
  trap cleanup_claudex_translation_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  HOME="$probe_home" \
  XDG_CACHE_HOME="$probe_home/cache" \
  XDG_RUNTIME_DIR="$probe_home/runtime" \
    "$WORKFLOW_DATA_ROOT/bin/claudex" \
      --config "$config_file" proxy start --port "$preflight_port" \
      >"$installer_temp/claudex-translation-preflight.log" 2>&1 &
  preflight_pid=$!
  for _ in {1..30}; do
    kill -0 "$preflight_pid" 2>/dev/null || break
    if curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/health" 2>/dev/null | \
        rg -Fxq ok && \
       curl -fsS --connect-timeout 1 --max-time 2 \
        "http://127.0.0.1:$preflight_port/v1/models" \
        >"$response_file" 2>/dev/null && \
       claudex_proxy_models_response_is_ready \
         "$response_file" "$active_controller_model"; then
      preflight_ready=true
      break
    fi
    sleep 1
  done
  if [[ "$preflight_ready" != true ]]; then
    sed -n '1,160p' \
      "$installer_temp/claudex-translation-preflight.log" >&2 || true
    return 1
  fi
)

require_activation_port_available() {
  local service_name="$1"
  local port="$2"
  local activation_port_ready=false
  for _ in {1..150}; do
    if port_is_available "$port"; then
      activation_port_ready=true
      break
    fi
    sleep 0.1
  done
  [[ "$activation_port_ready" == true ]] || workflow_die \
    "$service_name activation port $port remained occupied; prior state will be restored"
}

if [[ "$platform" == darwin ]]; then
  render_headroom_launch_agent \
    "$headroom_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$headroom_binary" "$headroom_ca_bundle" "$HEADROOM_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT"
  plutil -lint "$headroom_desired_service_file" >/dev/null
else
  render_headroom_systemd_user_unit \
    "$headroom_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$headroom_binary" "$headroom_ca_bundle" "$HEADROOM_PORT" \
    "$ROUTE_PROXY_LISTEN_PORT"
fi

headroom_service_changed="$(file_change_state \
  "$headroom_desired_service_file" "$headroom_service_file")"
headroom_health_ok=true
headroom_service_is_ready || headroom_health_ok=false
if [[ "$legacy_headroom_service_owned" == true ]]; then
  headroom_health_ok=false
fi
reconcile_headroom_transaction "$headroom_version_changed" \
  "$headroom_service_changed" "$headroom_health_ok" \
  "$headroom_models_changed"

if [[ "$headroom_restart_required" == true ]]; then
  preflight_headroom_binary || workflow_die \
    "private Headroom failed isolated preflight; the existing service was left running"
  if [[ "$headroom_models_changed" == changed ]]; then
    activate_private_file_atomic \
      "$desired_headroom_models" "$headroom_models_file" 0600
    [[ "$(private_file_change_state \
      "$desired_headroom_models" "$headroom_models_file" 600)" == unchanged ]] || \
      workflow_die "Headroom model metadata was not activated safely"
  fi
  printf 'WARNING: restarting a changed or unhealthy service may interrupt active Claudex sessions.\n' >&2
  if [[ "$headroom_service_changed" == changed ]]; then
    activate_staged_file "$headroom_desired_service_file" \
      "$headroom_service_file" "$headroom_service_mode"
  fi
  if [[ "$legacy_headroom_service_owned" == true ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootout "gui/$(id -u)" "$legacy_headroom_service_file" \
        >/dev/null 2>&1 || true
    else
      systemctl --user stop "$legacy_headroom_service_unit" >/dev/null 2>&1 || true
    fi
    legacy_headroom_stopped=true
  fi
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$headroom_service_file" >/dev/null 2>&1 || true
  else
    systemctl --user stop orichum-headroom.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload
    systemctl --user enable orichum-headroom.service
  fi
  require_activation_port_available Headroom "$HEADROOM_PORT"
  if [[ "$platform" == darwin ]]; then
    launchctl bootstrap "gui/$(id -u)" "$headroom_service_file"
    launchctl enable "gui/$(id -u)/io.orichum.headroom"
  else
    systemctl --user start orichum-headroom.service
  fi
  headroom_ready=false
  for _ in {1..30}; do
    if headroom_service_is_ready; then headroom_ready=true; break; fi
    sleep 1
  done
  [[ "$headroom_ready" == true ]] || workflow_die \
    "Headroom failed health checks; prior service definition and top-level version recovery will be attempted"
  if [[ "$legacy_headroom_service_owned" == true ]]; then
    if [[ "$platform" == systemd ]]; then
      systemctl --user disable "$legacy_headroom_service_unit" >/dev/null 2>&1 || true
    fi
    rm -f -- "$legacy_headroom_service_file"
    if [[ "$platform" == systemd ]]; then
      systemctl --user daemon-reload
    fi
  fi
fi

if [[ "$cliproxy_binary_changed" == true ]] || \
   [[ "$claudex_binary_changed" == true ]] || \
   [[ "$cliproxy_config_changed" == changed ]] || \
   [[ "$cliproxy_service_changed" == changed ]] || \
   [[ "$cliproxy_restart_required" == true ]]; then
  cliproxy_transaction_active=true
fi
if [[ "$cliproxy_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$cliproxy_state")" \
    "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" 0755
fi
if [[ "$claudex_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$claudex_state")" \
    "$WORKFLOW_DATA_ROOT/bin/claudex" 0755
fi
if [[ "$cliproxy_config_changed" == changed ]]; then
  activate_staged_file "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/cliproxy.yaml" 0600
fi
if [[ "$cliproxy_service_changed" == changed ]]; then
  activate_staged_file "$desired_service_file" "$service_file" "$service_mode"
fi

if [[ "$cliproxy_restart_required" == true ]]; then
  printf 'WARNING: restarting a changed or unhealthy service may interrupt active Claudex sessions.\n' >&2
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
  else
    systemctl --user stop orichum-cliproxy.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload
    systemctl --user enable orichum-cliproxy.service
  fi
  require_activation_port_available CLIProxyAPI "$CLIPROXY_PORT"
  if [[ "$platform" == darwin ]]; then
    launchctl bootstrap "gui/$(id -u)" "$service_file"
  else
    systemctl --user start orichum-cliproxy.service
  fi
  wait_for_cliproxy || workflow_die \
    "CLIProxyAPI failed readiness checks; previous service will be restored"
fi

for launcher in orichum; do
  ln -sfn "$WORKFLOW_ROOT/bin/$launcher" "$USER_BIN_DIR/$launcher"
  orichum_launcher_mutated=true
done

write_service_ports "$WORKFLOW_DATA_ROOT" \
  "$CLIPROXY_PORT" "$HEADROOM_PORT" "$CLAUDEX_PROXY_PORT" \
  "$ROUTE_PROXY_LISTEN_PORT" || \
  workflow_die "service port configuration could not be saved"

source "$WORKFLOW_ROOT/discover-models.sh"
model_discovery_succeeded=true
discovery_entrypoint=discover_models_main_core
model_discovery_status=0
CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint" || \
  model_discovery_status=$?
if [[ "$model_discovery_status" -ne 0 ]]; then
  model_discovery_succeeded=false
  if [[ -z "$prior_model_generation" ]] && \
     [[ "$model_discovery_status" -eq \
        "$MODEL_DISCOVERY_LOGIN_INCOMPLETE" ]]; then
    printf 'NOTICE: persistent Orichum route proxy is pending-provider-login.\n' >&2
    printf 'Next: orichum provider login <provider>; %s/install.sh\n' \
      "$WORKFLOW_ROOT" >&2
  elif [[ -n "$prior_model_generation" ]] && \
       [[ "$ports_changed" == false ]] && \
       [[ "$claudex_binary_changed" == false ]] && \
       [[ "$claudex_proxy_service_changed" == unchanged ]] && \
       [[ "$claudex_proxy_listener_owned" == true ]]; then
    printf 'WARNING: model discovery failed; unchanged healthy proxy state was retained.\n' >&2
  else
    workflow_die \
      "model discovery failed while persistent proxy reconciliation was required"
  fi
fi

claudex_proxy_action=pending-provider-login
claudex_proxy_readiness_drifted=false
if [[ "$model_discovery_succeeded" == true || \
      -n "$prior_model_generation" ]]; then
  active_claudex_config="$(model_config_file \
    "$WORKFLOW_DATA_ROOT" claudex.toml)"
  active_controller_model="$(claudex_config_default_model \
    "$active_claudex_config")" || \
    workflow_die "active Claudex controller model could not be resolved"
  preflight_claudex_translation_proxy "$active_claudex_config" || \
    workflow_die \
      "Claudex translation proxy failed isolated bind and catalogue preflight"

  if [[ "$claudex_proxy_service_owned" != true ]] || \
     ! claudex_proxy_runtime_is_owned \
       "$ROUTE_PROXY_LISTEN_PORT" "$active_controller_model"; then
    claudex_proxy_readiness_drifted=true
  fi

  claudex_proxy_restart_required=false
  if [[ "$claudex_proxy_service_changed" == changed ]] || \
     [[ "$claudex_proxy_port_changed" == true ]] || \
     [[ "$claudex_proxy_readiness_drifted" == true ]]; then
    claudex_proxy_restart_required=true
  fi

  if [[ "$claudex_proxy_restart_required" == true ]]; then
    preflight_claudex_proxy || workflow_die \
      "Orichum recovery proxy failed isolated preflight; the existing service was left running"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_prior_runtime_safe_to_stop || workflow_die \
        "refusing to stop ownership-drifted Orichum route proxy runtime"
    fi
    claudex_proxy_transaction_active=true
    printf 'WARNING: restarting the shared Orichum route proxy may interrupt one in-flight request across active sessions.\n' >&2
    if [[ "$claudex_proxy_service_changed" == changed ]]; then
      activate_staged_file "$claudex_proxy_desired_service_file" \
        "$claudex_proxy_service_file" "$claudex_proxy_service_mode"
    fi
    claudex_proxy_service_is_owned \
      "$claudex_proxy_service_file" "$WORKFLOW_DATA_ROOT" \
      "$WORKFLOW_ROOT" || \
      workflow_die "installed Orichum route proxy service definition is not owned"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_runtime_mutated=true
      if [[ "$platform" == darwin ]]; then
        launchctl bootout \
          "gui/$(id -u)/$claudex_proxy_service_label" \
          >/dev/null 2>&1 || true
      else
        systemctl --user stop "$claudex_proxy_service_unit" \
          >/dev/null 2>&1 || true
      fi
    fi
    activation_port_ready=false
    for _ in {1..150}; do
      if port_is_available "$ROUTE_PROXY_LISTEN_PORT"; then
        activation_port_ready=true
        break
      fi
      sleep 0.1
    done
    [[ "$activation_port_ready" == true ]] || workflow_die \
      "Orichum route proxy activation port $ROUTE_PROXY_LISTEN_PORT is occupied; prior state will be restored"
    if [[ "$platform" == darwin ]]; then
      claudex_proxy_loaded_target_is_expected || workflow_die \
        "Orichum route proxy definition ownership drifted before start"
      claudex_proxy_runtime_mutated=true
      launchctl enable \
        "gui/$(id -u)/$claudex_proxy_service_label"
      launchctl bootstrap \
        "gui/$(id -u)" "$claudex_proxy_service_file"
    else
      systemctl --user daemon-reload
      claudex_proxy_loaded_target_is_expected || workflow_die \
        "Orichum route proxy definition ownership drifted before start"
      systemctl --user enable "$claudex_proxy_service_unit"
      claudex_proxy_runtime_mutated=true
      systemctl --user start "$claudex_proxy_service_unit"
    fi
    wait_for_claudex_proxy \
      "$ROUTE_PROXY_LISTEN_PORT" "$active_controller_model" || \
      workflow_die \
        "Orichum route proxy failed ownership or readiness checks; previous state will be restored"
    if [[ "$claudex_proxy_service_was_present" == true ]]; then
      claudex_proxy_action=reconciled
    else
      claudex_proxy_action=installed
    fi
  else
    claudex_proxy_action=reused
  fi
fi

if [[ "$claudex_proxy_action" != pending-provider-login ]]; then
  headroom_ready=false
  for _ in {1..30}; do
    if headroom_is_ready; then headroom_ready=true; break; fi
    sleep 1
  done
  [[ "$headroom_ready" == true ]] || workflow_die \
    "Headroom did not become fully ready after route proxy activation"
fi

if [[ "$endpoint_lock_owned" == true ]]; then
  release_endpoint_config_lock \
    "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || \
    workflow_die "endpoint model publication lock could not be released"
  endpoint_lock_owned=false
fi
cliproxy_transaction_active=false
headroom_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=false
private_tools_transaction_active=false
python_transaction_active=false
WORKFLOW_TRANSACTION_ACTIVE=false
install -m 0600 "$WORKFLOW_ROOT/controller/settings.json" \
  "$WORKFLOW_DATA_ROOT/claude-config/settings.json"
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
  "$WORKFLOW_ROOT/bin/orichum-plugin" sync || \
  workflow_die \
    "services are healthy, but declared Claude plugins could not be synchronized; rerun the installer after correcting the plugin error"
if [[ "$model_discovery_succeeded" == true ]]; then
  prune_model_config_generations "$WORKFLOW_DATA_ROOT" || \
    printf 'WARNING: stale model configuration could not be pruned.\n' >&2
fi
cliproxy_action=reused
if [[ "$cliproxy_restart_required" == true ]]; then
  if [[ "$cliproxy_service_was_present" == true ]]; then
    cliproxy_action=reconciled
  else
    cliproxy_action=installed
  fi
fi
headroom_action=reused
if [[ "$headroom_restart_required" == true ]]; then
  if [[ "$legacy_headroom_stopped" == true ]]; then
    headroom_action=migrated
  elif [[ "$headroom_service_was_present" == true ]]; then
    headroom_action=reconciled
  else
    headroom_action=installed
  fi
fi
printf 'Installed Orichum with Claudex %s and CLIProxyAPI %s for %s.\n' \
  "$claudex_version" "$cliproxy_version" "$platform"
print_install_summary \
  "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" "$USER_BIN_DIR" \
  "$WORKFLOW_DATA_ROOT/bin/claudex" \
  "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$headroom_binary" \
  "$mempalace_mcp" "$graphify_mcp" "$service_file" \
  "$headroom_service_file" "$CLIPROXY_PORT" "$HEADROOM_PORT" \
  "$cliproxy_action" "$headroom_action" \
  "$claudex_proxy_service_file" "$CLAUDEX_PROXY_PORT" \
  "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_action" \
  "$ORICHUM_PYTHON" "$orichum_python_version" \
  "$orichum_python_candidate" "$orichum_python_action"
if [[ "$claudex_proxy_action" == pending-provider-login ]]; then
  printf 'Next: orichum provider login <provider>; %s/install.sh\n' \
    "$WORKFLOW_ROOT"
else
  printf '\nRunning Orichum doctor...\n'
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$USER_BIN_DIR/orichum" doctor
fi
