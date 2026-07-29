#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(cd -P "$(mktemp -d)" && pwd)"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

FAKE_BIN="$TEST_ROOT/fake-bin"
install -d "$FAKE_BIN"

cat >"$FAKE_BIN/uname" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == -s ]]; then
  printf '%s\n' "${FAKE_UNAME_S:?}"
elif [[ "${1:-}" == -m ]]; then
  printf '%s\n' "${FAKE_UNAME_M:-x86_64}"
else
  /usr/bin/uname "$@"
fi
SH

cat >"$FAKE_BIN/launchctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'launchctl %s\n' "$*" >>"${FAKE_SERVICE_LOG:?}"
case "${1:-}" in
  print)
    label="${2##*/}"
    marker="$FAKE_LOADED_ROOT/$label"
    [[ -f "$marker" ]] || exit 113
    printf 'path = %s\n' "$(<"$marker")"
    ;;
  bootout)
    service_file="${3:-}"
    label="$(basename "$service_file" .plist)"
    rm -f -- "$FAKE_LOADED_ROOT/$label"
    ;;
  *) ;;
esac
SH

cat >"$FAKE_BIN/systemctl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >>"${FAKE_SERVICE_LOG:?}"
arguments=" $* "
unit="${*: -1}"
marker="$FAKE_LOADED_ROOT/$unit"
if [[ "$arguments" == *" show "* && "$arguments" == *" LoadState "* ]]; then
  if [[ -f "$marker" ]]; then printf 'loaded\n'; else printf 'not-found\n'; fi
elif [[ "$arguments" == *" show "* && "$arguments" == *" FragmentPath "* ]]; then
  [[ -f "$marker" ]]
  sed -n '1p' "$marker"
elif [[ "$arguments" == *" stop "* ]]; then
  rm -f -- "$marker"
elif [[ "$arguments" == *" disable "* || \
        "$arguments" == *" daemon-reload "* ]]; then
  :
else
  printf 'unexpected fake systemctl call: %s\n' "$*" >&2
  exit 1
fi
SH

chmod 0755 "$FAKE_BIN/uname" "$FAKE_BIN/launchctl" "$FAKE_BIN/systemctl"

# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
# shellcheck source=../lib/uninstall.sh
source "$ROOT/lib/uninstall.sh"
declare -F orichum_uninstall_validate_lifecycle_roots >/dev/null
if orichum_uninstall_validate_lifecycle_roots \
    "$TEST_ROOT/home/.local" "$TEST_ROOT/config" \
    "$TEST_ROOT/home/.local/state/orichum/install.lock" \
    >/dev/null 2>&1; then
  printf 'data root containing the lifecycle lock was accepted\n' >&2
  exit 1
fi
if orichum_uninstall_validate_lifecycle_roots \
    "$TEST_ROOT/data" "$TEST_ROOT/home" \
    "$TEST_ROOT/home/.local/state/orichum/install.lock" \
    >/dev/null 2>&1; then
  printf 'config root containing the lifecycle lock was accepted\n' >&2
  exit 1
fi

render_services() {
  local platform="$1"
  local home="$2"
  local data_root="$3"
  local digest
  digest="$(printf '0%.0s' {1..64})"
  (
    export HOME="$home"
    # shellcheck source=../lib/workflow.sh
    source "$ROOT/lib/workflow.sh"
    if [[ "$platform" == darwin ]]; then
      install -d "$HOME/Library/LaunchAgents"
      render_launch_agent \
        "$HOME/Library/LaunchAgents/io.orichum.cliproxy.plist" \
        "$data_root"
      render_leanctx_proxy_launch_agent \
        "$HOME/Library/LaunchAgents/io.orichum.leanctx-proxy.plist" \
        "$data_root" 13458
      render_claudex_proxy_launch_agent \
        "$HOME/Library/LaunchAgents/io.orichum.route-proxy.plist" \
        "$data_root" "$ROOT" 13456 13458 8317 "$digest"
    else
      install -d "$HOME/.config/systemd/user"
      render_systemd_user_unit \
        "$HOME/.config/systemd/user/orichum-cliproxy.service" \
        "$data_root"
      render_leanctx_proxy_systemd_user_unit \
        "$HOME/.config/systemd/user/orichum-leanctx-proxy.service" \
        "$data_root" 13458
      render_claudex_proxy_systemd_user_unit \
        "$HOME/.config/systemd/user/orichum-route-proxy.service" \
        "$data_root" "$ROOT" 13456 13458 8317 "$digest"
    fi
  )
}

