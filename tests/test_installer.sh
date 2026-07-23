#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for script in \
  install.sh doctor.sh rollback.sh smoke-test.sh discover-models.sh \
  bin/claudex-gpt bin/claudex-login bin/claudex-models \
  bin/claudex-doctor bin/claude-headroom \
  bin/claudex-headroom bin/claudex-context bin/claudex-plugin \
  tests/test_install_transaction.sh tests/test_claudex_proxy.sh \
  controller/plugin/scripts/check-local-services.sh \
  controller/plugin/scripts/guard-orchestration.sh
do
  bash -n "$ROOT/$script"
done

if rg -q 'CLIPROXY_VERSION|CLAUDEX_VERSION|/opt/homebrew|/Users/arvind' \
  "$ROOT/install.sh" "$ROOT/bin" "$ROOT/lib"; then
  printf 'portable scripts contain a tool pin or personal absolute path\n' >&2
  exit 1
fi

if ! rg -q 'releases/latest' "$ROOT/lib/workflow.sh"; then
  printf 'installer is not using rolling GitHub releases\n' >&2
  exit 1
fi

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-installer-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
data_root="$fixture/data with % and \$"

headroom_cli_root="$fixture/headroom-cli"
install -d "$headroom_cli_root/headroom/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$HEADROOM_CONFIG_DIR" "$HEADROOM_WORKSPACE_DIR" "${HEADROOM_SAVINGS_PATH-unset}" "${HEADROOM_SAVINGS_EVENTS_PATH-unset}" "${HEADROOM_TOIN_PATH-unset}" "${HEADROOM_SUBSCRIPTION_STATE_PATH-unset}" "${HEADROOM_SETTINGS_PATH-unset}" "$@"' \
  >"$headroom_cli_root/headroom/bin/headroom"
chmod 0755 "$headroom_cli_root/headroom/bin/headroom"
headroom_cli_output="$(
  CLAUDEX_DATA_DIR="$headroom_cli_root" \
  HEADROOM_CONFIG_DIR=/inherited/config \
  HEADROOM_WORKSPACE_DIR=/inherited/workspace \
  HEADROOM_SAVINGS_PATH=/inherited/savings \
  HEADROOM_SAVINGS_EVENTS_PATH=/inherited/savings-events \
  HEADROOM_TOIN_PATH=/inherited/toin \
  HEADROOM_SUBSCRIPTION_STATE_PATH=/inherited/subscription \
  HEADROOM_SETTINGS_PATH=/inherited/settings \
    "$ROOT/bin/claudex-headroom" perf --hours 24 --format json
)"
[[ "$(sed -n '1p' <<<"$headroom_cli_output")" == \
   "$headroom_cli_root/headroom/config" ]]
[[ "$(sed -n '2p' <<<"$headroom_cli_output")" == \
   "$headroom_cli_root/headroom/state" ]]
[[ "$(sed -n '3,7p' <<<"$headroom_cli_output")" == \
   $'unset\nunset\nunset\nunset\nunset' ]]
[[ "$(sed -n '8,12p' <<<"$headroom_cli_output")" == \
   $'perf\n--hours\n24\n--format\njson' ]]

if CLAUDEX_DATA_DIR="$fixture/missing-headroom" \
    "$ROOT/bin/claudex-headroom" perf \
    >"$fixture/missing-headroom.stdout" \
    2>"$fixture/missing-headroom.stderr"; then
  printf 'claudex-headroom accepted a missing private binary\n' >&2
  exit 1
fi
[[ ! -s "$fixture/missing-headroom.stdout" ]]
rg -q 'workflow Headroom is not installed' \
  "$fixture/missing-headroom.stderr"

for valid_port in 1024 8317 8787 65535; do
  valid_service_port "$valid_port"
done
for invalid_port in '' 0 1023 65536 abc '8317 ' 013456; do
  if valid_service_port "$invalid_port"; then
    printf 'invalid service port was accepted: %q\n' "$invalid_port" >&2
    exit 1
  fi
done

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 18787 13457
[[ "$(read_service_ports "$ports_root")" == $'18317\t18787\t13457' ]]
[[ "$(jq -r 'keys | @tsv' "$(service_ports_file "$ports_root")")" == \
   $'claudexProxyPort\tcliproxyPort\theadroomPort' ]]
[[ "$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' \
  "$(service_ports_file "$ports_root")")" == 600 ]]
[[ "$(read_service_ports "$fixture/no-ports-yet")" == $'8317\t8787\t13456' ]]
printf '{"cliproxyPort": 18317, "headroomPort": 18787}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == $'18317\t18787\t13456' ]]
printf '{"cliproxyPort": 13456, "headroomPort": 18787}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == $'13456\t18787\t13457' ]]
printf '{"cliproxyPort": 18317, "headroomPort": 13456}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == $'18317\t13456\t13457' ]]
printf '{"cliproxyPort": 8787, "headroomPort": 8787}\n' \
  >"$(service_ports_file "$ports_root")"
if read_service_ports "$ports_root" >/dev/null 2>&1; then
  printf 'duplicate service ports were accepted\n' >&2
  exit 1
fi
if write_service_ports "$ports_root" 18317 18787 18787; then
  printf 'duplicate third service port was accepted\n' >&2
  exit 1
fi
write_service_ports "$ports_root" 18317 18787 13457

listener_port_file="$fixture/listener.port"
python3 - "$listener_port_file" <<'PY' &
import socket
import sys
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
open(sys.argv[1], "w", encoding="utf-8").write(str(listener.getsockname()[1]))
time.sleep(30)
PY
listener_pid=$!
for _ in {1..50}; do
  [[ -s "$listener_port_file" ]] && break
  sleep 0.05
done
occupied_port="$(cat "$listener_port_file")"
if port_is_available "$occupied_port"; then
  printf 'occupied loopback port was reported available\n' >&2
  exit 1
fi
available_port="$(next_available_port "$occupied_port")"
valid_service_port "$available_port"
[[ "$available_port" -gt "$occupied_port" ]]
port_is_available "$available_port"
second_reserved_port="$(next_available_port "$occupied_port" "$available_port")"
unreserved_port="$(next_available_port \
  "$occupied_port" "$available_port" "$second_reserved_port")"
valid_service_port "$unreserved_port"
[[ "$unreserved_port" != "$available_port" ]]
[[ "$unreserved_port" != "$second_reserved_port" ]]
wrapped_port="$(next_available_port 65535 1024 1025)"
valid_service_port "$wrapped_port"
[[ "$wrapped_port" -lt 65535 && "$wrapped_port" != 1024 && "$wrapped_port" != 1025 ]]
/bin/bash -c '
  set -u
  source "$1"
  [[ "$(select_service_port CLIProxyAPI CLAUDEX_CLIPROXY_PORT \
    "$2" true false)" == "$2" ]]
' _ "$ROOT/lib/workflow.sh" "$available_port"
[[ "$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" true false)" == \
  "$occupied_port" ]]
kill_marker="$fixture/select-service-port.kill"
kill() {
  printf 'called\n' >"$kill_marker"
  return 99
}
selected_noninteractive="$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" false false \
  "$available_port" "$second_reserved_port" \
  2>"$fixture/noninteractive-port.stderr")"
