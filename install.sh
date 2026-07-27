#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true

install_usage() {
  printf 'Usage: ./install.sh [--upgrade | --uninstall [--purge]]\n' >&2
}

INSTALL_MODE="$(parse_install_mode "$@")" || {
  install_usage
  exit 2
}
case "$INSTALL_MODE" in
  fast|upgrade) ;;
  uninstall)
    # shellcheck source=lib/uninstall.sh
    source "$WORKFLOW_ROOT/lib/uninstall.sh"
    orichum_uninstall false
    exit
    ;;
  purge)
    # shellcheck source=lib/uninstall.sh
    source "$WORKFLOW_ROOT/lib/uninstall.sh"
    orichum_uninstall true
    exit
    ;;
esac

# BEGIN installed control-plane transaction
stage_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local candidate_root="$4"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$candidate_root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import stage

stage(root, Path(sys.argv[2]), Path(sys.argv[3]))
PY
  )
}

activate_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local candidate_root="$3"
  local installed_root="$4"
  local snapshot_root="$5"
  local install_lock_fd="$6"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$candidate_root" "$installed_root" \
      "$snapshot_root" "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import activate

activate(
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    int(sys.argv[5]),
)
PY
  )
}

rollback_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local snapshot_root="$4"
  local install_lock_fd="$5"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$snapshot_root" \
      "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import rollback

rollback(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
PY
  )
}

recover_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local installed_root="$3"
  local journal_root="$4"
  local install_lock_fd="$5"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$installed_root" "$journal_root" \
      "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import recover

recover(Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
PY
  )
}

finalize_installed_control_plane() {
  local python_runtime="$1"
  local workflow_root="$2"
  local journal_root="$3"
  local install_lock_fd="$4"
  (
    cd "$workflow_root"
    PYTHONDONTWRITEBYTECODE=1 "$python_runtime" -I -B - \
      "$workflow_root" "$journal_root" "$install_lock_fd" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common.install_control_plane import finalize

finalize(Path(sys.argv[2]), int(sys.argv[3]))
PY
  )
}

verify_committed_control_plane() {
  local installed_root="$1"
  local data_root="$2"
  ORICHUM_CONFIG_HOME="$installed_root" \
  ORICHUM_DATA_HOME="$data_root" \
    "$WORKFLOW_ROOT/bin/orichum" config validate >/dev/null
}
# END installed control-plane transaction


USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"
WORKFLOW_DATA_ROOT="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || \
  workflow_die "refusing unsafe ORICHUM_DATA_HOME"
ORICHUM_CONFIG_ROOT="${ORICHUM_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/orichum}"
INSTALLED_CONFIG_ROOT="$ORICHUM_CONFIG_ROOT"
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
    orichum-context orichum-doctor orichum-login \
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
leanctx_release_asset_suffix="$(
  leanctx_release_suffix "$platform" "$claudex_arch"
)"

if [[ "$platform" == darwin ]]; then
  for command_name in launchctl plutil lsof; do
    command -v "$command_name" >/dev/null || workflow_die "missing required command: $command_name"
  done
fi

preflight_owned_headroom_installation \
  "$platform" "$WORKFLOW_DATA_ROOT" || \
  workflow_die "legacy Orichum Headroom installation is unsafe"
preflight_private_tool_layout "$WORKFLOW_DATA_ROOT" || \
  workflow_die "private Orichum tools root is unsafe"

install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT" "$WORKFLOW_DATA_ROOT/state" "$ORICHUM_CONFIG_ROOT"
acquire_workflow_lock "$WORKFLOW_DATA_ROOT/state/install.lock"
installer_temp="$(mktemp -d "${TMPDIR:-/tmp}/orichum-install.XXXXXX")"
register_cleanup_path "$installer_temp"
snapshot_dir="$installer_temp/snapshots"
install -d -m 0700 "$snapshot_dir"
install_state_path="$WORKFLOW_DATA_ROOT/state/install-state.json"
install_state_platform="$platform:$cliproxy_arch"
prior_install_state="$installer_temp/prior-install-state.json"
prior_install_state_verified=false
install_state_read_status=0
python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
  read "$install_state_path" "$install_state_platform" \
  >"$prior_install_state" 2>/dev/null || install_state_read_status=$?
if [[ "$install_state_read_status" -eq 0 ]]; then
  chmod 0600 "$prior_install_state"
  prior_install_state_verified=true
  snapshot_path "$install_state_path" "$snapshot_dir" install-state
else
  rm -f -- "$prior_install_state"
fi
control_plane_journal="$WORKFLOW_DATA_ROOT/state/install-control-plane"
recover_installed_control_plane \
  python3 "$WORKFLOW_ROOT" \
  "$INSTALLED_CONFIG_ROOT" "$control_plane_journal" \
  "$WORKFLOW_LOCK_FD" || \
  workflow_die "unfinished Orichum control-plane activation could not be recovered"

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

install_contract_fingerprint() {
  python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
    fingerprint "$WORKFLOW_ROOT" "$@"
}
python_input_sha="$(
  install_contract_fingerprint lib/workflow.sh
)" || workflow_die "Python installer input fingerprint failed"
python_probe_sha="$(
  install_contract_fingerprint \
    lib/workflow.sh integrations/common/route_proxy.py
)" || workflow_die "Python probe fingerprint failed"
cliproxy_input_sha="$(
  install_contract_fingerprint install.sh lib/workflow.sh
)" || workflow_die "CLIProxyAPI installer input fingerprint failed"
cliproxy_probe_sha="$cliproxy_input_sha"
claudex_input_sha="$cliproxy_input_sha"
claudex_probe_sha="$(
  install_contract_fingerprint \
    install.sh lib/workflow.sh integrations/common/route_proxy.py
)" || workflow_die "Claudex probe fingerprint failed"
leanctx_input_sha="$(
  install_contract_fingerprint \
    install.sh lib/workflow.sh \
    integrations/common/leanctx_contract.py \
    integrations/common/mcp_probe.py
)" || workflow_die "LeanCTX installer input fingerprint failed"
leanctx_probe_sha="$(
  install_contract_fingerprint \
    lib/workflow.sh integrations/common/leanctx_contract.py \
    integrations/common/mcp_probe.py
)" || workflow_die "LeanCTX probe fingerprint failed"
empty_artifact_sha="$(printf '0%.0s' {1..64})"
plugin_paths=()
while IFS= read -r plugin_path; do
  plugin_paths+=("$plugin_path")
