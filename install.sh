#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"

USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"
SERVICE_LABEL="com.user.claudex-cliproxy"

for command_name in curl jq tar install python3 git rg uv; do
  command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
done
command -v claude >/dev/null || workflow_die "Claude Code is not installed or not on PATH"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || \
  workflow_die "Python 3.10 or newer is required"

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

validation_config="$(mktemp -d "${TMPDIR:-/tmp}/claudex-plugin.XXXXXX")"
chmod 0700 "$validation_config"
trap 'rm -rf -- "$validation_config"' EXIT
CLAUDE_CONFIG_DIR="$validation_config" \
  claude plugin validate --strict "$WORKFLOW_ROOT/controller/plugin" >/dev/null || \
  workflow_die "controller plugin validation failed"
rm -rf -- "$validation_config"
trap - EXIT

install -d -m 0755 "$WORKFLOW_ROOT/bin" "$WORKFLOW_ROOT/runtime" "$WORKFLOW_ROOT/logs" "$USER_BIN_DIR"
install -d -m 0700 \
  "$WORKFLOW_ROOT/runtime/auth" \
  "$WORKFLOW_ROOT/runtime/claude-config" \
  "$WORKFLOW_ROOT/runtime/state" \
  "$WORKFLOW_ROOT/runtime/state/sessions" \
  "$WORKFLOW_ROOT/backups"
find "$WORKFLOW_ROOT/runtime/auth" -maxdepth 1 -type f -exec chmod 0600 {} \;
install -m 0600 "$WORKFLOW_ROOT/controller/settings.json" \
  "$WORKFLOW_ROOT/runtime/claude-config/settings.json"
chmod 0755 "$WORKFLOW_ROOT/controller/plugin/scripts/"*.sh

export PATH="$HOME/.local/bin:$PATH"
if ! command -v mempalace-mcp >/dev/null 2>&1; then
  uv tool install --upgrade mempalace
fi
if ! command -v graphify-mcp >/dev/null 2>&1; then
  uv tool install --upgrade graphifyy
fi

cliproxy_version="$(install_latest_github_binary \
  router-for-me/CLIProxyAPI 'CLIProxyAPI_' "_${cliproxy_os}_${cliproxy_arch}.tar.gz" \
  cli-proxy-api "$WORKFLOW_ROOT/bin/cli-proxy-api")"
claudex_version="$(install_latest_github_binary \
  StringKe/claudex 'claudex-v' "-${claudex_arch}-${claudex_os}.tar.gz" \
  claudex "$WORKFLOW_ROOT/bin/claudex")"

render_cliproxy_config "$WORKFLOW_ROOT/runtime/cliproxy.yaml.new" \
  "$WORKFLOW_ROOT/runtime/auth"
chmod 0600 "$WORKFLOW_ROOT/runtime/cliproxy.yaml.new"
mv "$WORKFLOW_ROOT/runtime/cliproxy.yaml.new" "$WORKFLOW_ROOT/runtime/cliproxy.yaml"

if [[ ! -f "$WORKFLOW_ROOT/runtime/claudex.toml" ]]; then
  render_claudex_config "$WORKFLOW_ROOT/runtime/claudex.toml.new" \
    gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol \
    claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8 \
    "$(command -v claude)"
  chmod 0600 "$WORKFLOW_ROOT/runtime/claudex.toml.new"
  mv "$WORKFLOW_ROOT/runtime/claudex.toml.new" "$WORKFLOW_ROOT/runtime/claudex.toml"
fi

if [[ "$platform" == darwin ]]; then
  service_file="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  install -d -m 0755 "$(dirname "$service_file")"
  render_launch_agent "$WORKFLOW_ROOT/runtime/$SERVICE_LABEL.plist" "$WORKFLOW_ROOT"
  plutil -lint "$WORKFLOW_ROOT/runtime/$SERVICE_LABEL.plist" >/dev/null
  install -m 0644 "$WORKFLOW_ROOT/runtime/$SERVICE_LABEL.plist" "$service_file"
  launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$service_file"
  launchctl enable "gui/$(id -u)/$SERVICE_LABEL"
else
  service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claudex-cliproxy.service"
  install -d -m 0755 "$(dirname "$service_file")"
  render_systemd_user_unit "$service_file.new" "$WORKFLOW_ROOT"
  install -m 0600 "$service_file.new" "$service_file"
  rm -f "$service_file.new"
  systemctl --user daemon-reload
  systemctl --user enable --now claudex-cliproxy.service
fi

for launcher in claudex-gpt claude-headroom claudex-login claudex-doctor; do
  ln -sfn "$WORKFLOW_ROOT/bin/$launcher" "$USER_BIN_DIR/$launcher"
done

headroom_health="$(mktemp "${TMPDIR:-/tmp}/headroom-health.XXXXXX")"
trap 'rm -f -- "$headroom_health"' EXIT
if curl -fsS --connect-timeout 1 --max-time 2 \
  http://127.0.0.1:8787/health >"$headroom_health"; then
  jq -e '.service == "headroom-proxy" and .status == "healthy" and .ready == true' \
    "$headroom_health" >/dev/null || workflow_die "port 8787 is not a compatible Headroom service"
else
  if ! command -v headroom >/dev/null 2>&1; then
    uv tool install --upgrade 'headroom-ai[all]'
  fi
  command -v headroom >/dev/null || workflow_die "Headroom installation did not provide a headroom command"
  headroom install apply \
    --preset persistent-service --runtime python --scope user \
    --providers manual --profile claudex-workflow --port 8787 --mode token \
    --no-telemetry --intercept-tool-results \
    --env "HEADROOM_CONFIG_DIR=$WORKFLOW_ROOT/runtime/headroom/config" \
    --env "HEADROOM_WORKSPACE_DIR=$WORKFLOW_ROOT/runtime/headroom/state" \
    --env HEADROOM_LOSSLESS=1 --env HEADROOM_CACHE_ENABLED=0
fi
rm -f -- "$headroom_health"
trap - EXIT

printf 'Installed Claudex %s and CLIProxyAPI %s for %s.\n' \
  "$claudex_version" "$cliproxy_version" "$platform"
printf 'Next: claudex-login codex; claudex-login claude; %s/discover-models.sh\n' \
  "$WORKFLOW_ROOT"