seed_installation() {
  local platform="$1"
  local fixture_root="$2"
  local home="$fixture_root/home"
  local data_root="$fixture_root/data"
  local config_root="$fixture_root/config"
  local user_bin="$fixture_root/user-bin"
  local loaded_root="$fixture_root/loaded"

  install -d \
    "$home" "$data_root"/{auth,bin,claude-config,logs,model-config,python,state,tools} \
    "$config_root" "$user_bin" "$loaded_root"
  printf 'credential\n' >"$data_root/auth/account.json"
  printf 'session\n' >"$data_root/state/session.json"
  printf 'claude session\n' >"$data_root/claude-config/history.jsonl"
  printf 'model\n' >"$data_root/model-config/current-state"
  printf 'project\n' >"$config_root/projects.json"
  printf 'runtime\n' >"$data_root/bin/cli-proxy-api"
  printf 'python\n' >"$data_root/python/runtime"
  printf 'tool\n' >"$data_root/tools/runtime"
  printf 'log\n' >"$data_root/logs/cliproxy.log"
  printf 'config\n' >"$data_root/cliproxy.yaml"
  printf 'key\n' >"$data_root/cliproxy-management.key"
  ln -s "$ROOT/bin/orichum" "$user_bin/orichum"
  render_services "$platform" "$home" "$data_root"
  (
    export HOME="$home"
    export XDG_CONFIG_HOME="$home/.config"
    export ORICHUM_HOME="$home/.orichum"
    export ORICHUM_CONFIG_HOME="$config_root"
    export ORICHUM_DATA_HOME="$data_root"
    export ORICHUM_INSTALL_BOOTSTRAP=true
    reconcile_orichum_completions \
      "$ROOT" "$home/.orichum" "$config_root" "$data_root"
  )

  if [[ "$platform" == darwin ]]; then
    printf '%s\n' \
      "$home/Library/LaunchAgents/io.orichum.cliproxy.plist" \
      >"$loaded_root/io.orichum.cliproxy"
    printf '%s\n' \
      "$home/Library/LaunchAgents/io.orichum.leanctx-proxy.plist" \
      >"$loaded_root/io.orichum.leanctx-proxy"
    printf '%s\n' \
      "$home/Library/LaunchAgents/io.orichum.route-proxy.plist" \
      >"$loaded_root/io.orichum.route-proxy"
  else
    printf '%s\n' \
      "$home/.config/systemd/user/orichum-cliproxy.service" \
      >"$loaded_root/orichum-cliproxy.service"
    printf '%s\n' \
      "$home/.config/systemd/user/orichum-leanctx-proxy.service" \
      >"$loaded_root/orichum-leanctx-proxy.service"
    printf '%s\n' \
      "$home/.config/systemd/user/orichum-route-proxy.service" \
      >"$loaded_root/orichum-route-proxy.service"
  fi
}

run_uninstall() {
  local platform="$1"
  local fixture_root="$2"
  local kernel_name="$platform"
  shift 2
  [[ "$platform" != darwin ]] || kernel_name=Darwin
  HOME="$fixture_root/home" \
  ORICHUM_HOME="$fixture_root/home/.orichum" \
  XDG_CONFIG_HOME="$fixture_root/home/.config" \
  ORICHUM_DATA_HOME="$fixture_root/data" \
  ORICHUM_CONFIG_HOME="$fixture_root/config" \
  USER_BIN_DIR="$fixture_root/user-bin" \
  FAKE_UNAME_S="$kernel_name" \
  FAKE_SERVICE_LOG="$fixture_root/service.log" \
  FAKE_LOADED_ROOT="$fixture_root/loaded" \
  PATH="$FAKE_BIN:$PATH" \
    "$ROOT/install.sh" --uninstall "$@"
}

locked_root="$TEST_ROOT/lifecycle-locked"
seed_installation darwin "$locked_root"
lifecycle_lock="$locked_root/home/.local/state/orichum/install.lock"
install -d -m 0700 "$lifecycle_lock"
printf '%s\n' "$$" >"$lifecycle_lock/pid"
printf '%s\n' "test-owner" >"$lifecycle_lock/identity"
if run_uninstall darwin "$locked_root" \
    >"$locked_root/locked-output.log" 2>&1; then
  printf 'uninstall ignored the shared lifecycle lock\n' >&2
  exit 1