done < <(
  git -C "$WORKFLOW_ROOT" ls-files \
    controller/plugin config/plugins.json | LC_ALL=C sort
)
((${#plugin_paths[@]} > 1)) || \
  workflow_die "controller plugin fingerprint inputs are missing"
controller_plugin_input_sha="$(
  install_contract_fingerprint "${plugin_paths[@]}"
)" || workflow_die "controller plugin input fingerprint failed"
controller_plugin_probe_sha="$(
  install_contract_fingerprint \
    bin/orichum-plugin controller/plugin/hooks/hooks.json
)" || workflow_die "controller plugin probe fingerprint failed"
mempalace_input_sha="$(
  printf 'pypi:mempalace\n' | sha256_text
)"
mempalace_probe_sha="$(
  printf '%s\n' \
    "$(sha256_file "$WORKFLOW_ROOT/integrations/common/mcp_probe.py")" \
    mempalace_get_taxonomy mempalace_search mempalace_checkpoint | \
    sha256_text
)"
graphify_input_sha="$(
  printf 'pypi:graphifyy[mcp,terraform]\n' | sha256_text
)"
graphify_probe_sha="$(
  printf '%s\n' \
    "$(sha256_file "$WORKFLOW_ROOT/integrations/common/mcp_probe.py")" \
    "$(sha256_file "$WORKFLOW_ROOT/lib/workflow.sh")" \
    query_graph graph_stats | sha256_text
)"
controller_plugin_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  controller_plugin_decision="$(
    decide_install_component \
      "$prior_install_state" controllerPlugin \
      1 orichum:controller-plugin \
      "$controller_plugin_input_sha" \
      "$controller_plugin_input_sha" "$controller_plugin_probe_sha"
  )"
fi

