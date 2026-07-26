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
      render_claudex_proxy_launch_agent \
        "$HOME/Library/LaunchAgents/io.orichum.route-proxy.plist" \
        "$data_root" "$ROOT" 13456 8317 "$digest"
    else
      install -d "$HOME/.config/systemd/user"
      render_systemd_user_unit \
        "$HOME/.config/systemd/user/orichum-cliproxy.service" \
        "$data_root"
      render_claudex_proxy_systemd_user_unit \
        "$HOME/.config/systemd/user/orichum-route-proxy.service" \
        "$data_root" "$ROOT" 13456 8317 "$digest"
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
    "$home" "$data_root"/{auth,bin,claude-config,graphs,logs,model-config,python,state,tools} \
    "$config_root" "$user_bin" "$loaded_root"
  printf 'credential\n' >"$data_root/auth/account.json"
  printf 'session\n' >"$data_root/state/session.json"
  printf 'graph\n' >"$data_root/graphs/graph.json"
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

  if [[ "$platform" == darwin ]]; then
    printf '%s\n' \
      "$home/Library/LaunchAgents/io.orichum.cliproxy.plist" \
      >"$loaded_root/io.orichum.cliproxy"
    printf '%s\n' \
      "$home/Library/LaunchAgents/io.orichum.route-proxy.plist" \
      >"$loaded_root/io.orichum.route-proxy"
  else
    printf '%s\n' \
      "$home/.config/systemd/user/orichum-cliproxy.service" \
      >"$loaded_root/orichum-cliproxy.service"
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

darwin_root="$TEST_ROOT/darwin-default"
seed_installation darwin "$darwin_root"
printf 'standalone\n' >"$darwin_root/standalone-tool"
run_uninstall darwin "$darwin_root"

for preserved in \
    auth/account.json \
    state/session.json \
    graphs/graph.json \
    claude-config/history.jsonl \
    model-config/current-state; do
  [[ -f "$darwin_root/data/$preserved" ]]
done
[[ -f "$darwin_root/config/projects.json" ]]
for removed in \
    bin python tools logs cliproxy.yaml cliproxy-management.key; do
  [[ ! -e "$darwin_root/data/$removed" && \
     ! -L "$darwin_root/data/$removed" ]]
done
[[ ! -e "$darwin_root/user-bin/orichum" ]]
[[ ! -e "$darwin_root/home/Library/LaunchAgents/io.orichum.cliproxy.plist" ]]
[[ ! -e "$darwin_root/home/Library/LaunchAgents/io.orichum.route-proxy.plist" ]]
[[ -f "$darwin_root/standalone-tool" ]]
grep -Fq 'bootout' "$darwin_root/service.log"

# Default uninstall is idempotent and keeps the preserved state.
run_uninstall darwin "$darwin_root"
[[ -f "$darwin_root/data/auth/account.json" ]]
[[ -f "$darwin_root/config/projects.json" ]]

systemd_root="$TEST_ROOT/systemd-purge"
seed_installation Linux "$systemd_root"
printf 'standalone\n' >"$systemd_root/standalone-tool"
run_uninstall Linux "$systemd_root" --purge
[[ ! -e "$systemd_root/data" ]]
[[ ! -e "$systemd_root/config" ]]
[[ ! -e "$systemd_root/user-bin/orichum" ]]
[[ ! -e "$systemd_root/home/.config/systemd/user/orichum-cliproxy.service" ]]
[[ ! -e "$systemd_root/home/.config/systemd/user/orichum-route-proxy.service" ]]
[[ -f "$systemd_root/standalone-tool" ]]
grep -Fq 'stop orichum-cliproxy.service' "$systemd_root/service.log"
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