fi
grep -Fq 'another installer owns' "$locked_root/locked-output.log"
[[ -L "$locked_root/user-bin/orichum" ]]
[[ -f "$locked_root/data/bin/cli-proxy-api" ]]
rm -rf -- "$lifecycle_lock"

darwin_root="$TEST_ROOT/darwin-default"
seed_installation darwin "$darwin_root"
printf 'standalone\n' >"$darwin_root/standalone-tool"
run_uninstall darwin "$darwin_root"

for preserved in \
    auth/account.json \
    state/session.json \
    claude-config/history.jsonl \
    model-config/current-state; do
  [[ -f "$darwin_root/data/$preserved" ]]
done
[[ -f "$darwin_root/config/projects.json" ]]
for removed in \
    bin python tools logs leanctx/proxy \
    cliproxy.yaml cliproxy-management.key; do
  [[ ! -e "$darwin_root/data/$removed" && \
     ! -L "$darwin_root/data/$removed" ]]
done
[[ ! -e "$darwin_root/user-bin/orichum" ]]
[[ ! -e "$darwin_root/home/.orichum/completions/zsh/_orichum" ]]
[[ ! -e "$darwin_root/home/.orichum/completions/bash/orichum" ]]
[[ ! -e "$darwin_root/home/.config/fish/completions/orichum.fish" ]]
if rg -q '^# (>>>|<<<) Orichum completion' \
    "$darwin_root/home/.zshrc" "$darwin_root/home/.bashrc" \
    "$darwin_root/home/.bash_profile"; then
  printf 'managed completion profile block survived uninstall\n' >&2
  exit 1
fi
[[ ! -e "$darwin_root/home/Library/LaunchAgents/io.orichum.cliproxy.plist" ]]
[[ ! -e "$darwin_root/home/Library/LaunchAgents/io.orichum.leanctx-proxy.plist" ]]
[[ ! -e "$darwin_root/home/Library/LaunchAgents/io.orichum.route-proxy.plist" ]]
[[ -f "$darwin_root/standalone-tool" ]]
grep -Fq 'bootout' "$darwin_root/service.log"

# Default uninstall is idempotent and keeps the preserved state.
run_uninstall darwin "$darwin_root"
[[ -f "$darwin_root/data/auth/account.json" ]]
[[ -f "$darwin_root/config/projects.json" ]]

drift_root="$TEST_ROOT/drifted-completion"
seed_installation darwin "$drift_root"
printf '\n# user-edited completion\n' \
  >>"$drift_root/home/.orichum/completions/zsh/_orichum"
python3 - "$drift_root/home/.bashrc" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = path.read_text(encoding="utf-8")
path.write_text(
    payload.replace("  . ", "  . -- ", 1),
    encoding="utf-8",
)
PY
run_uninstall darwin "$drift_root" \
  >"$drift_root/uninstall.stdout" 2>"$drift_root/uninstall.stderr"
[[ -f "$drift_root/home/.orichum/completions/zsh/_orichum" ]]
[[ -f "$drift_root/home/.bashrc" ]]
rg -Fq '# >>> Orichum completion >>>' "$drift_root/home/.bashrc"
rg -Fq 'retained drifted Orichum completion' \
  "$drift_root/uninstall.stderr"
[[ ! -e "$drift_root/home/.orichum/completions/bash/orichum" ]]

xdg_change_root="$TEST_ROOT/xdg-change"
seed_installation darwin "$xdg_change_root"
install -d "$xdg_change_root/old-xdg"
(
  export HOME="$xdg_change_root/home"
  export XDG_CONFIG_HOME="$xdg_change_root/old-xdg"
  export ORICHUM_HOME="$xdg_change_root/home/.orichum"
  export ORICHUM_CONFIG_HOME="$xdg_change_root/config"
  export ORICHUM_DATA_HOME="$xdg_change_root/data"
  export ORICHUM_INSTALL_BOOTSTRAP=true
  reconcile_orichum_completions \
    "$ROOT" "$ORICHUM_HOME" "$ORICHUM_CONFIG_HOME" "$ORICHUM_DATA_HOME"
)
[[ -f "$xdg_change_root/old-xdg/fish/completions/orichum.fish" ]]
run_uninstall darwin "$xdg_change_root"
[[ ! -e "$xdg_change_root/old-xdg/fish/completions/orichum.fish" ]]