while IFS= read -r configured_palace; do
  case "$configured_palace" in
    "~/"*) resolved_palace="$HOME/${configured_palace#\~/}" ;;
    /*) resolved_palace="$configured_palace" ;;
    *) workflow_die "memoryPalace must be absolute or use ~/ syntax" ;;
  esac
  install -d -m 0700 "$resolved_palace"
done < <(jq -er '.contexts[].memoryPalace' "$WORKFLOW_ROOT/config/projects.json")

if [[ "$controller_plugin_decision" != reused ]]; then
  validation_config="$(mktemp -d "${TMPDIR:-/tmp}/orichum-plugin.XXXXXX")"
  register_cleanup_path "$validation_config"
  chmod 0700 "$validation_config"
  CLAUDE_CONFIG_DIR="$validation_config" \
    claude plugin validate --strict \
      "$WORKFLOW_ROOT/controller/plugin" >/dev/null || \
    workflow_die "controller plugin validation failed"
  rm -rf -- "$validation_config"
fi

install -d -m 0755 "$USER_BIN_DIR"
install -d -m 0700 "$WORKFLOW_DATA_ROOT"
install -d -m 0700 \
  "$WORKFLOW_DATA_ROOT/bin" \
  "$WORKFLOW_DATA_ROOT/auth" \
  "$WORKFLOW_DATA_ROOT/claude-config" \
  "$WORKFLOW_DATA_ROOT/state" \
  "$WORKFLOW_DATA_ROOT/state/sessions" \
  "$WORKFLOW_DATA_ROOT/logs" \
  "$WORKFLOW_DATA_ROOT/tools/bin" \
  "$WORKFLOW_DATA_ROOT/tools/uv"
chmod 0700 "$WORKFLOW_DATA_ROOT/bin"

python_entrypoint="$(orichum_python_entrypoint "$WORKFLOW_DATA_ROOT")"
snapshot_path "$python_entrypoint" "$snapshot_dir" orichum-python
python_recorded_version=
python_recorded_source=
python_recorded_artifact="$empty_artifact_sha"
python_current_artifact="$empty_artifact_sha"
python_resolve_upstream=true
python_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  python_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" python version 2>/dev/null || true
  )"
  python_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" python sourceIdentity 2>/dev/null || true
  )"
  python_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" python artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if python_identity="$(
      validate_orichum_python \
        "$WORKFLOW_DATA_ROOT" "$python_entrypoint" 2>/dev/null
    )"; then
    IFS=$'\t' read -r _ python_current_path <<<"$python_identity"
    python_current_artifact="$(sha256_file "$python_current_path")"
  fi
  python_decision="$(
    decide_install_component \
      "$prior_install_state" python \
      "$python_recorded_version" "$python_recorded_source" \
      "$python_current_artifact" "$python_input_sha" "$python_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast ]]; then
    python_resolve_upstream=false
  fi
fi
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
    install_or_reuse_orichum_python \
      "$WORKFLOW_DATA_ROOT" "$python_resolve_upstream" \
      "$python_recorded_version" "$python_recorded_artifact"
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
if [[ "$python_decision" != reused ]]; then
  preflight_orichum_python_runtime \
    "$orichum_python_candidate" "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" || \
    workflow_die "private Orichum Python failed recovery-proxy preflight"
  if [[ "$orichum_python_action" == reused ]]; then
    orichum_python_action=repaired
  fi
fi
activate_orichum_python \
  "$WORKFLOW_DATA_ROOT" "$orichum_python_candidate" || \
  workflow_die "private Orichum Python could not be activated"
ORICHUM_PYTHON="$(resolve_orichum_python "$WORKFLOW_DATA_ROOT")"
export ORICHUM_PYTHON
ORICHUM_PYTHON_VALIDATED="$ORICHUM_PYTHON"
export ORICHUM_PYTHON_VALIDATED
export ORICHUM_INSTALL_BOOTSTRAP=false

candidate_config_root="$installer_temp/control-plane"
stage_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
  "$INSTALLED_CONFIG_ROOT" "$candidate_config_root" || \
  workflow_die "installed Orichum control plane could not be staged"
ORICHUM_CONFIG_ROOT="$candidate_config_root"
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT"
export ORICHUM_CONFIG_HOME
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

UV_TOOL_DIR="$WORKFLOW_DATA_ROOT/tools/uv"
UV_TOOL_BIN_DIR="$WORKFLOW_DATA_ROOT/tools/bin"
export UV_TOOL_DIR UV_TOOL_BIN_DIR

migrate_legacy_model_config "$WORKFLOW_DATA_ROOT"
find "$WORKFLOW_DATA_ROOT/auth" -maxdepth 1 -type f -exec chmod 0600 {} \;
chmod 0755 "$WORKFLOW_ROOT/controller/plugin/scripts/"*.sh

export PATH="$UV_TOOL_BIN_DIR:$HOME/.local/bin:$PATH"

cliproxy_recorded_version=
cliproxy_recorded_source=
cliproxy_recorded_artifact="$empty_artifact_sha"
cliproxy_current_artifact="$empty_artifact_sha"
cliproxy_resolve_upstream=true
cliproxy_decision=upgraded
claudex_recorded_version=
claudex_recorded_source=
claudex_recorded_artifact="$empty_artifact_sha"
claudex_current_artifact="$empty_artifact_sha"
claudex_resolve_upstream=true
claudex_decision=upgraded
leanctx_recorded_version=
leanctx_recorded_source=
leanctx_recorded_artifact="$empty_artifact_sha"
leanctx_current_artifact="$empty_artifact_sha"
leanctx_resolve_upstream=true
leanctx_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  cliproxy_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" cliproxy version 2>/dev/null || true
  )"
  cliproxy_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" cliproxy sourceIdentity 2>/dev/null || true
  )"
  cliproxy_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" cliproxy artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe \
      "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"; then
    cliproxy_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
    )"
  fi
  cliproxy_decision="$(
    decide_install_component \
      "$prior_install_state" cliproxy \
      "$cliproxy_recorded_version" "$cliproxy_recorded_source" \
      "$cliproxy_current_artifact" \
      "$cliproxy_input_sha" "$cliproxy_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$cliproxy_decision" != upgraded ]]; then
    cliproxy_resolve_upstream=false
  fi

  claudex_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" claudex version 2>/dev/null || true
  )"
  claudex_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" claudex sourceIdentity 2>/dev/null || true
  )"
  claudex_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" claudex artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe "$WORKFLOW_DATA_ROOT/bin/claudex"; then
    claudex_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex"
    )"
  fi
  claudex_decision="$(
    decide_install_component \
      "$prior_install_state" claudex \
      "$claudex_recorded_version" "$claudex_recorded_source" \
      "$claudex_current_artifact" "$claudex_input_sha" "$claudex_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$claudex_decision" != upgraded ]]; then
    claudex_resolve_upstream=false
  fi

  leanctx_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" leanctx version 2>/dev/null || true
  )"
  leanctx_recorded_source="$(
    install_state_component_field \
      "$prior_install_state" leanctx sourceIdentity 2>/dev/null || true
  )"
  leanctx_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" leanctx artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if managed_executable_is_safe "$WORKFLOW_DATA_ROOT/bin/lean-ctx"; then
    leanctx_current_artifact="$(
      sha256_file "$WORKFLOW_DATA_ROOT/bin/lean-ctx"
    )"
  fi
  leanctx_decision="$(
    decide_install_component \
      "$prior_install_state" leanctx \
      "$leanctx_recorded_version" "$leanctx_recorded_source" \
      "$leanctx_current_artifact" "$leanctx_input_sha" "$leanctx_probe_sha"
  )"
  if [[ "$INSTALL_MODE" == fast && \
        "$leanctx_decision" != upgraded ]]; then
    leanctx_resolve_upstream=false
  fi
fi

cliproxy_state="$(stage_github_binary \
  router-for-me/CLIProxyAPI 'CLIProxyAPI_' "_${cliproxy_os}_${cliproxy_arch}.tar.gz" \
  cli-proxy-api "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
  "$installer_temp/cliproxy" "$cliproxy_resolve_upstream" \
  "$cliproxy_recorded_version" "$cliproxy_recorded_source" \
  "$cliproxy_recorded_artifact")"
cliproxy_version="$(jq -r '.version' <<<"$cliproxy_state")"
claudex_state="$(stage_github_binary \
  StringKe/claudex 'claudex-v' "-${claudex_arch}-${claudex_os}.tar.gz" \
  claudex "$WORKFLOW_DATA_ROOT/bin/claudex" \
  "$installer_temp/claudex" "$claudex_resolve_upstream" \
  "$claudex_recorded_version" "$claudex_recorded_source" \
  "$claudex_recorded_artifact")"
claudex_version="$(jq -r '.version' <<<"$claudex_state")"
leanctx_state="$(stage_github_binary \
  yvgude/lean-ctx 'lean-ctx-' "$leanctx_release_asset_suffix" \
  lean-ctx "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
  "$installer_temp/leanctx" "$leanctx_resolve_upstream" \
  "$leanctx_recorded_version" "$leanctx_recorded_source" \
  "$leanctx_recorded_artifact")"
leanctx_version="$(jq -r '.version' <<<"$leanctx_state")"
cliproxy_binary_changed="$(jq -r '.changed' <<<"$cliproxy_state")"
claudex_binary_changed="$(jq -r '.changed' <<<"$claudex_state")"
leanctx_binary_changed="$(jq -r '.changed' <<<"$leanctx_state")"
if [[ "$leanctx_binary_changed" == true ]]; then
  leanctx_candidate="$(jq -r '.staged_path' <<<"$leanctx_state")"
else
  leanctx_candidate="$WORKFLOW_DATA_ROOT/bin/lean-ctx"
fi
if [[ "$leanctx_decision" != reused ]]; then
  probe_leanctx_capabilities \
    "$leanctx_candidate" "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
    "$installer_temp" || \
    workflow_die "LeanCTX failed the bounded headless MCP capability probe"
fi
desired_cliproxy_config="$installer_temp/cliproxy.yaml"

if [[ "$platform" == darwin ]]; then
  service_file="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
  desired_service_file="$installer_temp/$SERVICE_LABEL.plist"
  headroom_cleanup_files=(
    "$HOME/Library/LaunchAgents/io.orichum.headroom.plist"
    "$HOME/Library/LaunchAgents/com.user.claudex-headroom.plist"
    "$HOME/Library/LaunchAgents/com.user.headroom-proxy.plist"
  )
  headroom_cleanup_labels=(
    io.orichum.headroom
    com.user.claudex-headroom
    com.user.headroom-proxy
  )
  headroom_cleanup_units=(- - -)
  headroom_cleanup_modes=(new legacy legacy)
  service_mode=0644
  claudex_proxy_service_mode=0644
  cliproxy_service_label="$SERVICE_LABEL"
  cliproxy_service_unit=-
else
  service_file="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service"
  desired_service_file="$installer_temp/orichum-cliproxy.service"
  systemd_user_root="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  headroom_cleanup_files=(
    "$systemd_user_root/orichum-headroom.service"
    "$systemd_user_root/claudex-headroom.service"
    "$systemd_user_root/headroom-proxy.service"
  )
  headroom_cleanup_labels=(- - -)
  headroom_cleanup_units=(
    orichum-headroom.service
    claudex-headroom.service
    headroom-proxy.service
  )
  headroom_cleanup_modes=(new legacy legacy)
  service_mode=0600
  claudex_proxy_service_mode=0600
  cliproxy_service_label=-
  cliproxy_service_unit=orichum-cliproxy.service
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
  "$(dirname "$claudex_proxy_service_file")"

cliproxy_service_was_present=false
cliproxy_service_owned=false
if [[ -e "$service_file" || -L "$service_file" ]]; then
  cliproxy_service_was_present=true
  cliproxy_service_is_owned "$service_file" "$WORKFLOW_DATA_ROOT" || \
    workflow_die "refusing to overwrite unknown service file: $service_file"
  cliproxy_service_owned=true
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
    CLIPROXY_PORT PERSISTED_CLAUDEX_PROXY_PORT PERSISTED_ROUTE_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  workflow_die "service port configuration is invalid"
fi
PRIOR_CLIPROXY_PORT="$CLIPROXY_PORT"
PRIOR_ROUTE_PROXY_PORT="$PERSISTED_ROUTE_PROXY_PORT"
CLIPROXY_PORT="${ORICHUM_CLIPROXY_PORT:-$CLIPROXY_PORT}"
CLAUDEX_PROXY_PORT="${ORICHUM_CLAUDEX_PROXY_PORT:-$PERSISTED_CLAUDEX_PROXY_PORT}"
ROUTE_PROXY_LISTEN_PORT="${ORICHUM_ROUTE_PROXY_PORT:-$PERSISTED_ROUTE_PROXY_PORT}"
valid_service_port "$CLIPROXY_PORT" || workflow_die "invalid CLIProxyAPI port"
valid_service_port "$CLAUDEX_PROXY_PORT" || \
  workflow_die "invalid Claudex proxy port"
valid_service_port "$ROUTE_PROXY_LISTEN_PORT" || \
  workflow_die "invalid Orichum route proxy port"
[[ "$CLIPROXY_PORT" != "$CLAUDEX_PROXY_PORT" && \
   "$CLIPROXY_PORT" != "$ROUTE_PROXY_LISTEN_PORT" && \
   "$CLAUDEX_PROXY_PORT" != "$ROUTE_PROXY_LISTEN_PORT" ]] || \
  workflow_die \
    "CLIProxyAPI, Claudex, and route proxy ports must differ"

cliproxy_endpoint_ready_at() {
  curl -fsS --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:$1/v1/models" 2>/dev/null | \
    cliproxy_models_response_is_ready /dev/stdin
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
cliproxy_listener_owned=false
if [[ "$CLIPROXY_PORT" == "$PRIOR_CLIPROXY_PORT" ]] && \
   [[ "$cliproxy_service_owned" == true ]] && \
   managed_listener_is_owned \
     "$service_file" "$cliproxy_service_label" "$cliproxy_service_unit" \
     "$CLIPROXY_PORT"; then
  cliproxy_listener_owned=true
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
interactive_install=false
if [[ -t 0 && -t 1 ]]; then
  interactive_install=true
fi
CLIPROXY_PORT="$(select_service_port \
  CLIProxyAPI ORICHUM_CLIPROXY_PORT "$CLIPROXY_PORT" \
  "$cliproxy_listener_owned" "$interactive_install")" || exit 1
ROUTE_PROXY_LISTEN_PORT="$(select_service_port \
  'Orichum route proxy' ORICHUM_ROUTE_PROXY_PORT "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_port_owned" "$interactive_install" \
  "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT")" || exit 1
ports_changed=false
if [[ "$CLIPROXY_PORT" != "$PRIOR_CLIPROXY_PORT" ]] || \
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
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" \
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
if [[ "$cliproxy_decision" != reused || \
      "$cliproxy_binary_changed" == true || \
      "$cliproxy_config_changed" == changed || \
      "$cliproxy_service_changed" == changed ]]; then
  probe_cliproxy_management || workflow_die \
    "CLIProxyAPI failed the required management PATCH/readback capability probe"
fi
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
snapshot_path "$WORKFLOW_DATA_ROOT/bin/lean-ctx" "$snapshot_dir" leanctx-binary
snapshot_path "$WORKFLOW_DATA_ROOT/cliproxy.yaml" "$snapshot_dir" cliproxy-config
snapshot_path "$service_file" "$snapshot_dir" cliproxy-service
snapshot_path "$claudex_proxy_service_file" \
  "$snapshot_dir" claudex-proxy-service
snapshot_path "$service_ports_path" "$snapshot_dir" service-ports
snapshot_path "$USER_BIN_DIR/orichum" \
  "$snapshot_dir" orichum-launcher
migrate_legacy_private_tools \
  "$WORKFLOW_DATA_ROOT" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" || \
  workflow_die "legacy private Mempalace and Graphify tools could not be migrated"
snapshot_private_tool_state \
  "$WORKFLOW_DATA_ROOT" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" \
  "$snapshot_dir/private-tools"

cliproxy_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=true
orichum_launcher_mutated=false
private_tools_transaction_active=false
leanctx_transaction_active=false
install_state_transaction_active=false
if [[ "$leanctx_binary_changed" == true ]]; then
  leanctx_transaction_active=true
fi

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

  if [[ "${config_transaction_active:-false}" == true ]]; then
    rollback_installed_control_plane \
      "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
      "$INSTALLED_CONFIG_ROOT" "$control_plane_journal" \
      "$WORKFLOW_LOCK_FD" || \
      rollback_ready=false
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

  if [[ "${leanctx_transaction_active:-false}" == true ]]; then
    restore_snapshot "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
      "$snapshot_dir" leanctx-binary || rollback_ready=false
    snapshot_path_matches "$WORKFLOW_DATA_ROOT/bin/lean-ctx" \
      "$snapshot_dir" leanctx-binary || rollback_ready=false
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

  if [[ "${install_state_transaction_active:-false}" == true ]]; then
    if [[ "$rollback_ready" == true && \
          "$prior_install_state_verified" == true ]]; then
      restore_snapshot "$install_state_path" \
        "$snapshot_dir" install-state || rollback_ready=false
      snapshot_path_matches "$install_state_path" \
        "$snapshot_dir" install-state || rollback_ready=false
    else
      if [[ -e "$install_state_path" || -L "$install_state_path" ]]; then
        if [[ -f "$install_state_path" && \
              ! -L "$install_state_path" && \
              "$(path_uid "$install_state_path")" == "$(id -u)" ]]; then
          rm -f -- "$install_state_path" || rollback_ready=false
        else
          rollback_ready=false
        fi
      fi
    fi
  fi

  [[ "$rollback_ready" == true ]]
}

WORKFLOW_ROLLBACK_HANDLER=rollback_install_transaction
WORKFLOW_TRANSACTION_ACTIVE=true
config_transaction_active=false

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

routing_probe_sha="$(
  install_contract_fingerprint \
    discover-models.sh integrations/common/model_routing.py \
    integrations/common/route_proxy.py
)" || workflow_die "routing probe fingerprint failed"
routing_input_descriptor="$installer_temp/routing-input"
cliproxy_desired_artifact="$(
  if [[ "$cliproxy_binary_changed" == true ]]; then
    sha256_file "$(jq -r '.staged_path' <<<"$cliproxy_state")"
  else
    sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"
  fi
)"
claudex_desired_artifact="$(
  if [[ "$claudex_binary_changed" == true ]]; then
    sha256_file "$(jq -r '.staged_path' <<<"$claudex_state")"
  else
    sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex"
  fi
)"
{
  printf 'cliproxy=%s\n' "$cliproxy_desired_artifact"
  printf 'claudex=%s\n' "$claudex_desired_artifact"
  for routing_input in \
      "$WORKFLOW_ROOT/config/providers.json" \
      "$WORKFLOW_ROOT/config/runtime.json" \
      "$WORKFLOW_ROOT/config/model-stacks.json" \
      "$candidate_config_root/accounts.json" \
      "$desired_cliproxy_config" \
      "$desired_service_file" \
      "$claudex_proxy_desired_service_file"; do
    printf '%s\n' "$(sha256_file "$routing_input")"
  done
  printf 'ports=%s,%s,%s\n' \
    "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT"
} >"$routing_input_descriptor"
chmod 0600 "$routing_input_descriptor"
routing_input_sha="$(sha256_file "$routing_input_descriptor")"

routing_runtime_artifact() {
  local artifact_descriptor="$installer_temp/routing-artifact"
  local active_claudex active_models active_effective active_path
  active_claudex="$(model_config_file \
    "$WORKFLOW_DATA_ROOT" claudex.toml)"
  active_models="$(model_config_file "$WORKFLOW_DATA_ROOT" models.json)"
  active_effective="$(model_config_file \
    "$WORKFLOW_DATA_ROOT" effective-models.json)"
  for active_path in \
      "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
      "$service_file" "$claudex_proxy_service_file" \
      "$active_claudex" "$active_models" "$active_effective"; do
    [[ -f "$active_path" && ! -L "$active_path" ]] || return 1
  done
  {
    for active_path in \
        "$WORKFLOW_DATA_ROOT/cliproxy.yaml" \
        "$service_file" "$claudex_proxy_service_file" \
        "$active_claudex" "$active_models" "$active_effective"; do
      printf '%s\n' "$(sha256_file "$active_path")"
    done
  } >"$artifact_descriptor"
  chmod 0600 "$artifact_descriptor"
  sha256_file "$artifact_descriptor"
}
routing_current_artifact="$empty_artifact_sha"
if observed_routing_artifact="$(routing_runtime_artifact 2>/dev/null)"; then
  routing_current_artifact="$observed_routing_artifact"
fi
routing_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  routing_decision="$(
    decide_install_component \
      "$prior_install_state" routing \
      1 orichum:routing "$routing_current_artifact" \
      "$routing_input_sha" "$routing_probe_sha"
  )"
  if [[ "$routing_decision" == reused && \
        ( "$cliproxy_listener_owned" != true || \
          "$cliproxy_ready_before" != true || \
          "$claudex_proxy_listener_owned" != true ) ]]; then
    routing_decision=repaired
  fi
fi

mempalace_recorded_version=
mempalace_recorded_artifact="$empty_artifact_sha"
mempalace_current_version=
mempalace_current_artifact="$empty_artifact_sha"
mempalace_decision=upgraded
graphify_recorded_version=
graphify_recorded_artifact="$empty_artifact_sha"
graphify_current_version=
graphify_current_artifact="$empty_artifact_sha"
graphify_decision=upgraded
if [[ "$prior_install_state_verified" == true ]]; then
  mempalace_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" mempalace version 2>/dev/null || true
  )"
  mempalace_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" mempalace artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if mempalace_identity="$(
      private_uv_tool_identity \
        "$WORKFLOW_DATA_ROOT" mempalace mempalace \
        mempalace mempalace-mcp 2>/dev/null
    )"; then
    IFS=$'\t' read -r \
      mempalace_current_version mempalace_current_artifact \
      <<<"$mempalace_identity"
  fi
  mempalace_decision="$(
    decide_install_component \
      "$prior_install_state" mempalace \
      "$mempalace_recorded_version" \
      "pypi:mempalace@$mempalace_recorded_version" \
      "$mempalace_current_artifact" \
      "$mempalace_input_sha" "$mempalace_probe_sha"
  )"

  graphify_recorded_version="$(
    install_state_component_field \
      "$prior_install_state" graphify version 2>/dev/null || true
  )"
  graphify_recorded_artifact="$(
    install_state_component_field \
      "$prior_install_state" graphify artifactSha256 2>/dev/null || \
      printf '%s' "$empty_artifact_sha"
  )"
  if graphify_identity="$(
      private_uv_tool_identity \
        "$WORKFLOW_DATA_ROOT" graphifyy graphifyy \
        graphify graphify-mcp 2>/dev/null
    )"; then
    IFS=$'\t' read -r graphify_current_version graphify_current_artifact \
      <<<"$graphify_identity"
  fi
  graphify_decision="$(
    decide_install_component \
      "$prior_install_state" graphify \
      "$graphify_recorded_version" \
      "pypi:graphifyy[mcp,terraform]@$graphify_recorded_version" \
      "$graphify_current_artifact" \
      "$graphify_input_sha" "$graphify_probe_sha"
  )"
fi

if [[ "$mempalace_decision" == upgraded ]]; then
  private_tools_transaction_active=true
  uv tool install --upgrade mempalace
elif [[ "$mempalace_decision" == repaired && \
        ( "$mempalace_current_version" != "$mempalace_recorded_version" || \
          "$mempalace_current_artifact" != \
            "$mempalace_recorded_artifact" ) ]]; then
  private_tools_transaction_active=true
  uv tool install --force "mempalace==$mempalace_recorded_version"
fi
mempalace_identity="$(
  private_uv_tool_identity \
    "$WORKFLOW_DATA_ROOT" mempalace mempalace \
    mempalace mempalace-mcp
)" || workflow_die "Mempalace private installation is unsafe"
IFS=$'\t' read -r mempalace_version mempalace_artifact \
  <<<"$mempalace_identity"
mempalace_mcp="$UV_TOOL_BIN_DIR/mempalace-mcp"
if [[ "$mempalace_decision" != reused ]]; then
  install -d -m 0700 "$installer_temp/mempalace-probe"
  if ! PYTHONDONTWRITEBYTECODE=1 "$ORICHUM_PYTHON" -B \
    "$WORKFLOW_ROOT/integrations/common/mcp_probe.py" \
    --timeout 30 \
    --require-tool mempalace_get_taxonomy \
    --require-tool mempalace_search \
    --require-tool mempalace_checkpoint \
    -- "$mempalace_mcp" --palace "$installer_temp/mempalace-probe"; then
    workflow_die "Mempalace MCP failed protocol readiness checks"
  fi
fi

if [[ "$graphify_decision" == upgraded ]]; then
  private_tools_transaction_active=true
  uv tool install --upgrade 'graphifyy[mcp,terraform]'
elif [[ "$graphify_decision" == repaired && \
        ( "$graphify_current_version" != "$graphify_recorded_version" || \
          "$graphify_current_artifact" != "$graphify_recorded_artifact" ) ]]; then
  private_tools_transaction_active=true
  uv tool install --force \
    "graphifyy[mcp,terraform]==$graphify_recorded_version"
fi
graphify_identity="$(
  private_uv_tool_identity \
    "$WORKFLOW_DATA_ROOT" graphifyy graphifyy \
    graphify graphify-mcp
)" || workflow_die "Graphify private installation is unsafe"
IFS=$'\t' read -r graphify_version graphify_artifact \
  <<<"$graphify_identity"
graphify_binary="$UV_TOOL_BIN_DIR/graphify"
graphify_mcp="$UV_TOOL_BIN_DIR/graphify-mcp"
if [[ "$graphify_decision" != reused ]]; then
  reconcile_graphify_storage \
    "$WORKFLOW_DATA_ROOT" "$graphify_binary" "$graphify_mcp" \
    "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" "$installer_temp" || \
    workflow_die \
      "Graphify failed absolute-output, extract, update, or MCP capability checks"
fi
if [[ "$INSTALL_MODE" == upgrade || \
      "$prior_install_state_verified" != true ]]; then
  graphify_doctor_diagnostics \
    "$WORKFLOW_DATA_ROOT" "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_ROOT" \
    "$ORICHUM_PYTHON" "$graphify_binary" || \
    printf 'NOTICE: repository graph upgrade diagnostics were unavailable\n' \
      >&2
fi

preflight_claudex_proxy() (
  local preflight_port preflight_pid= preflight_ready=false
  local response_file="$installer_temp/claudex-proxy-preflight-models.json"
  preflight_port="$(next_available_port \
    "$ROUTE_PROXY_LISTEN_PORT" "$CLAUDEX_PROXY_PORT" \
    "$CLIPROXY_PORT")" || \
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
    "$CLIPROXY_PORT")" || \
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

normalize_headroom_free_endpoint_snapshot \
  "$snapshot_dir/service-ports.data" \
  "${prior_model_generation_snapshot:-}" \
  "$PRIOR_CLIPROXY_PORT" "$PERSISTED_CLAUDEX_PROXY_PORT" \
  "$PRIOR_ROUTE_PROXY_PORT" || \
  workflow_die "rollback endpoint state could not be normalized safely"

for cleanup_index in "${!headroom_cleanup_files[@]}"; do
  remove_owned_headroom_installation \
    "$platform" "$WORKFLOW_DATA_ROOT" \
    "${headroom_cleanup_files[$cleanup_index]}" \
    "${headroom_cleanup_labels[$cleanup_index]}" \
    "${headroom_cleanup_units[$cleanup_index]}" \
    "${headroom_cleanup_modes[$cleanup_index]}" || \
    workflow_die \
      "legacy Orichum Headroom installation could not be removed safely"
done
if [[ "$platform" == systemd ]]; then
  systemctl --user daemon-reload
fi

write_service_ports "$WORKFLOW_DATA_ROOT" \
  "$CLIPROXY_PORT" "$CLAUDEX_PROXY_PORT" "$ROUTE_PROXY_LISTEN_PORT" || \
  workflow_die "service port configuration could not be saved"

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
if [[ "$leanctx_binary_changed" == true ]]; then
  activate_staged_file "$(jq -r '.staged_path' <<<"$leanctx_state")" \
    "$WORKFLOW_DATA_ROOT/bin/lean-ctx" 0755
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

source "$WORKFLOW_ROOT/discover-models.sh"
model_discovery_succeeded=true
model_discovery_performed=false
routing_action="$routing_decision"
discovery_entrypoint=discover_models_main_core
model_discovery_status=0
if [[ "$routing_decision" != reused ]]; then
  model_discovery_performed=true
  CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint" || \
    model_discovery_status=$?
fi
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
    routing_action=reused
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
  claudex_model_config_changed=true
  if [[ -n "$prior_model_generation_snapshot" && \
        -f "$prior_model_generation_snapshot/claudex.toml" ]] && \
     cmp -s "$active_claudex_config" \
       "$prior_model_generation_snapshot/claudex.toml"; then
    claudex_model_config_changed=false
  fi
  if [[ "$claudex_decision" != reused || \
        "$claudex_binary_changed" == true || \
        "$claudex_model_config_changed" == true || \
        "$claudex_proxy_service_changed" == changed || \
        "$claudex_proxy_port_changed" == true ]]; then
    preflight_claudex_translation_proxy "$active_claudex_config" || \
      workflow_die \
        "Claudex translation proxy failed isolated bind and catalogue preflight"
  fi

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
      if ! loopback_port_is_listening "$ROUTE_PROXY_LISTEN_PORT"; then
        activation_port_ready=true
        break
      fi
      sleep 0.1
    done
    [[ "$activation_port_ready" == true ]] || workflow_die \
      "Orichum route proxy activation port $ROUTE_PROXY_LISTEN_PORT still has a listener; prior state will be restored"
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

if [[ "$endpoint_lock_owned" == true ]]; then
  release_endpoint_config_lock \
    "$WORKFLOW_DATA_ROOT" "$endpoint_lock_token" || \
    workflow_die "endpoint model publication lock could not be released"
  endpoint_lock_owned=false
fi
config_transaction_active=true
activate_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" \
  "$candidate_config_root" "$INSTALLED_CONFIG_ROOT" \
  "$control_plane_journal" "$WORKFLOW_LOCK_FD" || \
  workflow_die "installed Orichum control plane could not be committed"
ORICHUM_CONFIG_ROOT="$INSTALLED_CONFIG_ROOT"
ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT"
export ORICHUM_CONFIG_HOME
verify_committed_control_plane \
  "$ORICHUM_CONFIG_ROOT" "$WORKFLOW_DATA_ROOT" || \
  workflow_die "committed Orichum control plane is invalid"
install -m 0600 "$WORKFLOW_ROOT/controller/settings.json" \
  "$WORKFLOW_DATA_ROOT/claude-config/settings.json"
if [[ "$controller_plugin_decision" != reused ]]; then
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT/bin/orichum-plugin" sync || \
    workflow_die \
      "services are healthy, but declared Claude plugins could not be synchronized; rerun the installer after correcting the plugin error"
fi
if [[ "$claudex_proxy_action" != pending-provider-login ]]; then
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$WORKFLOW_ROOT/bin/orichum-runtime-ready" \
      "$WORKFLOW_DATA_ROOT" || \
    workflow_die "focused Orichum runtime readiness failed"
fi
install_state_prior_components="$installer_temp/prior-components.json"
if [[ "$prior_install_state_verified" == true ]]; then
  jq -e '.components' "$prior_install_state" \
    >"$install_state_prior_components" || \
    workflow_die "verified installer component state could not be read"
else
  printf '{}\n' >"$install_state_prior_components"
fi
chmod 0600 "$install_state_prior_components"
install_state_components="$installer_temp/install-state-components.json"
jq -n \
  --slurpfile prior "$install_state_prior_components" \
  --arg python_version "$orichum_python_version" \
  --arg python_artifact "$(sha256_file "$ORICHUM_PYTHON")" \
  --arg python_input "$python_input_sha" \
  --arg python_probe "$python_probe_sha" \
  --arg cliproxy_version "$cliproxy_version" \
  --arg cliproxy_tag "$(jq -r '.tag' <<<"$cliproxy_state")" \
  --arg cliproxy_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api")" \
  --arg cliproxy_input "$cliproxy_input_sha" \
  --arg cliproxy_probe "$cliproxy_probe_sha" \
  --arg claudex_version "$claudex_version" \
  --arg claudex_tag "$(jq -r '.tag' <<<"$claudex_state")" \
  --arg claudex_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/claudex")" \
  --arg claudex_input "$claudex_input_sha" \
  --arg claudex_probe "$claudex_probe_sha" \
  --arg leanctx_version "$leanctx_version" \
  --arg leanctx_tag "$(jq -r '.tag' <<<"$leanctx_state")" \
  --arg leanctx_artifact \
    "$(sha256_file "$WORKFLOW_DATA_ROOT/bin/lean-ctx")" \
  --arg leanctx_input "$leanctx_input_sha" \
  --arg leanctx_probe "$leanctx_probe_sha" \
  --arg mempalace_version "$mempalace_version" \
  --arg mempalace_artifact "$mempalace_artifact" \
  --arg mempalace_input "$mempalace_input_sha" \
  --arg mempalace_probe "$mempalace_probe_sha" \
  --arg graphify_version "$graphify_version" \
  --arg graphify_artifact "$graphify_artifact" \
  --arg graphify_input "$graphify_input_sha" \
  --arg graphify_probe "$graphify_probe_sha" \
  --arg controller_plugin_input "$controller_plugin_input_sha" \
  --arg controller_plugin_probe "$controller_plugin_probe_sha" \
  '$prior[0] + {
    python: {
      version: $python_version,
      sourceIdentity: ("python:" + $python_version),
      artifactSha256: $python_artifact,
      inputSha256: $python_input,
      probeSha256: $python_probe
    },
    cliproxy: {
      version: $cliproxy_version,
      sourceIdentity: (
        "github:router-for-me/CLIProxyAPI@" + $cliproxy_tag
      ),
      artifactSha256: $cliproxy_artifact,
      inputSha256: $cliproxy_input,
      probeSha256: $cliproxy_probe
    },
    claudex: {
      version: $claudex_version,
      sourceIdentity: ("github:StringKe/claudex@" + $claudex_tag),
      artifactSha256: $claudex_artifact,
      inputSha256: $claudex_input,
      probeSha256: $claudex_probe
    },
    leanctx: {
      version: $leanctx_version,
      sourceIdentity: ("github:yvgude/lean-ctx@" + $leanctx_tag),
      artifactSha256: $leanctx_artifact,
      inputSha256: $leanctx_input,
      probeSha256: $leanctx_probe
    },
    mempalace: {
      version: $mempalace_version,
      sourceIdentity: ("pypi:mempalace@" + $mempalace_version),
      artifactSha256: $mempalace_artifact,
      inputSha256: $mempalace_input,
      probeSha256: $mempalace_probe
    },
    graphify: {
      version: $graphify_version,
      sourceIdentity: (
        "pypi:graphifyy[mcp,terraform]@" + $graphify_version
      ),
      artifactSha256: $graphify_artifact,
      inputSha256: $graphify_input,
      probeSha256: $graphify_probe
    },
    controllerPlugin: {
      version: "1",
      sourceIdentity: "orichum:controller-plugin",
      artifactSha256: $controller_plugin_input,
      inputSha256: $controller_plugin_input,
      probeSha256: $controller_plugin_probe
    }
  }' >"$install_state_components" || \
  workflow_die "candidate installer state could not be built"
chmod 0600 "$install_state_components"
if [[ "$model_discovery_succeeded" == true ]]; then
  routing_verified_artifact="$(routing_runtime_artifact)" || \
    workflow_die "verified routing artifact could not be fingerprinted"
  routing_state_candidate="$installer_temp/routing-state-components.json"
  jq \
    --arg artifact "$routing_verified_artifact" \
    --arg input "$routing_input_sha" \
    --arg probe "$routing_probe_sha" \
    '.routing = {
      version: "1",
      sourceIdentity: "orichum:routing",
      artifactSha256: $artifact,
      inputSha256: $input,
      probeSha256: $probe
    }' "$install_state_components" >"$routing_state_candidate" || \
    workflow_die "candidate routing state could not be built"
  chmod 0600 "$routing_state_candidate"
  mv -f "$routing_state_candidate" "$install_state_components"
fi
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
print_component_status_table \
  "$python_decision" "$cliproxy_decision" "$claudex_decision" \
  "$leanctx_decision" "$mempalace_decision" "$graphify_decision" \
  "$routing_action" "$controller_plugin_decision" || \
  workflow_die "component reconciliation status is invalid"
printf 'Installed Orichum with Claudex %s, CLIProxyAPI %s, and LeanCTX %s for %s.\n' \
  "$claudex_version" "$cliproxy_version" "$leanctx_version" "$platform"
print_install_summary \
  "$WORKFLOW_ROOT" "$WORKFLOW_DATA_ROOT" "$USER_BIN_DIR" \
  "$WORKFLOW_DATA_ROOT/bin/claudex" \
  "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api" \
  "$mempalace_mcp" "$graphify_mcp" "$service_file" \
  "$CLIPROXY_PORT" "$cliproxy_action" \
  "$claudex_proxy_service_file" "$CLAUDEX_PROXY_PORT" \
  "$ROUTE_PROXY_LISTEN_PORT" \
  "$claudex_proxy_action" \
  "$ORICHUM_PYTHON" "$orichum_python_version" \
  "$orichum_python_candidate" "$orichum_python_action" \
  "$WORKFLOW_DATA_ROOT/bin/lean-ctx"
if [[ "$claudex_proxy_action" == pending-provider-login ]]; then
  printf 'Next: orichum provider login <provider>; %s/install.sh\n' \
    "$WORKFLOW_ROOT"
elif [[ "$INSTALL_MODE" == upgrade || \
        "$prior_install_state_verified" != true ]]; then
  printf '\nRunning Orichum doctor...\n'
  ORICHUM_CONFIG_HOME="$ORICHUM_CONFIG_ROOT" \
  ORICHUM_DATA_HOME="$WORKFLOW_DATA_ROOT" \
    "$USER_BIN_DIR/orichum" doctor
else
  printf '\nFast readiness checks passed.\n'
fi
install_state_transaction_active=true
python3 -I -B "$WORKFLOW_ROOT/integrations/common/install_state.py" \
  write "$install_state_path" "$install_state_platform" \
  "$install_state_components" || \
  workflow_die "verified installer state could not be published"
finalize_installed_control_plane \
  "$ORICHUM_PYTHON" "$WORKFLOW_ROOT" "$control_plane_journal" \
  "$WORKFLOW_LOCK_FD" || \
  workflow_die "installed Orichum control-plane journal could not be finalized"
cliproxy_transaction_active=false
claudex_proxy_transaction_active=false
claudex_proxy_runtime_mutated=false
endpoint_transaction_active=false
private_tools_transaction_active=false
leanctx_transaction_active=false
python_transaction_active=false
config_transaction_active=false
install_state_transaction_active=false
WORKFLOW_TRANSACTION_ACTIVE=false