unset -f kill
[[ "$selected_noninteractive" == "$unreserved_port" ]]
[[ ! -e "$kill_marker" ]]
rg -Fq "port $occupied_port is occupied" "$fixture/noninteractive-port.stderr"
rg -Fq "using $unreserved_port" "$fixture/noninteractive-port.stderr"
[[ "$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$available_port" false false \
  "$available_port" "$second_reserved_port" \
  2>"$fixture/implicit-reserved-port.stderr")" == "$unreserved_port" ]]
rg -Fq "using $unreserved_port" "$fixture/implicit-reserved-port.stderr"
if CLAUDEX_CLIPROXY_PORT="$occupied_port" select_service_port \
    CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" false false \
    >"$fixture/explicit-port.stdout" 2>"$fixture/explicit-port.stderr"; then
  printf 'explicit occupied port override was silently rewritten\n' >&2
  exit 1
fi
rg -Fq 'CLAUDEX_CLIPROXY_PORT' "$fixture/explicit-port.stderr"
[[ "$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" false true \
  2>"$fixture/interactive-port.stderr" <<<"$available_port")" == "$available_port" ]]
rg -Fq "port $occupied_port is occupied" "$fixture/interactive-port.stderr"
kill "$listener_pid"
wait "$listener_pid" 2>/dev/null || true

owned_cliproxy="$fixture/owned-cliproxy.service"
printf '%s\n' \
  "ExecStart=$(systemd_quote "$data_root/bin/cli-proxy-api") --config $(systemd_quote "$data_root/cliproxy.yaml")" \
  >"$owned_cliproxy"
cliproxy_service_is_owned "$owned_cliproxy" "$data_root"
printf 'ExecStart=/foreign/cli-proxy-api --config /foreign/config\n' \
  >"$fixture/foreign-cliproxy.service"
if cliproxy_service_is_owned "$fixture/foreign-cliproxy.service" "$data_root"; then
  printf 'foreign CLIProxyAPI service was accepted as workflow-owned\n' >&2
  exit 1
fi
printf '%s\n' \
  '# expected strings hidden in comments are not ownership' \
  "# $data_root/bin/cli-proxy-api --config $data_root/cliproxy.yaml" \
  'ExecStart=/foreign/cli-proxy-api --config /foreign/config' \
  >"$fixture/deceptive-cliproxy.service"
if cliproxy_service_is_owned "$fixture/deceptive-cliproxy.service" "$data_root"; then
  printf 'comment strings spoofed CLIProxyAPI ownership\n' >&2
  exit 1
fi

owned_headroom="$fixture/owned-headroom.service"
printf '%s\n' \
  'Description=Headroom proxy for Claudex' \
  'ExecStart=/global/headroom proxy --host 127.0.0.1 --port 8787 --mode token --no-cache --intercept-tool-results --lossless --code-aware' \
  "Environment=\"HEADROOM_CONFIG_DIR=$data_root/headroom/config\"" \
  "Environment=\"HEADROOM_WORKSPACE_DIR=$data_root/headroom/state\"" \
  >"$owned_headroom"
headroom_service_is_owned "$owned_headroom" "$data_root" legacy
printf 'ExecStart=/foreign/headroom proxy --host 127.0.0.1 --port 8787\n' \
  >"$fixture/foreign-headroom.service"
if headroom_service_is_owned \
    "$fixture/foreign-headroom.service" "$data_root" legacy; then
  printf 'foreign Headroom service was accepted as workflow-owned\n' >&2
  exit 1
fi
printf '%s\n' \
  'Description=Unrelated service' \
  '# proxy 127.0.0.1' \
  "# HEADROOM_CONFIG_DIR=$data_root/headroom/config" \
  "# HEADROOM_WORKSPACE_DIR=$data_root/headroom/state" \
  'ExecStart=/foreign/headroom' >"$fixture/deceptive-headroom.service"
if headroom_service_is_owned \
    "$fixture/deceptive-headroom.service" "$data_root" legacy; then
  printf 'comment strings spoofed Headroom ownership\n' >&2
  exit 1
fi

summary="$(print_install_summary \
  /portable/checkout /portable/data /portable/user-bin \
  /portable/data/bin/claudex /portable/data/bin/cli-proxy-api \
  /portable/data/headroom/bin/headroom /portable/user-bin/mempalace-mcp \
  /portable/user-bin/graphify-mcp /portable/cliproxy.service \
  /portable/headroom.service 18317 18787 reused reconciled \
  /portable/claudex-proxy.service 13457 installed)"
for summary_value in \
  '/portable/checkout' '/portable/data' '/portable/user-bin' \
  '/portable/data/bin/claudex' '/portable/data/bin/cli-proxy-api' \
  '/portable/data/headroom/bin/headroom' '/portable/user-bin/mempalace-mcp' \
  '/portable/user-bin/graphify-mcp' '/portable/cliproxy.service' \
  '/portable/headroom.service' '127.0.0.1:18317' '127.0.0.1:18787' \
  '/portable/claudex-proxy.service' '127.0.0.1:13457' \
  'reused' 'reconciled' 'installed'
do
  grep -Fq "$summary_value" <<<"$summary"
done

ignore_fixture="$fixture/ignore-surface"
install -d "$ignore_fixture/bin" "$ignore_fixture/runtime/auth" \
  "$ignore_fixture/logs" "$ignore_fixture/backups"
cp "$ROOT/.gitignore" "$ignore_fixture/.gitignore"
printf 'legacy secret\n' >"$ignore_fixture/runtime/auth/provider.json"
printf 'legacy log\n' >"$ignore_fixture/logs/service.log"
printf 'legacy backup\n' >"$ignore_fixture/backups/config.old"
printf 'downloaded\n' >"$ignore_fixture/bin/claudex"
printf 'downloaded\n' >"$ignore_fixture/bin/cli-proxy-api"
printf '#!/bin/sh\n' >"$ignore_fixture/bin/claudex-context"
git -C "$ignore_fixture" init -q
ignore_status="$(git -C "$ignore_fixture" status --short --untracked-files=all)"
[[ "$ignore_status" == *'?? .gitignore'* ]]
[[ "$ignore_status" == *'?? bin/claudex-context'* ]]
[[ "$ignore_status" != *'runtime/auth/provider.json'* ]]
[[ "$ignore_status" != *'logs/service.log'* ]]
[[ "$ignore_status" != *'backups/config.old'* ]]
! grep -Fqx '?? bin/claudex' <<<"$ignore_status"
! grep -Fqx '?? bin/cli-proxy-api' <<<"$ignore_status"
ignore_add_surface="$(git -C "$ignore_fixture" add --dry-run .)"
[[ "$ignore_add_surface" != *'provider.json'* ]]

fixture_physical="$(cd "$fixture" && pwd -P)"
safe_data_parent="$fixture_physical/safe-data-parent"
install -d "$safe_data_parent/existing"
safe_data_root="$(HOME="$fixture/home" \
  CLAUDEX_DATA_DIR="$safe_data_parent/existing/../existing/new/child" \
  validated_workflow_data_dir "$ROOT")"
[[ "$safe_data_root" == "$safe_data_parent/existing/new/child" ]]
data_alias="$fixture/data-alias"
ln -s "$safe_data_parent" "$data_alias"
for unsafe_data_root in \
  / // /tmp/.. "$fixture/home/." "$ROOT" "$ROOT/nested" \
  "$data_alias/existing/new"
do
  if HOME="$fixture/home" CLAUDEX_DATA_DIR="$unsafe_data_root" \
    validated_workflow_data_dir "$ROOT" >/dev/null 2>&1; then
    printf 'unsafe data-root alias was accepted: %s\n' "$unsafe_data_root" >&2
    exit 1
  fi
done

printf 'same\n' >"$fixture/desired"
cp "$fixture/desired" "$fixture/current"
[[ "$(file_change_state "$fixture/desired" "$fixture/current")" == unchanged ]]
printf 'different\n' >"$fixture/desired"
[[ "$(file_change_state "$fixture/desired" "$fixture/current")" == changed ]]
[[ "$(file_change_state "$fixture/desired" "$fixture/absent")" == changed ]]

printf 'same\n' >"$fixture/private-desired"
cp "$fixture/private-desired" "$fixture/private-current"
chmod 0600 "$fixture/private-current"
[[ "$(private_file_change_state \
  "$fixture/private-desired" "$fixture/private-current" 600)" == unchanged ]]
chmod 0644 "$fixture/private-current"
[[ "$(private_file_change_state \
  "$fixture/private-desired" "$fixture/private-current" 600)" == changed ]]
cp "$fixture/private-desired" "$fixture/private-target"
rm -f "$fixture/private-current"
ln -s "$fixture/private-target" "$fixture/private-current"
[[ "$(private_file_change_state \
  "$fixture/private-desired" "$fixture/private-current" 600)" == changed ]]

if service_restart_required false unchanged true; then
  printf 'unchanged healthy service was selected for restart\n' >&2
  exit 1
fi
for restart_case in \
  'true unchanged true' \
  'false changed true' \
  'false unchanged false'
do
  # shellcheck disable=SC2086
  if ! service_restart_required $restart_case; then
    printf 'changed or unhealthy service was not selected for restart: %s\n' \
      "$restart_case" >&2
    exit 1
  fi
done

for restart_case in \
  'true unchanged true unchanged' \
  'false changed true unchanged' \
  'false unchanged false unchanged' \
  'false unchanged true changed'
do
  headroom_transaction_active=false
  # shellcheck disable=SC2086
  reconcile_headroom_transaction $restart_case
  [[ "$headroom_restart_required" == true ]]
  [[ "$headroom_transaction_active" == true ]]
done
headroom_transaction_active=true
reconcile_headroom_transaction false unchanged true unchanged
[[ "$headroom_restart_required" == false ]]
[[ "$headroom_transaction_active" == false ]]

rollback_probe_calls=0
rollback_probe() { rollback_probe_calls=$((rollback_probe_calls + 1)); }
run_rollback_if_active false rollback_probe
[[ "$rollback_probe_calls" == 0 ]]
run_rollback_if_active true rollback_probe
[[ "$rollback_probe_calls" == 1 ]]

install -d "$fixture/headroom-version-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$FAKE_HEADROOM_DIST_VERSION"' \
  >"$fixture/headroom-version-python"
chmod 0755 "$fixture/headroom-version-python"
printf '#!%s\nexit 0\n' "$fixture/headroom-version-python" \
  >"$fixture/headroom-version-bin/headroom"
chmod 0755 "$fixture/headroom-version-bin/headroom"
for complete_version in \
  '1.2.3rc1' \
  '1.2.3.post4' \
  '1.2.3+workflow.7'
do
  observed_version="$(FAKE_HEADROOM_DIST_VERSION="$complete_version" \
    headroom_distribution_version "$fixture/headroom-version-bin/headroom")"
  [[ "$observed_version" == "$complete_version" ]]
done
if distribution_version_changed '1.2.3rc1' '1.2.3rc1'; then
  printf 'identical complete distribution versions were marked changed\n' >&2
  exit 1
fi
for version_pair in \
  '1.2.3rc1 1.2.3' \
  '1.2.3.post1 1.2.3.post10' \
  '1.2.3+local.1 1.2.3+local.10'
do
  # shellcheck disable=SC2086
  if ! distribution_version_changed $version_pair; then
    printf 'distinct complete distribution versions overlapped: %s\n' \
      "$version_pair" >&2
    exit 1
  fi
done

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" >>"$HEADROOM_UV_CALLS"' \
  >"$fixture/headroom-version-bin/uv"
chmod 0755 "$fixture/headroom-version-bin/uv"
export FAKE_HEADROOM_DIST_VERSION='1.2.3rc1.post2+workflow.5'
restore_headroom_probe() {
  restore_headroom_distribution "$FAKE_HEADROOM_DIST_VERSION"
}
HEADROOM_UV_CALLS="$fixture/headroom-uv.calls" \
  PATH="$fixture/headroom-version-bin:$PATH" \
  run_rollback_if_active false restore_headroom_probe
[[ ! -e "$fixture/headroom-uv.calls" ]]
HEADROOM_UV_CALLS="$fixture/headroom-uv.calls" \
  PATH="$fixture/headroom-version-bin:$PATH" \
  run_rollback_if_active true restore_headroom_probe
[[ "$(wc -l <"$fixture/headroom-uv.calls" | tr -d ' ')" == 1 ]]
rg -Fxq 'tool install --force headroom-ai[all]==1.2.3rc1.post2+workflow.5' \
  "$fixture/headroom-uv.calls"
unset FAKE_HEADROOM_DIST_VERSION

install -d "$fixture/headroom-transaction-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "partial mutation\n" >"$HEADROOM_PARTIAL_MARKER"' \
  'case "$HEADROOM_UV_BEHAVIOR" in' \
  '  fail) exit 73 ;;' \
  '  signal) kill -TERM "$PPID"; exit 0 ;;' \
  '  success) exit 0 ;;' \
  '  *) exit 74 ;;' \
  'esac' >"$fixture/headroom-transaction-bin/uv"
chmod 0755 "$fixture/headroom-transaction-bin/uv"

run_headroom_transaction_case() {
  local behavior="$1"
  local case_root="$2"
  local later_status="${3:-0}"
  install -d "$case_root"
  HEADROOM_UV_BEHAVIOR="$behavior" \
    HEADROOM_PARTIAL_MARKER="$case_root/partial" \
    PATH="$fixture/headroom-transaction-bin:$PATH" \
    bash -c '
      set -euo pipefail
      source "$1"
      workflow_cleanup_init
      trap '\''workflow_cleanup "$?"'\'' EXIT
      trap '\''workflow_cleanup 143'\'' TERM
      case_root="$2"
      later_status="$3"
      transaction_recovery_probe() {
        printf "recovered\n" >"$case_root/recovery"
      }
      transaction_recovery() {
        run_rollback_if_active "$headroom_transaction_active" \
          transaction_recovery_probe
      }
      WORKFLOW_TRANSACTION_ACTIVE=true
      WORKFLOW_ROLLBACK_HANDLER=transaction_recovery
      headroom_transaction_active=false
      upgrade_headroom_distribution
      reconcile_headroom_transaction false unchanged true unchanged
      [[ "$headroom_transaction_active" == false ]]
      exit "$later_status"
    ' _ "$ROOT/lib/workflow.sh" "$case_root" "$later_status"
}

set +e
run_headroom_transaction_case fail "$fixture/headroom-partial-failure"
headroom_partial_status=$?
set -e
[[ "$headroom_partial_status" == 73 ]]
[[ -f "$fixture/headroom-partial-failure/partial" ]]
[[ "$(cat "$fixture/headroom-partial-failure/recovery")" == recovered ]]

set +e
run_headroom_transaction_case signal "$fixture/headroom-signal"
headroom_signal_status=$?
set -e
[[ "$headroom_signal_status" == 143 ]]
[[ -f "$fixture/headroom-signal/partial" ]]
[[ "$(cat "$fixture/headroom-signal/recovery")" == recovered ]]

set +e
run_headroom_transaction_case success "$fixture/headroom-unchanged" 17
headroom_unchanged_status=$?
set -e
[[ "$headroom_unchanged_status" == 17 ]]
[[ -f "$fixture/headroom-unchanged/partial" ]]
[[ ! -e "$fixture/headroom-unchanged/recovery" ]]

install -d "$fixture/headroom-before-cliproxy"
set +e
HEADROOM_UV_BEHAVIOR=success \
  HEADROOM_PARTIAL_MARKER="$fixture/headroom-before-cliproxy/upgrade" \
  PATH="$fixture/headroom-transaction-bin:$PATH" \
  bash -c '
    set -euo pipefail
    source "$1"
    workflow_cleanup_init
    trap '\''workflow_cleanup "$?"'\'' EXIT
    case_root="$2"
    headroom_recovery_probe() {
      printf "unexpected Headroom recovery\n" >"$case_root/recovery"
    }
    installer_recovery() {
      run_rollback_if_active "$headroom_transaction_active" \
        headroom_recovery_probe
    }
    WORKFLOW_TRANSACTION_ACTIVE=true
    WORKFLOW_ROLLBACK_HANDLER=installer_recovery
    headroom_transaction_active=false
    upgrade_headroom_distribution
    printf "version-read\n" >>"$case_root/markers"
    reconcile_headroom_transaction false unchanged true unchanged
    printf "headroom-reconciled\n" >>"$case_root/markers"
    [[ "$headroom_transaction_active" == false ]]
    printf "cliproxy-activation\n" >>"$case_root/markers"
    activate_staged_file "$case_root/missing-cliproxy" \
      "$case_root/cliproxy" 0755
  ' _ "$ROOT/lib/workflow.sh" "$fixture/headroom-before-cliproxy" \
    2>"$fixture/headroom-before-cliproxy/stderr"
headroom_before_cliproxy_status=$?
set -e
[[ "$headroom_before_cliproxy_status" != 0 ]]
[[ "$(cat "$fixture/headroom-before-cliproxy/markers")" == $'version-read\nheadroom-reconciled\ncliproxy-activation' ]]
[[ ! -e "$fixture/headroom-before-cliproxy/recovery" ]]

install -d "$fixture/snapshots"
printf 'original\n' >"$fixture/existing"
chmod 0640 "$fixture/existing"
snapshot_path "$fixture/existing" "$fixture/snapshots" existing
printf 'replacement\n' >"$fixture/existing"
chmod 0600 "$fixture/existing"
restore_snapshot "$fixture/existing" "$fixture/snapshots" existing
[[ "$(cat "$fixture/existing")" == original ]]
snapshot_path_matches "$fixture/existing" "$fixture/snapshots" existing
if [[ "$(uname -s)" == Darwin ]]; then
  [[ "$(stat -f '%Lp' "$fixture/existing")" == 640 ]]
else
  [[ "$(stat -c '%a' "$fixture/existing")" == 640 ]]
fi
chmod 0600 "$fixture/existing"
if snapshot_path_matches "$fixture/existing" "$fixture/snapshots" existing; then
  printf 'mode-drifted restored service definition passed exact verification\n' >&2
  exit 1
fi
restore_snapshot "$fixture/existing" "$fixture/snapshots" existing
snapshot_path "$fixture/absent" "$fixture/snapshots" absent
printf 'created\n' >"$fixture/absent"
restore_snapshot "$fixture/absent" "$fixture/snapshots" absent
[[ ! -e "$fixture/absent" ]]
snapshot_path_matches "$fixture/absent" "$fixture/snapshots" absent
printf 'drifted\n' >"$fixture/existing"
if snapshot_path_matches "$fixture/existing" "$fixture/snapshots" existing; then
  printf 'drifted restored service definition passed exact verification\n' >&2
  exit 1
fi
restore_snapshot "$fixture/existing" "$fixture/snapshots" existing

lock_dir="$fixture/state/install.lock"
install -d "$(dirname "$lock_dir")"
acquire_workflow_lock "$lock_dir"
if bash -c 'source "$1"; acquire_workflow_lock "$2"' _ \
  "$ROOT/lib/workflow.sh" "$lock_dir" 2>/dev/null; then
  printf 'live installer lock was not rejected\n' >&2
  exit 1
fi
release_workflow_lock "$lock_dir"
(
  exit 0
) &
stale_pid=$!
wait "$stale_pid"
mkdir "$lock_dir"
printf '%s\n' "$stale_pid" >"$lock_dir/pid"
acquire_workflow_lock "$lock_dir"
[[ "$(cat "$lock_dir/pid")" == "$$" ]]
release_workflow_lock "$lock_dir"
[[ ! -e "$lock_dir" ]]

(
  exit 0
) &
observed_stale_pid=$!
wait "$observed_stale_pid"
mkdir "$lock_dir"
printf '%s\n' "$observed_stale_pid" >"$lock_dir/pid"
printf 'stale-owner\n' >"$lock_dir/identity"
sleep 30 &
new_live_pid=$!
install -d "$fixture/race-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == "$RACE_LOCK_DIR" ]]; then' \
  '  /bin/rm -rf -- "$RACE_LOCK_DIR"' \
  '  /bin/mkdir "$RACE_LOCK_DIR"' \
  '  printf "%s\\n" "$RACE_LIVE_PID" >"$RACE_LOCK_DIR/pid"' \
  '  printf "new-live-owner\\n" >"$RACE_LOCK_DIR/identity"' \
  '  /bin/mv "$@"' \
  '  set +e' \
  '  /bin/bash -c '\''source "$1"; acquire_workflow_lock "$2"'\'' _ "$WORKFLOW_LIB" "$RACE_LOCK_DIR" >/dev/null 2>&1' \
  '  printf "%s\\n" "$?" >"$RACE_C_STATUS"' \
  '  exit 0' \
  'fi' \
  'exec /bin/mv "$@"' >"$fixture/race-bin/mv"
chmod 0755 "$fixture/race-bin/mv"
if PATH="$fixture/race-bin:$PATH" RACE_LOCK_DIR="$lock_dir" \
  RACE_LIVE_PID="$new_live_pid" WORKFLOW_LIB="$ROOT/lib/workflow.sh" \
  RACE_C_STATUS="$fixture/actor-c.status" \
  acquire_workflow_lock "$lock_dir" 2>/dev/null; then
  race_status=0
else
  race_status=$?
fi
actor_c_status="$(cat "$fixture/actor-c.status" 2>/dev/null || true)"
if [[ "$race_status" == 0 ]] || \
   [[ ! "$actor_c_status" =~ ^[0-9]+$ ]] || \
   [[ "$actor_c_status" == 0 ]] || \
   [[ "$(cat "$lock_dir/pid" 2>/dev/null || true)" != "$new_live_pid" ]]; then
  kill "$new_live_pid" 2>/dev/null || true
  wait "$new_live_pid" 2>/dev/null || true
  printf 'three-actor reclamation displaced B or allowed C to acquire\n' >&2
  exit 1
fi
[[ ! -e "$lock_dir.guard" ]]
kill "$new_live_pid" 2>/dev/null || true
wait "$new_live_pid" 2>/dev/null || true
rm -rf -- "$lock_dir"

for quarantine_signal in INT TERM; do
  signal_case_dir="$fixture/quarantine-$quarantine_signal"
  signal_lock="$signal_case_dir/install.lock"
  install -d "$signal_case_dir" "$signal_case_dir/race-bin"
  (
    exit 0
  ) &
  signal_stale_pid=$!
  wait "$signal_stale_pid"
  mkdir "$signal_lock"
  printf '%s\n' "$signal_stale_pid" >"$signal_lock/pid"
  printf 'signal-stale-owner\n' >"$signal_lock/identity"
  sleep 30 &
  signal_live_pid=$!
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'if [[ "$1" == "$RACE_LOCK_DIR" ]]; then' \
    '  /bin/rm -rf -- "$RACE_LOCK_DIR"' \
    '  /bin/mkdir "$RACE_LOCK_DIR"' \
    '  printf "%s\\n" "$RACE_LIVE_PID" >"$RACE_LOCK_DIR/pid"' \
    '  printf "signal-live-owner\\n" >"$RACE_LOCK_DIR/identity"' \
    '  /bin/mv "$@"' \
    '  /bin/kill -s "$RACE_SIGNAL" "$RACE_A_PID"' \
    '  exit 0' \
    'fi' \
    'exec /bin/mv "$@"' >"$signal_case_dir/race-bin/mv"
  chmod 0755 "$signal_case_dir/race-bin/mv"
  set +e
  PATH="$signal_case_dir/race-bin:$PATH" RACE_LOCK_DIR="$signal_lock" \
    RACE_LIVE_PID="$signal_live_pid" RACE_SIGNAL="$quarantine_signal" \
    bash -c '
      set -euo pipefail
      source "$1"
      workflow_cleanup_init
      trap '\''workflow_cleanup "$?"'\'' EXIT
      trap '\''workflow_cleanup 130'\'' INT
      trap '\''workflow_cleanup 143'\'' TERM
      export RACE_A_PID="$$"
      acquire_workflow_lock "$2"
    ' _ "$ROOT/lib/workflow.sh" "$signal_lock" 2>"$signal_case_dir/stderr"
  signal_case_status=$?
  set -e
  if [[ "$quarantine_signal" == INT ]]; then
    expected_signal_status=130
  else
    expected_signal_status=143
  fi
  if [[ "$signal_case_status" != "$expected_signal_status" ]] || \
     [[ "$(cat "$signal_lock/pid" 2>/dev/null || true)" != "$signal_live_pid" ]] || \
     [[ -e "$signal_lock.guard" ]] || \
     bash -c 'source "$1"; acquire_workflow_lock "$2"' _ \
       "$ROOT/lib/workflow.sh" "$signal_lock" 2>/dev/null; then
    kill "$signal_live_pid" 2>/dev/null || true
    wait "$signal_live_pid" 2>/dev/null || true
    printf '%s cleanup stranded the quarantined live owner or released its guard\n' \
      "$quarantine_signal" >&2
    exit 1
  fi
  kill "$signal_live_pid" 2>/dev/null || true
  wait "$signal_live_pid" 2>/dev/null || true
  rm -rf -- "$signal_case_dir"
done

nonzero_case_dir="$fixture/quarantine-nonzero-mv"
nonzero_lock="$nonzero_case_dir/install.lock"
install -d "$nonzero_case_dir" "$nonzero_case_dir/race-bin"
(
  exit 0
) &
nonzero_stale_pid=$!
wait "$nonzero_stale_pid"
mkdir "$nonzero_lock"
printf '%s\n' "$nonzero_stale_pid" >"$nonzero_lock/pid"
printf 'nonzero-stale-owner\n' >"$nonzero_lock/identity"
sleep 30 &
nonzero_live_pid=$!
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == "$RACE_LOCK_DIR" ]]; then' \
  '  /bin/rm -rf -- "$RACE_LOCK_DIR"' \
  '  /bin/mkdir "$RACE_LOCK_DIR"' \
  '  printf "%s\\n" "$RACE_LIVE_PID" >"$RACE_LOCK_DIR/pid"' \
  '  printf "nonzero-live-owner\\n" >"$RACE_LOCK_DIR/identity"' \
  '  /bin/mv "$@"' \
  '  exit 1' \
  'fi' \
  'exec /bin/mv "$@"' >"$nonzero_case_dir/race-bin/mv"
