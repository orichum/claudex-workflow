#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"

USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"
WORKFLOW_DATA_ROOT="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || \
  workflow_die "refusing unsafe CLAUDEX_DATA_DIR"
LEGACY_RUNTIME_ROOT="$WORKFLOW_ROOT/runtime"
SERVICE_LABEL="com.user.claudex-cliproxy"

workflow_cleanup_init
trap 'workflow_cleanup "$?"' EXIT
trap 'workflow_cleanup 129' HUP
trap 'workflow_cleanup 130' INT
trap 'workflow_cleanup 143' TERM

for command_name in curl jq tar install python3 git rg uv; do
  command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
done
command -v claude >/dev/null || workflow_die "Claude Code is not installed or not on PATH"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
  workflow_die "Python 3.10 or newer is required"

install -d -m 0700 "$WORKFLOW_DATA_ROOT" "$WORKFLOW_DATA_ROOT/state"
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
    if rg -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null && \
       ! rg -qi 'wsl2|microsoft-standard' /proc/sys/kernel/osrelease 2>/dev/null; then
      workflow_die "WSL1 is unsupported; use WSL2 with systemd enabled"
    fi
    systemctl --user show-environment >/dev/null 2>&1 || \
      workflow_die "a working systemd user manager is required"
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
  for command_name in launchctl plutil; do
    command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
  done
fi

(
  cd "$WORKFLOW_ROOT"
  PYTHONDONTWRITEBYTECODE=1 python3 -B -m integrations.common.project_context \
    validate-config --config "$WORKFLOW_ROOT/controller/project-context.json"
) || workflow_die "controller/project-context.json is invalid"