profile_race="$TEST_ROOT/uninstall-profile-race"
install -d "$profile_race"
orichum_profile_block bash "/tmp/orichum-completion" "$profile_race/block"
{
  printf '# user profile\n'
  cat "$profile_race/block"
} >"$profile_race/profile"
(
  workflow_python() {
    shift 3
    command python3 -c '
import sys
code = sys.stdin.read()
mutation = "    profile.write_bytes(payload + b\"# concurrent edit\\n\")\n"
marker = "    # Claim the path atomically before replacement.\n"
if marker in code:
    code = code.replace(marker, mutation + marker, 1)
else:
    needle = "    os.replace(temporary, profile)\n"
    code = code.replace(needle, mutation + needle, 1)
exec(compile(code, "<profile-race>", "exec"))
' "$@"
  }
  orichum_uninstall_remove_profile_block \
    "$profile_race/profile" "$profile_race/block" \
    >"$profile_race/stdout" 2>"$profile_race/stderr"
)
rg -Fq '# concurrent edit' "$profile_race/profile"
rg -Fq '# >>> Orichum completion >>>' "$profile_race/profile"
rg -Fq 'retained drifted Orichum completion profile' \
  "$profile_race/stderr"

systemd_root="$TEST_ROOT/systemd-purge"
seed_installation Linux "$systemd_root"
printf 'standalone\n' >"$systemd_root/standalone-tool"
run_uninstall Linux "$systemd_root" --purge
[[ ! -e "$systemd_root/data" ]]
[[ ! -e "$systemd_root/config" ]]
[[ ! -e "$systemd_root/user-bin/orichum" ]]
[[ ! -e "$systemd_root/home/.config/systemd/user/orichum-cliproxy.service" ]]
[[ ! -e "$systemd_root/home/.config/systemd/user/orichum-leanctx-proxy.service" ]]
[[ ! -e "$systemd_root/home/.config/systemd/user/orichum-route-proxy.service" ]]
[[ -f "$systemd_root/standalone-tool" ]]
grep -Fq 'stop orichum-cliproxy.service' "$systemd_root/service.log"
grep -Fq 'stop orichum-leanctx-proxy.service' "$systemd_root/service.log"
grep -Fq 'disable orichum-route-proxy.service' "$systemd_root/service.log"
grep -Fq 'daemon-reload' "$systemd_root/service.log"

# Purge is also idempotent.
run_uninstall Linux "$systemd_root" --purge

foreign_root="$TEST_ROOT/foreign-service"
seed_installation darwin "$foreign_root"
printf 'foreign definition\n' \
  >"$foreign_root/home/Library/LaunchAgents/io.orichum.cliproxy.plist"
if run_uninstall darwin "$foreign_root" \
    >"$foreign_root/output.log" 2>&1; then
  printf 'foreign service definition was accepted\n' >&2
  exit 1
fi
grep -Fq 'refusing unknown Orichum service' "$foreign_root/output.log"
[[ -L "$foreign_root/user-bin/orichum" ]]
[[ -f "$foreign_root/data/bin/cli-proxy-api" ]]
[[ -f "$foreign_root/home/Library/LaunchAgents/io.orichum.route-proxy.plist" ]]

argument_root="$TEST_ROOT/arguments"
seed_installation darwin "$argument_root"
if run_uninstall darwin "$argument_root" --unknown >/dev/null 2>&1; then
  printf 'unknown uninstall argument was accepted\n' >&2
  exit 1
fi
if HOME="$argument_root/home" \
   ORICHUM_DATA_HOME="$argument_root/data" \
   ORICHUM_CONFIG_HOME="$argument_root/config" \
   USER_BIN_DIR="$argument_root/user-bin" \
   "$ROOT/install.sh" --purge >/dev/null 2>&1; then
  printf 'purge without uninstall was accepted\n' >&2
  exit 1
fi
[[ -L "$argument_root/user-bin/orichum" ]]
[[ -f "$argument_root/data/bin/cli-proxy-api" ]]

printf 'uninstall contract tests passed\n'