chmod 0755 "$nonzero_case_dir/race-bin/mv"
if PATH="$nonzero_case_dir/race-bin:$PATH" RACE_LOCK_DIR="$nonzero_lock" \
  RACE_LIVE_PID="$nonzero_live_pid" acquire_workflow_lock "$nonzero_lock" \
  2>"$nonzero_case_dir/stderr"; then
  nonzero_case_status=0
else
  nonzero_case_status=$?
fi
set +e
bash -c 'source "$1"; acquire_workflow_lock "$2"' _ \
  "$ROOT/lib/workflow.sh" "$nonzero_lock" >/dev/null 2>&1
nonzero_actor_c_status=$?
set -e
nonzero_owner_restored=false
if [[ "$(cat "$nonzero_lock/pid" 2>/dev/null || true)" == "$nonzero_live_pid" ]]; then
  nonzero_owner_restored=true
fi
if [[ "$nonzero_case_status" == 0 ]] || [[ "$nonzero_actor_c_status" == 0 ]] || \
   { [[ "$nonzero_owner_restored" != true ]] && [[ ! -d "$nonzero_lock.guard" ]]; }; then
  kill "$nonzero_live_pid" 2>/dev/null || true
  wait "$nonzero_live_pid" 2>/dev/null || true
  rm -rf -- "$nonzero_case_dir"
  WORKFLOW_LOCK_GUARD_DIR=
  WORKFLOW_LOCK_GUARD_IDENTITY=
  clear_workflow_lock_quarantine
  printf 'nonzero mv result stranded B and exposed canonical lock to C\n' >&2
  exit 1
fi
kill "$nonzero_live_pid" 2>/dev/null || true
wait "$nonzero_live_pid" 2>/dev/null || true
rm -rf -- "$nonzero_case_dir"
WORKFLOW_LOCK_GUARD_DIR=
WORKFLOW_LOCK_GUARD_IDENTITY=
clear_workflow_lock_quarantine

restore_case_dir="$fixture/quarantine-restore-failure"
restore_lock="$restore_case_dir/install.lock"
install -d "$restore_case_dir" "$restore_case_dir/race-bin"
(
  exit 0
) &
restore_stale_pid=$!
wait "$restore_stale_pid"
mkdir "$restore_lock"
printf '%s\n' "$restore_stale_pid" >"$restore_lock/pid"
printf 'restore-stale-owner\n' >"$restore_lock/identity"
sleep 30 &
restore_live_pid=$!
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == "$RACE_LOCK_DIR" ]]; then' \
  '  /bin/rm -rf -- "$RACE_LOCK_DIR"' \
  '  /bin/mkdir "$RACE_LOCK_DIR"' \
  '  printf "%s\\n" "$RACE_LIVE_PID" >"$RACE_LOCK_DIR/pid"' \
  '  printf "restore-live-owner\\n" >"$RACE_LOCK_DIR/identity"' \
  '  exec /bin/mv "$@"' \
  'fi' \
  'case "$1" in' \
  '  "$RACE_LOCK_DIR".stale.*) exit 1 ;;' \
  'esac' \
  'exec /bin/mv "$@"' >"$restore_case_dir/race-bin/mv"
chmod 0755 "$restore_case_dir/race-bin/mv"
if PATH="$restore_case_dir/race-bin:$PATH" RACE_LOCK_DIR="$restore_lock" \
  RACE_LIVE_PID="$restore_live_pid" acquire_workflow_lock "$restore_lock" \
  2>"$restore_case_dir/stderr"; then
  restore_case_status=0
else
  restore_case_status=$?