while IFS= read -r configured_palace; do
  case "$configured_palace" in
    "~/"*) resolved_palace="$HOME/${configured_palace#\~/}" ;;
    /*) resolved_palace="$configured_palace" ;;
    *) workflow_die "memoryPalace must be absolute or use ~/ syntax" ;;
  esac
  install -d -m 0700 "$resolved_palace"
done < <(jq -er '.contexts[].memoryPalace' "$WORKFLOW_ROOT/controller/project-context.json")

validation_config="$(mktemp -d "${TMPDIR:-/tmp}/claudex-plugin.XXXXXX")"
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
  "$WORKFLOW_DATA_ROOT/headroom/config" \
  "$WORKFLOW_DATA_ROOT/headroom/state"

legacy_marker="$WORKFLOW_DATA_ROOT/.legacy-repo-state-migrated"
if [[ ! -e "$legacy_marker" ]]; then
  if [[ -d "$LEGACY_RUNTIME_ROOT/auth" ]]; then
    while IFS= read -r legacy_auth; do
      auth_name="$(basename "$legacy_auth")"
      [[ -e "$WORKFLOW_DATA_ROOT/auth/$auth_name" ]] || \
        install -m 0600 "$legacy_auth" "$WORKFLOW_DATA_ROOT/auth/$auth_name"
    done < <(find "$LEGACY_RUNTIME_ROOT/auth" -maxdepth 1 -type f)
  fi
  if [[ -d "$LEGACY_RUNTIME_ROOT/claude-config" ]] && \
     [[ -z "$(find "$WORKFLOW_DATA_ROOT/claude-config" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    cp -pPR "$LEGACY_RUNTIME_ROOT/claude-config/." "$WORKFLOW_DATA_ROOT/claude-config/"
  fi
  for legacy_name in claudex.toml models.json; do
    if [[ -f "$LEGACY_RUNTIME_ROOT/$legacy_name" ]] && \
       [[ ! -e "$WORKFLOW_DATA_ROOT/$legacy_name" ]]; then
      install -m 0600 "$LEGACY_RUNTIME_ROOT/$legacy_name" "$WORKFLOW_DATA_ROOT/$legacy_name"
    fi
  done
  install -m 0600 /dev/null "$legacy_marker"
fi
migrate_legacy_model_config "$WORKFLOW_DATA_ROOT"
find "$WORKFLOW_DATA_ROOT/auth" -maxdepth 1 -type f -exec chmod 0600 {} \;
install -m 0600 "$WORKFLOW_ROOT/controller/settings.json" \
  "$WORKFLOW_DATA_ROOT/claude-config/settings.json"
chmod 0755 "$WORKFLOW_ROOT/controller/plugin/scripts/"*.sh

export PATH="$HOME/.local/bin:$PATH"
headroom_prior_version=
if headroom_prior_binary="$(command -v headroom 2>/dev/null)"; then
  headroom_prior_version="$(headroom_distribution_version "$headroom_prior_binary")" || \
    workflow_die "installed Headroom distribution version could not be read"
fi

installer_temp="$(mktemp -d "${TMPDIR:-/tmp}/claudex-install.XXXXXX")"
register_cleanup_path "$installer_temp"
cliproxy_state="$(stage_latest_github_binary \
  router-for-me/CLIProxyAPI 'CLIProxyAPI_' "_${cliproxy_os}_${cliproxy_arch}.tar.gz" \
  cli-proxy-api "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$installer_temp/cliproxy")"
claudex_state="$(stage_latest_github_binary \
  StringKe/claudex 'claudex-v' "-${claudex_arch}-${claudex_os}.tar.gz" \
  claudex "$WORKFLOW_DATA_ROOT/bin/claudex" "$installer_temp/claudex")"
cliproxy_version="$(jq -r '.version' <<<"$cliproxy_state")"
claudex_version="$(jq -r '.version' <<<"$claudex_state")"
cliproxy_binary_changed="$(jq -r '.changed' <<<"$cliproxy_state")"
claudex_binary_changed="$(jq -r '.changed' <<<"$claudex_state")"

desired_cliproxy_config="$installer_temp/cliproxy.yaml"
render_cliproxy_config "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/auth"
chmod 0600 "$desired_cliproxy_config"

if [[ "$platform" == darwin ]]; then
  service_file="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  desired_service_file="$installer_temp/$SERVICE_LABEL.plist"
  headroom_service_file="$HOME/Library/LaunchAgents/com.user.headroom-proxy.plist"
  headroom_desired_service_file="$installer_temp/com.user.headroom-proxy.plist"
  service_mode=0644
  headroom_service_mode=0600
  render_launch_agent "$desired_service_file" "$WORKFLOW_DATA_ROOT"
  plutil -lint "$desired_service_file" >/dev/null
else
  service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claudex-cliproxy.service"
  desired_service_file="$installer_temp/claudex-cliproxy.service"
  headroom_service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/headroom-proxy.service"
  headroom_desired_service_file="$installer_temp/headroom-proxy.service"
  service_mode=0600
  headroom_service_mode=0600
  render_systemd_user_unit "$desired_service_file" "$WORKFLOW_DATA_ROOT"
fi
install -d -m 0755 "$(dirname "$service_file")" "$(dirname "$headroom_service_file")"

cliproxy_config_changed="$(file_change_state \
  "$desired_cliproxy_config" "$WORKFLOW_DATA_ROOT/cliproxy.yaml")"
cliproxy_service_changed="$(file_change_state "$desired_service_file" "$service_file")"

cliproxy_is_ready() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    http://127.0.0.1:8317/v1/models | \
    cliproxy_models_response_is_ready /dev/stdin
}

wait_for_cliproxy() {
  for _ in {1..30}; do
    if cliproxy_is_ready; then
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

snapshot_dir="$installer_temp/snapshots"
snapshot_path "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" "$snapshot_dir" cliproxy-binary
snapshot_path "$WORKFLOW_DATA_ROOT/bin/claudex" "$snapshot_dir" claudex-binary
snapshot_path "$WORKFLOW_DATA_ROOT/cliproxy.yaml" "$snapshot_dir" cliproxy-config
snapshot_path "$service_file" "$snapshot_dir" cliproxy-service
snapshot_path "$headroom_service_file" "$snapshot_dir" headroom-service

cliproxy_transaction_active=false
headroom_transaction_active=false
headroom_health_is_ready() {
  local expected_version="$1"
  curl -fsS --connect-timeout 1 --max-time 2 \
    http://127.0.0.1:8787/health | jq -e --arg version "$expected_version" \
    '.service == "headroom-proxy" and .status == "healthy" and
     .ready == true and ($version == "" or .version == $version)' \
    >/dev/null 2>&1
}

wait_for_headroom_health() {
  local expected_version="$1"
  for _ in {1..30}; do
    if headroom_health_is_ready "$expected_version"; then
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
    systemctl --user stop headroom-proxy.service >/dev/null 2>&1 || true
  fi

  restore_snapshot "$headroom_service_file" \
    "$snapshot_dir" headroom-service || recovery_ready=false
  snapshot_path_matches "$headroom_service_file" \
    "$snapshot_dir" headroom-service || recovery_ready=false

  if [[ -n "$headroom_prior_version" ]]; then
    printf 'WARNING: Headroom rollback restores only top-level version %s; dependency versions may differ.\n' \
      "$headroom_prior_version" >&2
    if ! restore_headroom_distribution "$headroom_prior_version"; then
      recovery_ready=false
    fi
  fi

  if [[ -f "$snapshot_dir/headroom-service.present" ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootstrap "gui/$(id -u)" "$headroom_service_file" \
        >/dev/null 2>&1 || recovery_ready=false
    else
      systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
      systemctl --user enable headroom-proxy.service >/dev/null 2>&1 || \
        recovery_ready=false
      systemctl --user restart headroom-proxy.service >/dev/null 2>&1 || \
        recovery_ready=false
    fi
    wait_for_headroom_health "$headroom_prior_version" || recovery_ready=false
  elif [[ "$platform" == systemd ]]; then
    systemctl --user disable headroom-proxy.service >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || recovery_ready=false
  fi

  [[ "$recovery_ready" == true ]]
}

rollback_install_transaction() {
  local rollback_ready=true

  run_rollback_if_active "${headroom_transaction_active:-false}" \
    restore_headroom_service || rollback_ready=false

  if [[ "$cliproxy_transaction_active" == true ]]; then
    if [[ "$platform" == darwin ]]; then
      launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
    else
      systemctl --user stop claudex-cliproxy.service >/dev/null 2>&1 || true
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
        systemctl --user enable claudex-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
        systemctl --user restart claudex-cliproxy.service >/dev/null 2>&1 || \
          rollback_ready=false
      fi
      wait_for_cliproxy || rollback_ready=false
    elif [[ "$platform" == systemd ]]; then
      systemctl --user disable claudex-cliproxy.service >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || rollback_ready=false
    fi
  fi

  [[ "$rollback_ready" == true ]]
}

WORKFLOW_ROLLBACK_HANDLER=rollback_install_transaction
WORKFLOW_TRANSACTION_ACTIVE=true

uv tool install --upgrade mempalace
if ! command -v mempalace-mcp >/dev/null || \
   ! mempalace-mcp --version >/dev/null; then
  workflow_die "Mempalace installation did not provide a working mempalace-mcp --version"
fi
uv tool install --upgrade graphifyy
if ! command -v graphify-mcp >/dev/null || ! graphify-mcp --version >/dev/null; then
  workflow_die "Graphify installation did not provide a working graphify-mcp --version"
fi
upgrade_headroom_distribution
if ! command -v headroom >/dev/null || ! headroom --version >/dev/null; then
  workflow_die "Headroom installation did not provide a working headroom --version"
fi
headroom_current_version="$(headroom_distribution_version "$(command -v headroom)")" || \
  workflow_die "Headroom distribution version could not be read"
headroom_version_changed=false
if distribution_version_changed "$headroom_prior_version" \
  "$headroom_current_version"; then
  headroom_version_changed=true
fi

headroom_binary="$(command -v headroom)"
headroom_python="$(sed -n '1s/^#!//p' "$headroom_binary")"
[[ -x "$headroom_python" ]] || workflow_die "Headroom Python runtime could not be resolved"
headroom_ca_bundle="$($headroom_python -c 'import certifi; print(certifi.where())')"
[[ -f "$headroom_ca_bundle" ]] || workflow_die "Headroom CA bundle could not be resolved"

headroom_is_ready() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    http://127.0.0.1:8787/health | jq -e \
    --arg version "$headroom_current_version" \
    '.service == "headroom-proxy" and .status == "healthy" and
     .ready == true and .version == $version and .config.optimize == true and
     .config.cache == false and .config.memory == false and
     .config.code_graph == false and
     .config.runtime_env.HEADROOM_OUTPUT_SHAPER == "0" and
     .config.runtime_env.HEADROOM_VERBOSITY_AUTOTUNE == "0" and
     .config.runtime_env.HEADROOM_EFFORT_ROUTER == "0"' >/dev/null 2>&1
}

if [[ "$platform" == darwin ]]; then
  render_headroom_launch_agent \
    "$headroom_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$headroom_binary" "$headroom_ca_bundle"
  plutil -lint "$headroom_desired_service_file" >/dev/null
else
  render_headroom_systemd_user_unit \
    "$headroom_desired_service_file" "$WORKFLOW_DATA_ROOT" \
    "$headroom_binary" "$headroom_ca_bundle"
fi

headroom_service_changed="$(file_change_state \
  "$headroom_desired_service_file" "$headroom_service_file")"
headroom_health_ok=true
headroom_is_ready || headroom_health_ok=false
reconcile_headroom_transaction "$headroom_version_changed" \
  "$headroom_service_changed" "$headroom_health_ok"

if [[ "$headroom_restart_required" == true ]]; then
  printf 'WARNING: restarting a changed or unhealthy service may interrupt active Claudex sessions.\n' >&2
  if [[ "$headroom_service_changed" == changed ]]; then
    activate_staged_file "$headroom_desired_service_file" \
      "$headroom_service_file" "$headroom_service_mode"
  fi
  if [[ "$platform" == darwin ]]; then
    launchctl bootout "gui/$(id -u)" "$headroom_service_file" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$headroom_service_file"
    launchctl enable "gui/$(id -u)/com.user.headroom-proxy"
  else
    systemctl --user daemon-reload
    systemctl --user enable headroom-proxy.service
    systemctl --user restart headroom-proxy.service
  fi
  headroom_ready=false
  for _ in {1..30}; do
    if headroom_is_ready; then headroom_ready=true; break; fi
    sleep 1
  done
  [[ "$headroom_ready" == true ]] || workflow_die \
    "Headroom failed health checks; prior service definition and top-level version recovery will be attempted"
fi
headroom_transaction_active=false

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
    launchctl bootstrap "gui/$(id -u)" "$service_file"
  else
    systemctl --user daemon-reload
    systemctl --user enable claudex-cliproxy.service
    systemctl --user restart claudex-cliproxy.service
  fi
  wait_for_cliproxy || workflow_die \
    "CLIProxyAPI failed readiness checks; previous service will be restored"
fi

for launcher in claudex-gpt claude-headroom claudex-login claudex-doctor claudex-context; do
  ln -sfn "$WORKFLOW_ROOT/bin/$launcher" "$USER_BIN_DIR/$launcher"
done

source "$WORKFLOW_ROOT/discover-models.sh"
discover_models_main || true

cliproxy_transaction_active=false
WORKFLOW_TRANSACTION_ACTIVE=false
printf 'Installed Claudex %s and CLIProxyAPI %s for %s.\n' \
  "$claudex_version" "$cliproxy_version" "$platform"
printf 'Next: claudex-login codex; claudex-login claude; %s/discover-models.sh\n' \
  "$WORKFLOW_ROOT"