fi
set +e
bash -c 'source "$1"; acquire_workflow_lock "$2"' _ \
  "$ROOT/lib/workflow.sh" "$restore_lock" >/dev/null 2>&1
restore_actor_c_status=$?
set -e
restore_quarantine="$(find "$restore_case_dir" -maxdepth 1 -type d \
  -name 'install.lock.stale.*' -print -quit)"
if [[ "$restore_case_status" == 0 ]] || [[ "$restore_actor_c_status" == 0 ]] || \
   [[ ! -d "$restore_lock.guard" ]] || [[ -z "$restore_quarantine" ]] || \
   [[ "$(cat "$restore_quarantine/pid" 2>/dev/null || true)" != "$restore_live_pid" ]] || \
   ! rg -q 'fail.closed|guard.*retained' "$restore_case_dir/stderr"; then
  kill "$restore_live_pid" 2>/dev/null || true
  wait "$restore_live_pid" 2>/dev/null || true
  rm -rf -- "$restore_case_dir"
  WORKFLOW_LOCK_GUARD_DIR=
  WORKFLOW_LOCK_GUARD_IDENTITY=
  clear_workflow_lock_quarantine
  printf 'restoration failure did not retain the guard and block actor C\n' >&2
  exit 1
fi
kill "$restore_live_pid" 2>/dev/null || true
wait "$restore_live_pid" 2>/dev/null || true
rm -rf -- "$restore_case_dir"
WORKFLOW_LOCK_GUARD_DIR=
WORKFLOW_LOCK_GUARD_IDENTITY=
clear_workflow_lock_quarantine

guard_dir="$lock_dir.guard"
mkdir "$guard_dir"
printf 'not-a-pid\n' >"$guard_dir/pid"
printf 'stale-guard\n' >"$guard_dir/identity"
if acquire_workflow_lock "$lock_dir" 2>"$fixture/stale-guard.stderr"; then
  printf 'stale acquisition guard was automatically reclaimed\n' >&2
  exit 1
fi
[[ -d "$guard_dir" && ! -e "$lock_dir" ]]
rg -q 'stale.*guard|guard.*stale' "$fixture/stale-guard.stderr"
rm -rf -- "$guard_dir"

printf '{"data":[]}\n' >"$fixture/models-empty.json"
printf '{"data":[{"id":"gpt-test"}]}\n' >"$fixture/models.json"
printf '{"data":{}}\n' >"$fixture/models-invalid.json"
cliproxy_models_response_is_ready "$fixture/models-empty.json"
cliproxy_models_response_is_ready "$fixture/models.json"
if cliproxy_models_response_is_ready "$fixture/models-invalid.json"; then
  printf 'invalid CLIProxy readiness payload was accepted\n' >&2
  exit 1
fi

cliproxy_destination="$fixture/install/cli-proxy-api"
install -d "$fixture/release/archive" "$fixture/staging" \
  "$fixture/mock-bin" "$(dirname "$cliproxy_destination")"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "CLIProxyAPI Version: 1.2.3, Commit: test, BuiltAt: test\n"' \
  '[[ "${1:-}" == --help ]]' >"$fixture/release/archive/cli-proxy-api"
chmod 0755 "$fixture/release/archive/cli-proxy-api"
tar -czf "$fixture/release/asset.tar.gz" -C "$fixture/release/archive" cli-proxy-api
release_sha="$(sha256_file "$fixture/release/asset.tar.gz")"
printf '{"tag_name":"v1.2.3","assets":[{"name":"CLIProxyAPI_test_linux_amd64.tar.gz","browser_download_url":"https://example.invalid/asset.tar.gz","digest":"sha256:%s"}]}\n' \
  "$release_sha" >"$fixture/release/release.json"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'output=' \
  'url=' \
  'while (($#)); do' \
  '  case "$1" in' \
  '    --output) output="$2"; shift 2 ;;' \
  '    http*) url="$1"; shift ;;' \
  '    *) shift ;;' \
  '  esac' \
  'done' \
  'case "$url" in' \
  '  */releases/latest) cp "$MOCK_RELEASE_JSON" "$output" ;;' \
  '  */asset.tar.gz) cp "$MOCK_RELEASE_ARCHIVE" "$output" ;;' \
  '  *) exit 1 ;;' \
  'esac' >"$fixture/mock-bin/curl"
chmod 0755 "$fixture/mock-bin/curl"
stage_state="$(PATH="$fixture/mock-bin:$PATH" \
  MOCK_RELEASE_JSON="$fixture/release/release.json" \
  MOCK_RELEASE_ARCHIVE="$fixture/release/asset.tar.gz" \
  stage_latest_github_binary router-for-me/CLIProxyAPI CLIProxyAPI_ \
    _linux_amd64.tar.gz cli-proxy-api "$cliproxy_destination" "$fixture/staging")"
[[ "$(jq -r .version <<<"$stage_state")" == 1.2.3 ]]
[[ "$(jq -r .tag <<<"$stage_state")" == v1.2.3 ]]
[[ "$(jq -r .changed <<<"$stage_state")" == true ]]
staged_binary="$(jq -r .staged_path <<<"$stage_state")"
[[ ! -e "$cliproxy_destination" && -x "$staged_binary" ]]
activate_staged_file "$staged_binary" "$cliproxy_destination" 0755
unchanged_state="$(PATH="$fixture/mock-bin:$PATH" \
  MOCK_RELEASE_JSON="$fixture/release/release.json" \
  MOCK_RELEASE_ARCHIVE="$fixture/release/asset.tar.gz" \
  stage_latest_github_binary router-for-me/CLIProxyAPI CLIProxyAPI_ \
    _linux_amd64.tar.gz cli-proxy-api "$cliproxy_destination" "$fixture/staging-2")"
[[ "$(jq -r .changed <<<"$unchanged_state")" == false ]]
[[ "$(jq -r .tag <<<"$unchanged_state")" == v1.2.3 ]]
[[ "$(jq -r .staged_path <<<"$unchanged_state")" == null ]]

printf '#!/usr/bin/env bash\nprintf "cli-proxy-api 11.2.30\\n"\n' \
  >"$fixture/overlap-destination"
chmod 0755 "$fixture/overlap-destination"
overlap_state="$(PATH="$fixture/mock-bin:$PATH" \
  MOCK_RELEASE_JSON="$fixture/release/release.json" \
  MOCK_RELEASE_ARCHIVE="$fixture/release/asset.tar.gz" \
  stage_latest_github_binary router-for-me/CLIProxyAPI CLIProxyAPI_ \
    _linux_amd64.tar.gz cli-proxy-api "$fixture/overlap-destination" \
    "$fixture/staging-overlap")"
if [[ "$(jq -r .changed <<<"$overlap_state")" != true ]]; then
  printf 'installed overlapping semantic version was accepted\n' >&2
  exit 1
fi

printf '#!/usr/bin/env bash\nprintf "cli-proxy-api 11.2.30\\n"\n' \
  >"$fixture/release/archive/cli-proxy-api"
chmod 0755 "$fixture/release/archive/cli-proxy-api"
tar -czf "$fixture/release/overlap-asset.tar.gz" \
  -C "$fixture/release/archive" cli-proxy-api
overlap_sha="$(sha256_file "$fixture/release/overlap-asset.tar.gz")"
printf '{"tag_name":"v1.2.3","assets":[{"name":"CLIProxyAPI_test_linux_amd64.tar.gz","browser_download_url":"https://example.invalid/asset.tar.gz","digest":"sha256:%s"}]}\n' \
  "$overlap_sha" >"$fixture/release/overlap-release.json"
if PATH="$fixture/mock-bin:$PATH" \
  MOCK_RELEASE_JSON="$fixture/release/overlap-release.json" \
  MOCK_RELEASE_ARCHIVE="$fixture/release/overlap-asset.tar.gz" \
  stage_latest_github_binary router-for-me/CLIProxyAPI CLIProxyAPI_ \
    _linux_amd64.tar.gz cli-proxy-api "$fixture/missing-destination" \
    "$fixture/staging-bad-version" >/dev/null 2>&1; then
  printf 'staged overlapping semantic version was accepted\n' >&2
  exit 1
fi

bash -c '
  set -euo pipefail
  source "$1"
  workflow_cleanup_init
  trap '\''workflow_cleanup "$?"'\'' EXIT
  cleanup_root="$2"
  register_cleanup_path "$2/temp"
  mkdir -p "$2/temp"
  acquire_workflow_lock "$2/cleanup.lock"
  WORKFLOW_TRANSACTION_ACTIVE=true
  WORKFLOW_ROLLBACK_HANDLER=test_rollback
  test_rollback() { printf "rolled-back\\n" >"$cleanup_root/rollback"; }
  exit 7
' _ "$ROOT/lib/workflow.sh" "$fixture/cleanup" 2>/dev/null && {
  printf 'cleanup subprocess unexpectedly succeeded\n' >&2
  exit 1
}
[[ "$(cat "$fixture/cleanup/rollback")" == rolled-back ]]
[[ ! -e "$fixture/cleanup/temp" && ! -e "$fixture/cleanup.lock" ]]

set +e
bash -c '
  set -euo pipefail
  source "$1"
  workflow_cleanup_init
  trap '\''workflow_cleanup "$?"'\'' EXIT
  trap '\''workflow_cleanup 130'\'' INT
  signal_root="$2"
  register_cleanup_path "$2/temp"
  mkdir -p "$2/temp"
  acquire_workflow_lock "$2/signal.lock"
  WORKFLOW_TRANSACTION_ACTIVE=true
  WORKFLOW_ROLLBACK_HANDLER=test_rollback
  test_rollback() { printf "signal-rollback\\n" >"$signal_root/rollback"; }
  kill -INT "$$"
' _ "$ROOT/lib/workflow.sh" "$fixture/signal-cleanup" 2>/dev/null
signal_status=$?
set -e
[[ "$signal_status" == 130 ]]
[[ "$(cat "$fixture/signal-cleanup/rollback")" == signal-rollback ]]
[[ ! -e "$fixture/signal-cleanup/temp" && ! -e "$fixture/signal-cleanup/signal.lock" ]]

set +e
bash -c '
  set -euo pipefail
  source "$1"
  workflow_cleanup_init
  trap '\''workflow_cleanup "$?"'\'' EXIT
  trap '\''workflow_cleanup 130'\'' INT
  mkdir -p "$2"
  acquire_workflow_lock_guard "$2/held.guard"
  kill -INT "$$"
' _ "$ROOT/lib/workflow.sh" "$fixture/guard-cleanup" 2>/dev/null
guard_signal_status=$?
set -e
[[ "$guard_signal_status" == 130 ]]
[[ ! -e "$fixture/guard-cleanup/held.guard" ]]

set +e
bash -c '
  set -euo pipefail
  source "$1"
  workflow_cleanup_init
  trap '\''workflow_cleanup "$?"'\'' EXIT
  mkdir -p "$2"
  acquire_workflow_lock_guard "$2/held.guard"
  exit 9
' _ "$ROOT/lib/workflow.sh" "$fixture/guard-exit-cleanup" 2>/dev/null
guard_exit_status=$?
set -e
[[ "$guard_exit_status" == 9 ]]
[[ ! -e "$fixture/guard-exit-cleanup/held.guard" ]]

render_systemd_user_unit "$fixture/claudex.service" "$data_root"
rg -q '^Type=exec$' "$fixture/claudex.service"
rg -q '^Restart=on-failure$' "$fixture/claudex.service"
rg -q '^StandardOutput="append:' "$fixture/claudex.service"
rg -q '%%' "$fixture/claudex.service"
rg -q '\$\$' "$fixture/claudex.service"
rg -Fq 'data with %% and $$/bin/cli-proxy-api' "$fixture/claudex.service"
rg -Fq 'data with %% and $$/logs/cliproxy.log' "$fixture/claudex.service"
for repository_state_dir in runtime logs backups; do
  if rg -Fq "$ROOT/$repository_state_dir/" "$fixture/claudex.service"; then
    printf 'service definition references repository-owned %s state\n' \
      "$repository_state_dir" >&2
    exit 1
  fi
done

proxy_port=13457
HOME="$fixture/proxy-home" \
  render_claudex_proxy_launch_agent \
    "$fixture/claudex-proxy.plist" "$data_root" "$proxy_port"
python3 - "$fixture/claudex-proxy.plist" "$data_root" "$proxy_port" \
  "$fixture/proxy-home" <<'PY'
import plistlib
import sys

path, data_root, port, home = sys.argv[1:]
document = plistlib.load(open(path, "rb"))
assert document["Label"] == "com.user.claudex-translation-proxy"
assert document["ProgramArguments"] == [
    f"{data_root}/bin/claudex",
    "--config",
    f"{data_root}/model-config/current/claudex.toml",
    "proxy",
    "start",
    "--port",
    port,
]
assert document["RunAtLoad"] is True
assert document["KeepAlive"] is True
assert document["EnvironmentVariables"] == {"HOME": home}
assert document["StandardOutPath"] == f"{data_root}/logs/claudex-proxy.log"
assert document["StandardErrorPath"] == f"{data_root}/logs/claudex-proxy.log"
PY
HOME="$fixture/proxy-home" claudex_proxy_service_is_owned \
  "$fixture/claudex-proxy.plist" "$data_root"

render_claudex_proxy_systemd_user_unit \
  "$fixture/claudex-proxy.service" "$data_root" "$proxy_port"
rg -q '^Description=Claudex translation proxy$' \
  "$fixture/claudex-proxy.service"
rg -q '^Wants=claudex-headroom.service claudex-cliproxy.service$' \
  "$fixture/claudex-proxy.service"
rg -q '^After=claudex-headroom.service claudex-cliproxy.service$' \
  "$fixture/claudex-proxy.service"
rg -q '^Type=exec$' "$fixture/claudex-proxy.service"
rg -Fq "ExecStart=$(systemd_quote "$data_root/bin/claudex") --config $(systemd_quote "$data_root/model-config/current/claudex.toml") proxy start --port $proxy_port" \
  "$fixture/claudex-proxy.service"
rg -q '^Restart=always$' "$fixture/claudex-proxy.service"
rg -q '^RestartSec=3$' "$fixture/claudex-proxy.service"
rg -Fq "StandardOutput=$(systemd_quote "append:$data_root/logs/claudex-proxy.log")" \
  "$fixture/claudex-proxy.service"
claudex_proxy_service_is_owned \
  "$fixture/claudex-proxy.service" "$data_root"

printf '%s\n' \
  '[Unit]' \
  'Description=Claudex translation proxy' \
  '[Service]' \
  "# ExecStart=$(systemd_quote "$data_root/bin/claudex") --config $(systemd_quote "$data_root/model-config/current/claudex.toml") proxy start --port $proxy_port" \
  'ExecStart=/foreign/claudex proxy start --port 13457' \
  >"$fixture/claudex-proxy-spoof.service"
if claudex_proxy_service_is_owned \
    "$fixture/claudex-proxy-spoof.service" "$data_root"; then
  printf 'comment-spoofed Claudex proxy service was accepted\n' >&2
  exit 1
fi

special_proxy_root="$fixture/proxy data & < \"quoted\" \\slash % \$"
HOME="$fixture/proxy home & < \"user\" \\ % \$" \
  render_claudex_proxy_launch_agent \
    "$fixture/claudex-proxy-special.plist" "$special_proxy_root" 13457
python3 - "$fixture/claudex-proxy-special.plist" <<'PY'
import plistlib
import sys
plistlib.load(open(sys.argv[1], "rb"))
PY
render_claudex_proxy_systemd_user_unit \
  "$fixture/claudex-proxy-special.service" "$special_proxy_root" 13457
rg -Fq "$(systemd_quote "$special_proxy_root/bin/claudex")" \
  "$fixture/claudex-proxy-special.service"
claudex_proxy_service_is_owned \
  "$fixture/claudex-proxy-special.service" "$special_proxy_root"
repeated_dollar_home="$fixture/home \$\$ literal"
HOME="$repeated_dollar_home" render_claudex_proxy_systemd_user_unit \
  "$fixture/claudex-proxy-dollar-home.service" "$data_root" 13457
HOME="$repeated_dollar_home" claudex_proxy_service_is_owned \
  "$fixture/claudex-proxy-dollar-home.service" "$data_root"
python3 - "$fixture/claudex-proxy-special.service" \
  "$fixture/claudex-proxy-noncanonical.service" <<'PY'
import sys

source, destination = sys.argv[1:]
text = open(source, encoding="utf-8").read()
open(destination, "w", encoding="utf-8").write(
    text.replace("%%", "%").replace("$$", "$")
)
PY
if claudex_proxy_service_is_owned \
    "$fixture/claudex-proxy-noncanonical.service" "$special_proxy_root"; then
  printf 'non-canonically escaped systemd service was accepted\n' >&2
  exit 1
fi
if render_claudex_proxy_systemd_user_unit \
    "$fixture/claudex-proxy-leading-zero.service" "$data_root" 013456; then
  printf 'leading-zero proxy port was rendered\n' >&2
  exit 1
fi

identity_home="$fixture/identity-home"
IFS=$'\t' read -r identity_file identity_label identity_unit \
  < <(HOME="$identity_home" claudex_proxy_service_identity darwin)
[[ "$identity_file" == \
  "$identity_home/Library/LaunchAgents/com.user.claudex-translation-proxy.plist" ]]
[[ "$identity_label" == com.user.claudex-translation-proxy ]]
[[ "$identity_unit" == - ]]
IFS=$'\t' read -r identity_file identity_label identity_unit \
  < <(HOME="$identity_home" XDG_CONFIG_HOME="$fixture/xdg-config" \
    claudex_proxy_service_identity systemd)
[[ "$identity_file" == \
  "$fixture/xdg-config/systemd/user/claudex-translation-proxy.service" ]]
[[ "$identity_label" == - ]]
[[ "$identity_unit" == claudex-translation-proxy.service ]]
if claudex_proxy_service_identity unsupported >/dev/null 2>&1; then
  printf 'unsupported service platform was accepted\n' >&2
  exit 1
fi

pid_tools="$fixture/pid-tools"
install -d "$pid_tools"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  '[[ "${FAKE_TARGET_LOADED:-1}" == 1 ]] || exit "${FAKE_TARGET_STATUS:-113}"' \
  'printf "service = {\\n  path = %s\\n  pid = %s\\n}\\n" "${FAKE_SERVICE_PATH:-/fixture/proxy.plist}" "${FAKE_SERVICE_PID:-0}"' \
  >"$pid_tools/launchctl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *LoadState* ]]; then printf "%s\\n" "${FAKE_LOAD_STATE:-loaded}"; elif [[ "$*" == *FragmentPath* ]]; then printf "%s\\n" "${FAKE_SERVICE_PATH:-/fixture/proxy.service}"; else printf "%s\\n" "${FAKE_SERVICE_PID:-0}"; fi' \
  >"$pid_tools/systemctl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\\n" "$FAKE_LISTENER_PID"' \
  >"$pid_tools/lsof"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\\n" "$FAKE_UNAME"' \
  >"$pid_tools/uname"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\\n" "$FAKE_SS_OUTPUT"' \
  >"$pid_tools/ss"
chmod 0755 "$pid_tools/launchctl" "$pid_tools/systemctl" \
  "$pid_tools/lsof" "$pid_tools/uname" "$pid_tools/ss"

[[ "$(FAKE_SERVICE_PID=4242 PATH="$pid_tools:$PATH" \
  managed_service_main_pid darwin com.user.claudex-translation-proxy -)" == \
  4242 ]]
[[ "$(FAKE_SERVICE_PID=4343 PATH="$pid_tools:$PATH" \
  managed_service_main_pid systemd - claudex-translation-proxy.service)" == \
  4343 ]]
FAKE_TARGET_LOADED=1 PATH="$pid_tools:$PATH" \
  managed_service_target_is_loaded \
    darwin com.user.claudex-translation-proxy -
if FAKE_TARGET_LOADED=0 PATH="$pid_tools:$PATH" \
    managed_service_target_is_loaded \
      darwin com.user.claudex-translation-proxy -; then
  printf 'absent launchd target was reported loaded\n' >&2
  exit 1
fi
[[ "$(FAKE_TARGET_LOADED=0 FAKE_TARGET_STATUS=113 PATH="$pid_tools:$PATH" \
  managed_service_target_state \
    darwin com.user.claudex-translation-proxy -)" == absent ]]
if FAKE_TARGET_LOADED=0 FAKE_TARGET_STATUS=70 PATH="$pid_tools:$PATH" \
    managed_service_target_state \
      darwin com.user.claudex-translation-proxy - >/dev/null 2>&1; then
  printf 'launchd inspection error was reported absent\n' >&2
  exit 1
fi
[[ "$(FAKE_LOAD_STATE=not-found PATH="$pid_tools:$PATH" \
  managed_service_target_state \
    systemd - claudex-translation-proxy.service)" == absent ]]
if FAKE_LOAD_STATE=failed PATH="$pid_tools:$PATH" \
    managed_service_target_state \
      systemd - claudex-translation-proxy.service >/dev/null 2>&1; then
  printf 'systemd inspection error was reported absent\n' >&2
  exit 1
fi
[[ "$(FAKE_SERVICE_PID=0 PATH="$pid_tools:$PATH" \
  managed_service_main_pid_value \
    darwin com.user.claudex-translation-proxy -)" == 0 ]]
[[ "$(FAKE_SERVICE_PID=0 PATH="$pid_tools:$PATH" \
  managed_service_main_pid_value \
    systemd - claudex-translation-proxy.service)" == 0 ]]
FAKE_LOAD_STATE=loaded PATH="$pid_tools:$PATH" \
  managed_service_target_is_loaded \
    systemd - claudex-translation-proxy.service
if FAKE_LOAD_STATE=not-found PATH="$pid_tools:$PATH" \
    managed_service_target_is_loaded \
      systemd - claudex-translation-proxy.service; then
  printf 'absent systemd target was reported loaded\n' >&2
  exit 1
fi
[[ "$(FAKE_SERVICE_PATH=/fixture/proxy.plist PATH="$pid_tools:$PATH" \
  managed_service_definition_path \
    darwin com.user.claudex-translation-proxy -)" == \
  /fixture/proxy.plist ]]
[[ "$(FAKE_SERVICE_PATH=/fixture/proxy.service PATH="$pid_tools:$PATH" \
  managed_service_definition_path \
    systemd - claudex-translation-proxy.service)" == \
  /fixture/proxy.service ]]
FAKE_UNAME=Darwin FAKE_LISTENER_PID=4242 PATH="$pid_tools:$PATH" \
  pid_owns_loopback_listener 4242 13457
if FAKE_UNAME=Darwin FAKE_LISTENER_PID=99 PATH="$pid_tools:$PATH" \
    pid_owns_loopback_listener 4242 13457; then
  printf 'listener owned by another Darwin PID was accepted\n' >&2
  exit 1
fi
linux_listener='LISTEN 0 128 127.0.0.1:13457 0.0.0.0:* users:(("claudex",pid=4343,fd=7))'
FAKE_UNAME=Linux FAKE_SS_OUTPUT="$linux_listener" PATH="$pid_tools:$PATH" \
  pid_owns_loopback_listener 4343 13457
if FAKE_UNAME=Linux \
    FAKE_SS_OUTPUT='LISTEN 0 128 0.0.0.0:13457 0.0.0.0:* users:(("claudex",pid=4343,fd=7))' \
    PATH="$pid_tools:$PATH" pid_owns_loopback_listener 4343 13457; then
  printf 'wildcard listener was accepted as loopback-only\n' >&2
  exit 1
fi
if FAKE_UNAME=Linux \
    FAKE_SS_OUTPUT='LISTEN 0 128 127.0.0.1:13457 0.0.0.0:*' \
    PATH="$pid_tools:$PATH" pid_owns_loopback_listener 4343 13457; then
  printf 'listener without process metadata was accepted\n' >&2
  exit 1
fi
if FAKE_UNAME=Darwin PATH="/usr/bin:/bin" \
    pid_owns_loopback_listener 4242 13457; then
  printf 'missing lsof was accepted\n' >&2
  exit 1
fi

jq -n '{object:"list",data:[{id:"gpt-5.6-sol"}]}' \
  >"$fixture/claudex-proxy-models.json"
printf '%s\n' 'default_model = "gpt-5.6-sol"' \
  >"$fixture/claudex-default-model.toml"
[[ "$(claudex_config_default_model \
  "$fixture/claudex-default-model.toml")" == gpt-5.6-sol ]]
claudex_proxy_models_response_is_ready \
  "$fixture/claudex-proxy-models.json" gpt-5.6-sol
if claudex_proxy_models_response_is_ready \
    "$fixture/claudex-proxy-models.json" gpt-5.6-terra; then
  printf 'missing controller model was accepted\n' >&2
  exit 1
fi
rg -Fq 'for command_name in launchctl plutil lsof' "$ROOT/install.sh"
rg -Fq 'missing required command: ss' "$ROOT/install.sh"
rg -Fq 'Claudex proxy PID/listener inspection requires lsof' "$ROOT/doctor.sh"
rg -Fq 'Claudex proxy PID/listener inspection requires ss' "$ROOT/doctor.sh"

resolved_data_root="$(HOME="$fixture/home" XDG_DATA_HOME="$fixture/xdg" \
  CLAUDEX_DATA_DIR= workflow_data_dir)"
[[ "$resolved_data_root" == "$fixture/xdg/claudex-workflow" ]]

if mutable_refs="$(rg -n '\$WORKFLOW_ROOT/(runtime|logs|backups)|\$WORKFLOW_ROOT/bin/(claudex|cli-proxy-api)"' \
  "$ROOT/install.sh" "$ROOT/discover-models.sh" "$ROOT/doctor.sh" \
  "$ROOT/bin/claudex-gpt" "$ROOT/bin/claudex-login" | \
  rg -v 'LEGACY_RUNTIME_ROOT=' || true)" && [[ -n "$mutable_refs" ]]; then
  printf '%s\n' "$mutable_refs" >&2
  printf 'mutable state still points inside the Git checkout\n' >&2
  exit 1
fi

render_headroom_launch_agent \
  "$fixture/headroom.plist" "$data_root" /portable/bin/headroom /portable/ca.pem 18787
rg -Fq '/portable/bin/headroom' "$fixture/headroom.plist"
rg -q '<string>com.user.claudex-headroom</string>' "$fixture/headroom.plist"
rg -q '<string>18787</string>' "$fixture/headroom.plist"
rg -q '<string>token</string>' "$fixture/headroom.plist"
rg -q '<string>--lossless</string>' "$fixture/headroom.plist"
rg -q '<string>--code-aware</string>' "$fixture/headroom.plist"
rg -q '<key>HEADROOM_OUTPUT_SHAPER</key>' "$fixture/headroom.plist"
rg -q '<key>HEADROOM_VERBOSITY_AUTOTUNE</key>' "$fixture/headroom.plist"
rg -q '<key>HEADROOM_EFFORT_ROUTER</key>' "$fixture/headroom.plist"

special_root="$fixture/data & < \"quoted\" \\slash % \$"
special_binary="$special_root/bin/headroom & < \"binary\" \\ % \$"
special_ca="$special_root/ca & < \"bundle\" \\ % \$.pem"
HOME="$special_root/home & < \"user\" \\ % \$" \
  render_headroom_launch_agent \
    "$fixture/headroom-special.plist" "$special_root" \
    "$special_binary" "$special_ca" 18787
python3 - "$fixture/headroom-special.plist" <<'PY'
import sys
from xml.etree import ElementTree
ElementTree.parse(sys.argv[1])
PY
rg -q '&amp;' "$fixture/headroom-special.plist"
rg -q '&lt;' "$fixture/headroom-special.plist"
rg -q '&quot;' "$fixture/headroom-special.plist"

HOME="$special_root/home & < \"user\" \\ % \$" \
  render_launch_agent "$fixture/cliproxy-special.plist" "$special_root"
python3 - "$fixture/cliproxy-special.plist" <<'PY'
import sys
from xml.etree import ElementTree
ElementTree.parse(sys.argv[1])
PY
rg -q '&amp;' "$fixture/cliproxy-special.plist"
rg -q '&lt;' "$fixture/cliproxy-special.plist"
rg -q '&quot;' "$fixture/cliproxy-special.plist"

render_headroom_systemd_user_unit \
  "$fixture/headroom.service" "$data_root" /portable/bin/headroom /portable/ca.pem 18787
rg -q '^Description=Claudex Headroom proxy$' "$fixture/headroom.service"
rg -q -- '--port 18787' "$fixture/headroom.service"
rg -q -- '--mode token' "$fixture/headroom.service"
rg -q -- '--lossless' "$fixture/headroom.service"
rg -q '^Environment="HEADROOM_OUTPUT_SHAPER=0"$' "$fixture/headroom.service"
render_headroom_systemd_user_unit \
  "$fixture/headroom-special.service" "$special_root" \
  "$special_binary" "$special_ca" 18787
rg -Fq "ExecStart=$(systemd_quote "$special_binary") proxy" \
  "$fixture/headroom-special.service"
for dynamic_environment in \
  "HEADROOM_CONFIG_DIR=$special_root/headroom/config" \
  "HEADROOM_WORKSPACE_DIR=$special_root/headroom/state" \
  "SSL_CERT_FILE=$special_ca"
do
  python3 - "$fixture/headroom-special.service" "$dynamic_environment" <<'PY'
import sys

unit_path, expected = sys.argv[1:]
encoded = None
for line in open(unit_path, encoding="utf-8"):
    if line.startswith('Environment="') and line.rstrip("\n").endswith('"'):
        candidate = line.rstrip("\n")[13:-1]
        decoded = []
        index = 0
        while index < len(candidate):
            if candidate.startswith("%%", index):
                decoded.append("%")
                index += 2
            elif candidate[index] == "\\":
                index += 1
                if index == len(candidate):
                    raise SystemExit("trailing systemd escape")
                decoded.append(candidate[index])
                index += 1
            else:
                decoded.append(candidate[index])
                index += 1
        if "".join(decoded) == expected:
            encoded = candidate
            break
if encoded is None:
    raise SystemExit(f"literal Environment value not preserved: {expected!r}")
if "$" in expected and "$$" in encoded:
    raise SystemExit("Environment value incorrectly doubled a literal dollar")
PY
done
rg -Fq "StandardOutput=$(systemd_quote "append:$special_root/logs/headroom.log")" \
  "$fixture/headroom-special.service"
if rg -q 'headroom install apply' "$ROOT/install.sh"; then
  printf 'installer still delegates Headroom service management\n' >&2
  exit 1
fi
rg -q 'snapshot_path .*headroom-service' "$ROOT/install.sh"
rg -q 'snapshot_path .*headroom-models' "$ROOT/install.sh"
rg -Fq 'snapshot_path_matches "$headroom_service_file"' "$ROOT/install.sh"
rg -Fq 'snapshot_path_matches "$headroom_models_file"' "$ROOT/install.sh"
rg -Fq 'HEADROOM_CONFIG_DIR="$preflight_root/config"' "$ROOT/install.sh"
rg -Fq '"$desired_headroom_models" "$headroom_models_file" 0600' \
  "$ROOT/install.sh"
rg -Fq -- '--expected-version "$installed_cliproxy_version"' "$ROOT/doctor.sh"
rg -Fq 'uv tool install --upgrade mempalace' "$ROOT/install.sh"
rg -Fq -- '--require-tool mempalace_get_taxonomy' "$ROOT/install.sh"
rg -Fq -- '--require-tool mempalace_checkpoint' "$ROOT/install.sh"
rg -Fq "uv tool install --upgrade 'graphifyy[mcp,terraform]'" "$ROOT/install.sh"
rg -Fq 'integrations/common/mcp_probe.py' "$ROOT/install.sh"
rg -Fq 'integrations/common/mcp_probe.py' "$ROOT/doctor.sh"
rg -Fq 'integrations/common/context_population.py' "$ROOT/doctor.sh"
rg -Fq -- '--require-tool query_graph' "$ROOT/install.sh"
rg -Fq -- '--require-tool graph_stats' "$ROOT/doctor.sh"
rg -Fq -- '--require-tool mempalace_get_taxonomy' "$ROOT/doctor.sh"
rg -Fq -- '--require-tool mempalace_checkpoint' "$ROOT/doctor.sh"
rg -Fq 'claudex-audit-model-does-not-exist' "$ROOT/doctor.sh"
rg -Fq 'X-Headroom-Base-Url: http://127.0.0.1:$CLIPROXY_PORT' "$ROOT/doctor.sh"
for claudex_proxy_doctor_check in \
  'Claudex proxy definition is workflow-owned' \
  'Claudex proxy service PID owns 127.0.0.1:$CLAUDEX_PROXY_PORT' \
  'Claudex proxy exposes the configured controller model'
do
  rg -Fq "$claudex_proxy_doctor_check" "$ROOT/doctor.sh"
done
rg -Fq 'http://127.0.0.1:$CLAUDEX_PROXY_PORT/v1/models' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh"
rg -Fq 'claudex_proxy_models_response_is_ready' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh"
if rg -q -e 'launchctl|systemctl|service_(start|stop)|managed_service_(start|stop)' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh"; then
  printf 'SessionStart health hook mutates service-manager state\n' >&2
  exit 1
fi
health_hook_fixture="$fixture/session-health-hook"
health_hook_data="$health_hook_fixture/data"
health_hook_tools="$health_hook_fixture/tools"
install -d -m 0700 "$health_hook_data" "$health_hook_tools"
write_service_ports "$health_hook_data" 18317 18787 13457
printf '%s\n' \
  '[model]' \
  'default_model = "gpt-5.6-sol"' \
  >"$health_hook_data/claudex.toml"
cat >"$health_hook_tools/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url="${!#}"
case "$url" in
  http://127.0.0.1:18787/health)
    marker=headroom
    printf '%s\n' '{"service":"headroom-proxy","status":"healthy","ready":true}'
    ;;
  http://127.0.0.1:18317/v1/models)
    marker=cliproxy
    printf '%s\n' '{"data":[{"id":"gpt-5.6-sol"},{"id":"claude-opus-4-8"}]}'
    ;;
  http://127.0.0.1:13457/v1/models)
    marker=claudex
    if [[ "${HOOK_CLAUDEX_MODE:-ready}" == ready ]]; then
      printf '%s\n' '{"object":"list","data":[{"id":"gpt-5.6-sol"}]}'
    else
      printf '%s\n' '{"object":"list","data":[]}'
    fi
    ;;
  *) exit 22 ;;
esac
if [[ "${HOOK_REQUIRE_CONCURRENCY:-0}" == 1 ]]; then
  : >"$HOOK_BARRIER_DIR/$marker"
  for _ in {1..50}; do
    [[ -e "$HOOK_BARRIER_DIR/headroom" && \
       -e "$HOOK_BARRIER_DIR/cliproxy" && \
       -e "$HOOK_BARRIER_DIR/claudex" ]] && exit 0
    /bin/sleep 0.02
  done
  exit 23
fi
EOF
chmod 0755 "$health_hook_tools/curl"
health_hook_barrier="$health_hook_fixture/barrier"
install -d -m 0700 "$health_hook_barrier"
health_hook_output="$(
  CLAUDEX_DATA_DIR="$health_hook_data" \
  CLAUDEX_WORKFLOW_ROOT="$ROOT" \
  HOOK_REQUIRE_CONCURRENCY=1 \
  HOOK_BARRIER_DIR="$health_hook_barrier" \
  PATH="$health_hook_tools:$PATH" \
    "$ROOT/controller/plugin/scripts/check-local-services.sh"
)"
[[ -z "$health_hook_output" ]]
health_hook_output="$(
  CLAUDEX_DATA_DIR="$health_hook_data" \
  CLAUDEX_WORKFLOW_ROOT="$ROOT" \
  HOOK_CLAUDEX_MODE=missing \
  PATH="$health_hook_tools:$PATH" \
    "$ROOT/controller/plugin/scripts/check-local-services.sh"
)"
[[ "$(wc -l <<<"$health_hook_output" | tr -d ' ')" == 1 ]]
jq -e '
  (.systemMessage | type == "string") and
  (.systemMessage | contains("persistent Claudex proxy"))
' <<<"$health_hook_output" >/dev/null

doctor_proxy_fixture="$fixture/doctor-proxy"
doctor_proxy_home="$doctor_proxy_fixture/home"
doctor_proxy_data="$doctor_proxy_fixture/data"
doctor_proxy_xdg="$doctor_proxy_fixture/xdg"
doctor_proxy_tools="$doctor_proxy_fixture/tools"
doctor_proxy_service="$doctor_proxy_xdg/systemd/user/claudex-translation-proxy.service"
install -d -m 0700 \
  "$doctor_proxy_home" "$doctor_proxy_data" "$doctor_proxy_tools" \
  "$doctor_proxy_data/auth" "$doctor_proxy_data/state/sessions"
install -d -m 0755 "$doctor_proxy_data/bin" "$(dirname "$doctor_proxy_service")"
write_service_ports "$doctor_proxy_data" 18317 18787 13457
printf '%s\n' 'default_model = "gpt-5.6-sol"' \
  >"$doctor_proxy_data/claudex.toml"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' \
  >"$doctor_proxy_data/bin/claudex"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "${1:-}" == --help ]]; then' \
  '  printf "%s\n" "CLIProxyAPI Version: 1.0.0"' \
  'fi' \
  >"$doctor_proxy_data/bin/cli-proxy-api"
chmod 0755 "$doctor_proxy_data/bin/claudex" \
  "$doctor_proxy_data/bin/cli-proxy-api"
install -d -m 0700 "$doctor_proxy_data/headroom/config"
jq -n '{
  schemaVersion: 1,
  source: {
    repository: "router-for-me/CLIProxyAPI",
    tag: "v1.0.0",
    version: "1.0.0",
    registrySha256:
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  anthropic: {context_limits: {"gpt-5.6-sol": 200000}}
}' >"$doctor_proxy_data/headroom/config/models.json"
chmod 0600 "$doctor_proxy_data/headroom/config/models.json"
HOME="$doctor_proxy_home" render_claudex_proxy_systemd_user_unit \
  "$doctor_proxy_service" "$doctor_proxy_data" 13457
cat >"$doctor_proxy_tools/uname" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' Linux
EOF
cat >"$doctor_proxy_tools/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *LoadState* ]]; then
  printf '%s\n' "${DOCTOR_TARGET_STATE:-loaded}"
elif [[ "$*" == *FragmentPath* ]]; then
  printf '%s\n' "$DOCTOR_SERVICE_PATH"
elif [[ "$*" == *MainPID* ]]; then
  printf '%s\n' "${DOCTOR_MANAGER_PID:-4242}"
else
  exit 1
fi
EOF
cat >"$doctor_proxy_tools/ss" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
pid="${DOCTOR_LISTENER_PID:-4242}"
printf 'LISTEN 0 128 127.0.0.1:13457 0.0.0.0:* users:(("claudex",pid=%s,fd=7))\n' "$pid"
printf 'LISTEN 0 128 127.0.0.1:18317 0.0.0.0:* users:(("cliproxy",pid=4343,fd=7))\n'
printf 'LISTEN 0 128 127.0.0.1:18787 0.0.0.0:* users:(("headroom",pid=4444,fd=7))\n'
EOF
cat >"$doctor_proxy_tools/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url="${!#}"
case "$url" in
  http://127.0.0.1:13457/v1/models)
    if [[ "${DOCTOR_MODEL_MODE:-ready}" == ready ]]; then
      printf '%s\n' '{"object":"list","data":[{"id":"gpt-5.6-sol"}]}'
    else
      printf '%s\n' '{"object":"list","data":[]}'
    fi
    ;;
  http://127.0.0.1:18317/v1/models)
    printf '%s\n' '{"data":[{"id":"gpt-5.6-sol"},{"id":"claude-opus-4-8"}]}'
    ;;
  http://127.0.0.1:18787/health)
    printf '%s\n' '{"version":"fixture","ready":true,"config":{"optimize":true,"cache":false,"memory":false,"code_graph":false,"runtime_env":{"HEADROOM_OUTPUT_SHAPER":"0","HEADROOM_VERBOSITY_AUTOTUNE":"0","HEADROOM_EFFORT_ROUTER":"0"}}}'
    ;;
  http://127.0.0.1:18317/v1/messages|http://127.0.0.1:18787/v1/messages)
    printf '%s\n' '{"error":{"message":"unknown provider for model claudex-audit-model-does-not-exist"}}'
    ;;
  *) exit 22 ;;
esac
EOF
chmod 0755 "$doctor_proxy_tools"/*

run_proxy_doctor_case() {
  local case_name="$1"
  shift
  env \
    HOME="$doctor_proxy_home" \
    CLAUDEX_DATA_DIR="$doctor_proxy_data" \
    XDG_CONFIG_HOME="$doctor_proxy_xdg" \
    DOCTOR_SERVICE_PATH="$doctor_proxy_service" \
    PATH="$doctor_proxy_tools:/usr/bin:/bin:/opt/homebrew/bin" \
    "$@" "$ROOT/doctor.sh" \
      >"$doctor_proxy_fixture/$case_name.output" 2>&1 || :
}

run_proxy_doctor_case healthy
rg -Fxq 'OK   Claudex proxy definition is workflow-owned' \
  "$doctor_proxy_fixture/healthy.output"
rg -Fxq 'OK   Claudex proxy service PID owns 127.0.0.1:13457' \
  "$doctor_proxy_fixture/healthy.output"
rg -Fxq 'OK   Claudex proxy exposes the configured controller model' \
  "$doctor_proxy_fixture/healthy.output"
rg -Fxq 'OK   Headroom model metadata matches CLIProxyAPI 1.0.0' \
  "$doctor_proxy_fixture/healthy.output"

run_proxy_doctor_case definition-failure DOCTOR_TARGET_STATE=not-found
rg -Fxq 'FAIL Claudex proxy definition is workflow-owned' \
  "$doctor_proxy_fixture/definition-failure.output"
rg -Fxq 'OK   Claudex proxy service PID owns 127.0.0.1:13457' \
  "$doctor_proxy_fixture/definition-failure.output"
rg -Fxq 'OK   Claudex proxy exposes the configured controller model' \
  "$doctor_proxy_fixture/definition-failure.output"

run_proxy_doctor_case pid-failure DOCTOR_MANAGER_PID=99
rg -Fxq 'OK   Claudex proxy definition is workflow-owned' \
  "$doctor_proxy_fixture/pid-failure.output"
rg -Fxq 'FAIL Claudex proxy service PID owns 127.0.0.1:13457' \
  "$doctor_proxy_fixture/pid-failure.output"
rg -Fxq 'OK   Claudex proxy exposes the configured controller model' \
  "$doctor_proxy_fixture/pid-failure.output"

run_proxy_doctor_case model-failure DOCTOR_MODEL_MODE=missing
rg -Fxq 'OK   Claudex proxy definition is workflow-owned' \
  "$doctor_proxy_fixture/model-failure.output"
rg -Fxq 'OK   Claudex proxy service PID owns 127.0.0.1:13457' \
  "$doctor_proxy_fixture/model-failure.output"
rg -Fxq 'FAIL Claudex proxy exposes the configured controller model' \
  "$doctor_proxy_fixture/model-failure.output"

install -d -m 0755 \
  "$doctor_proxy_data/state/sessions/run.unsafe-newest"
run_proxy_doctor_case unsafe-newest-session
rg -Fxq 'FAIL latest session effective mapping is internally inconsistent' \
  "$doctor_proxy_fixture/unsafe-newest-session.output"
if rg -Fxq 'OK   no prior effective session to inspect' \
    "$doctor_proxy_fixture/unsafe-newest-session.output"; then
  printf 'doctor skipped the newest owned session with an unsafe mode\n' >&2
  exit 1
fi
rg -Fq 'UV_TOOL_DIR="$WORKFLOW_DATA_ROOT/headroom/tools"' "$ROOT/install.sh"
rg -Fq 'UV_TOOL_BIN_DIR="$WORKFLOW_DATA_ROOT/headroom/bin"' "$ROOT/install.sh"
rg -Fq 'com.user.claudex-headroom' "$ROOT/install.sh"
rg -Fq 'claudex-headroom.service' "$ROOT/install.sh"
rg -Fq 'select_service_port' "$ROOT/install.sh"
rg -Fq 'legacy_headroom_service_owned' "$ROOT/install.sh"
rg -Fq 'refusing to overwrite unknown service file' "$ROOT/install.sh"
rg -Fq 'print_install_summary' "$ROOT/install.sh"
rg -Fq 'wait_for_cliproxy "$PRIOR_CLIPROXY_PORT"' "$ROOT/install.sh"
rg -Fq '"$legacy_headroom_running_version" "$PRIOR_HEADROOM_PORT"' \
  "$ROOT/install.sh"
rg -Fq '"$headroom_service_file" "$WORKFLOW_DATA_ROOT" new' "$ROOT/install.sh"
rg -Fq '"$legacy_headroom_service_file" "$WORKFLOW_DATA_ROOT" legacy' \
  "$ROOT/install.sh"
rg -Fq 'upgrade_headroom_distribution' "$ROOT/install.sh"
rg -Fq 'reconcile_headroom_transaction' "$ROOT/install.sh"
rg -Fq 'preflight_headroom_binary' "$ROOT/install.sh"
for proxy_install_contract in \
  claudex_proxy_service_is_owned \
  claudex_proxy_listener_owned \
  claudex_proxy_transaction_active \
  claudex_proxy_runtime_mutated \
  preflight_claudex_proxy \
  wait_for_claudex_proxy \
  restore_claudex_proxy_service \
  pending-provider-login \
  'com.user.claudex-translation-proxy' \
  'claudex-translation-proxy.service'
do
  rg -Fq "$proxy_install_contract" "$ROOT/install.sh"
done
rg -Fq 'MODEL_DISCOVERY_LOGIN_INCOMPLETE=42' "$ROOT/discover-models.sh"
rg -Fq 'model discovery and publication are installer-owned' \
  "$ROOT/discover-models.sh"
if "$ROOT/discover-models.sh" \
    >"$fixture/direct-discovery.stdout" \
    2>"$fixture/direct-discovery.stderr"; then
  printf 'direct model discovery bypassed the installer transaction\n' >&2
  exit 1
fi
[[ ! -s "$fixture/direct-discovery.stdout" ]]
rg -Fq 'run ' "$fixture/direct-discovery.stderr"
rg -Fq '/install.sh' "$fixture/direct-discovery.stderr"
rg -Fq '"$MODEL_DISCOVERY_LOGIN_INCOMPLETE"' \
  "$ROOT/install.sh"
rg -Fq 'refusing to stop ownership-drifted Claudex proxy runtime' \
  "$ROOT/install.sh"
rg -Fq 'refusing to replace loaded unknown Claudex proxy target' \
  "$ROOT/install.sh"
rg -Fq 'claudex_proxy_binary_changed' "$ROOT/install.sh"
rg -Fq 'claudex_proxy_service_changed' "$ROOT/install.sh"
rg -Fq 'claudex_proxy_port_changed' "$ROOT/install.sh"
rg -Fq 'claudex_proxy_config_changed' "$ROOT/install.sh"
rg -Fq 'claudex_proxy_readiness_drifted' "$ROOT/install.sh"
rg -Fq 'claudex-login <installed-oauth-provider>; %s/install.sh' \
  "$ROOT/install.sh"
rg -Fq 'claudex-models' "$ROOT/install.sh" "$ROOT/doctor.sh"
rg -Fq 'load_routing' "$ROOT/install.sh" "$ROOT/doctor.sh"
rg -Fq 'snapshot_path "$USER_BIN_DIR/claudex-models"' "$ROOT/install.sh"
rg -Fq 'restore_snapshot "$USER_BIN_DIR/claudex-models"' "$ROOT/install.sh"
rg -Fq 'default model stack' "$ROOT/doctor.sh"
rg -Fq 'latest session effective mapping is internally consistent' \
  "$ROOT/doctor.sh"
! rg -q 'GPT model|Claude model|claude-opus-4-8 discovery' "$ROOT/doctor.sh"
for readme_contract in \
  'one persistent Claudex translation proxy' \
  'CLAUDEX_PROXY_PORT=13457' \
  'com.user.claudex-translation-proxy' \
  'claudex-translation-proxy.service' \
  'may interrupt one in-flight request' \
  'existing sessions do not own or terminate the proxy lifetime'
do
  rg -Fiq "$readme_contract" "$ROOT/README.md"
done
python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
snapshot = source.index('snapshot_path "$claudex_proxy_service_file"')
port_write = source.index('write_service_ports "$WORKFLOW_DATA_ROOT"', snapshot)
publication = source.index('"$discovery_entrypoint"', port_write)
manager_guard = source.index('claudex_proxy_manager_target_state=')
preflight = source.index('preflight_claudex_proxy ||', publication)
cutover = source.index('activate_staged_file "$claudex_proxy_desired_service_file"', preflight)
readiness = source.index('wait_for_claudex_proxy', cutover)
release = source.index('release_endpoint_config_lock', readiness)
disarm = source.index('claudex_proxy_transaction_active=false', release)
if not manager_guard < port_write:
    raise SystemExit("loaded proxy manager target is checked after publication")
if not snapshot < port_write < publication < preflight < cutover < readiness < release < disarm:
    raise SystemExit("persistent proxy transaction ordering is unsafe")
PY
python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
rollback = source.index("rollback_install_transaction()")
restore_endpoint = source.index('restore_model_config_generation', rollback)
restore_proxy = source.index('restore_claudex_proxy_service', restore_endpoint)
release = source.index('release_endpoint_config_lock', restore_proxy)
if not restore_endpoint < restore_proxy < release:
    raise SystemExit("rollback releases endpoint lock before proxy recovery")

activation = source.index('if [[ "$claudex_proxy_restart_required" == true ]]')
enable = source.index('launchctl enable', activation)
bootstrap = source.index('launchctl bootstrap', activation)
if not enable < bootstrap:
    raise SystemExit("launchd activation bootstraps a disabled job")

recovery = source.index('restore_claudex_proxy_service()')
enable = source.index('launchctl enable', recovery)
bootstrap = source.index('launchctl bootstrap', recovery)
if not enable < bootstrap:
    raise SystemExit("launchd recovery bootstraps a disabled job")

preflight = source.index('preflight_claudex_proxy ||', activation)
safety = source.index('claudex_proxy_prior_runtime_safe_to_stop', preflight)
arm = source.index('claudex_proxy_transaction_active=true', safety)
runtime_mutation = source.index('claudex_proxy_runtime_mutated=true', arm)
stop = source.index('launchctl bootout', runtime_mutation)
if not preflight < safety < arm < runtime_mutation < stop:
    raise SystemExit("proxy rollback is armed before ownership revalidation")

rollback = source.index('rollback_install_transaction()')
runtime_guard = source.index(
    '${claudex_proxy_runtime_mutated:-false}', rollback
)
rollback_stop = source.index('launchctl bootout', runtime_guard)
if not rollback < runtime_guard < rollback_stop:
    raise SystemExit("rollback can stop a runtime this transaction did not mutate")

safety_start = source.index('claudex_proxy_prior_runtime_safe_to_stop()')
safety_end = source.index('cliproxy_listener_owned=false', safety_start)
safety_body = source[safety_start:safety_end]
target_check_start = source.index('claudex_proxy_loaded_target_is_expected()')
target_check_end = source.index(
    'claudex_proxy_prior_runtime_safe_to_stop()', target_check_start
)
target_check_body = source[target_check_start:target_check_end]
if (
    'claudex_proxy_loaded_target_is_expected' not in safety_body
    or 'managed_service_definition_path' not in target_check_body
    or 'managed_service_target_state' not in target_check_body
    or 'managed_service_main_pid_value' not in safety_body
):
    raise SystemExit("proxy cutover does not revalidate the loaded definition")

rollback_body = source[rollback:source.index('WORKFLOW_ROLLBACK_HANDLER', rollback)]
if 'claudex_proxy_loaded_target_is_expected' not in rollback_body:
    raise SystemExit("proxy rollback can stop a definition that drifted after cutover")

systemd_recovery = source.index('systemctl --user daemon-reload', recovery)
systemd_enable = source.index('systemctl --user enable', systemd_recovery)
enable_gate = source.rfind('if [[ "$recovery_ready" == true ]]', systemd_recovery, systemd_enable)
systemd_restart = source.index('systemctl --user restart', systemd_enable)
restart_gate = source.rfind('if [[ "$recovery_ready" == true ]]', systemd_enable, systemd_restart)
if enable_gate < systemd_recovery or restart_gate < systemd_enable:
    raise SystemExit("systemd proxy recovery is not failure-gated")
PY
python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
version_read = source.index("headroom_current_version=")
reconcile = source.index("reconcile_headroom_transaction", version_read)
disarm = source.index("headroom_transaction_active=false", reconcile)
cliproxy_activation = source.index(
    'if [[ "$cliproxy_binary_changed" == true ]]', version_read
)
discovery = source.index('source "$WORKFLOW_ROOT/discover-models.sh"', version_read)
if not version_read < reconcile < cliproxy_activation < discovery < disarm:
    raise SystemExit(
        "Headroom rollback is not kept armed through CLIProxy activation"
    )
preflight = source.index('preflight_headroom_binary ||', reconcile)
legacy_stop = source.index('legacy_headroom_service_owned', preflight)
if not reconcile < preflight < legacy_stop:
    raise SystemExit("Headroom preflight does not precede legacy service cutover")
PY
python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()

headroom = source.split(
    'if [[ "$headroom_restart_required" == true ]]; then', 1
)[1].split(
    'if [[ "$cliproxy_binary_changed" == true ]]', 1
)[0]
for stop, start in (
    (
        'launchctl bootout "gui/$(id -u)" "$headroom_service_file"',
        'launchctl bootstrap "gui/$(id -u)" "$headroom_service_file"',
    ),
    (
        'systemctl --user stop claudex-headroom.service',
        'systemctl --user start claudex-headroom.service',
    ),
):
    if not (
        headroom.index(stop)
        < headroom.index('require_activation_port_available Headroom "$HEADROOM_PORT"')
        < headroom.index(start)
    ):
        raise SystemExit("Headroom port is not rechecked after stop and before start")

cliproxy = source.split(
    'if [[ "$cliproxy_restart_required" == true ]]; then', 1
)[1].split(
    'for launcher in claudex-gpt', 1
)[0]
for stop, start in (
    (
        'launchctl bootout "gui/$(id -u)" "$service_file"',
        'launchctl bootstrap "gui/$(id -u)" "$service_file"',
    ),
    (
        'systemctl --user stop claudex-cliproxy.service',
        'systemctl --user start claudex-cliproxy.service',
    ),
):
    if not (
        cliproxy.index(stop)
        < cliproxy.index(
            'require_activation_port_available CLIProxyAPI "$CLIPROXY_PORT"'
        )
        < cliproxy.index(start)
    ):
        raise SystemExit("CLIProxyAPI port is not rechecked after stop and before start")
PY
python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
snapshot = source.index('snapshot_path "$service_ports_path"')
write = source.index('write_service_ports "$WORKFLOW_DATA_ROOT"')
discovery = source.index(
    'CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint" ||', write
)
restore = source.index('restore_snapshot "$service_ports_path"')
disarm = source.index('endpoint_transaction_active=false', discovery)
if not snapshot < restore < write < discovery < disarm:
    raise SystemExit("endpoint publication is not rollback-safe")
failure_window = source[discovery:disarm]
for required_guard in (
    '[[ "$ports_changed" == false ]]',
    '[[ "$claudex_binary_changed" == false ]]',
    '[[ "$claudex_proxy_service_changed" == unchanged ]]',
    '[[ "$claudex_proxy_listener_owned" == true ]]',
    'model discovery failed while persistent proxy reconciliation was required',
):
    if required_guard not in failure_window:
        raise SystemExit("model discovery fatality matrix is incomplete")
if 'CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint"' not in source[write:disarm]:
    raise SystemExit("installer discovery can prune endpoint rollback state")
if 'restore_model_config_generation' not in source[snapshot:write]:
    raise SystemExit("endpoint rollback does not restore model publication")
if 'prior_model_generation_snapshot' not in source[snapshot:write]:
    raise SystemExit("prior model generation is not privately snapshotted")
acquire_endpoint = source.index('acquire_endpoint_config_lock', snapshot)
capture_prior = source.index('prior_model_generation="$(readlink', acquire_endpoint)
release_endpoint = source.index('release_endpoint_config_lock', discovery)
if not acquire_endpoint < capture_prior < write < discovery < release_endpoint < disarm:
    raise SystemExit("endpoint model publication is not serialized through commit")
overall_commit = source.index('WORKFLOW_TRANSACTION_ACTIVE=false', release_endpoint)
if not release_endpoint < overall_commit:
    raise SystemExit("cleanup is disarmed before the endpoint lock is released")
prune = source.index('prune_model_config_generations', disarm)
if not disarm < prune:
    raise SystemExit("prior model generation is pruned before endpoint commit")
PY
rg -Fq 'headroom --version' "$ROOT/install.sh"
rg -q 'interrupt active Claudex sessions' "$ROOT/install.sh"
rg -q 'claudex-context' "$ROOT/install.sh"
rg -Fq '"$WORKFLOW_ROOT/bin/claudex-plugin" sync' "$ROOT/install.sh"
rg -q 'for launcher in .*claudex-plugin' "$ROOT/install.sh"
rg -q 'for launcher in .*claudex-headroom' "$ROOT/install.sh"

python3 - "$ROOT/README.md" <<'PY'
import sys

readme = open(sys.argv[1], encoding="utf-8").read()
normalized_readme = " ".join(readme.split())
required_headings = [
    "Why use it",
    "How a request flows",
    "What happens automatically",
    "Install and upgrade",
    "Daily use",
    "Manage Claudex plugins",
    "Manage workspace contexts",
    "Project MCP behavior",
    "State and safety",
    "Diagnose, test, and rollback",
    "Upstream projects",
]
positions = []
for heading in required_headings:
    marker = f"## {heading}"
    position = readme.find(marker)
    if position < 0:
        raise SystemExit(f"README is missing required section: {heading}")
    positions.append(position)
if positions != sorted(positions):
    raise SystemExit("README sections are not in the required operational order")

required_copy = [
    "Sol",
    "Terra",
    "Sonnet",
    "Opus",
    "sol-builder",
    "CLIProxyAPI",
    "Headroom",
    "--lossless",
    "--code-aware",
    "HEADROOM_OUTPUT_SHAPER=0",
    "HEADROOM_VERBOSITY_AUTOTUNE=0",
    "HEADROOM_EFFORT_ROUTER=0",
    "./install.sh",
    "safe to rerun",
    "interrupt active Claudex sessions",
    "claudex-context list",
    "claudex-context add",
    "claudex-context update",
    "claudex-context remove",
    "claudex-context validate",
    "claudex-plugin add",
    "claudex-plugin list",
    "claudex-plugin sync",
    "claudex-plugin update",
    "claudex-plugin remove",
    "controller/plugins.json",
    "installed plugin agents remain subject to the orchestration allowlist",
    "plugin-provided MCP servers remain subject to strict per-session MCP configuration",
    "Docker MCP",
    "MemPalace",
    "graphify-out/graph.json",
    "CLAUDEX_DATA_DIR",
    "claudex-doctor",
    "./smoke-test.sh gpt",
    "./smoke-test.sh claude",
    "./smoke-test.sh controller",
    "./rollback.sh",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "read-only repository reconnaissance",
    "independent verification",
    "model-diverse correctness and regression criticism",
    "high-risk read-only adjudication",
    "no persistent backups",
    "LaunchAgent",
    "systemd",
    "model generation",
    "claudex-context populate",
    "mines each outermost canonical repository",
    "skips linked worktrees when their primary checkout is already in the context root",
    "a skipped worktree nested inside a canonical memory source aborts before MemPalace starts",
    "repositories cloned later",
    "--code-only",
    "does not run as a service",
    "Repository discovery and elapsed heartbeats are visible by default",
    "there is no `--verbose` mode to enable",
]
for phrase in required_copy:
    if phrase not in normalized_readme:
        raise SystemExit(f"README is missing required operational detail: {phrase}")

flow_section = readme.split("## How a request flows", 1)[1]
flow_block = flow_section.split("```text", 1)[1].split("```", 1)[0]
expected_flow = """many claudex-gpt sessions
  -> one persistent Claudex translation proxy
  -> selected model: Sol | Terra | Sonnet | Opus
  -> Headroom
  -> CLIProxyAPI
     -> Codex OAuth (GPT)
     -> Claude OAuth (Claude)"""
if expected_flow not in flow_block:
    raise SystemExit(
        "README request flow must connect selected model calls through "
        "Headroom and CLIProxyAPI before provider routing"
    )
PY
for documented_runtime_detail in \
  CLAUDEX_CLIPROXY_PORT CLAUDEX_HEADROOM_PORT service-ports.json \
  com.user.claudex-headroom claudex-headroom.service \
  'Installation locations'
do
  rg -Fq "$documented_runtime_detail" "$ROOT/README.md"
done

printf 'PASS: portable installer surface\n'
