#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for script in \
  install.sh doctor.sh rollback.sh smoke-test.sh discover-models.sh \
  bin/claudex-gpt bin/claudex-login bin/claudex-doctor bin/claude-headroom \
  bin/claudex-headroom bin/claudex-context bin/claudex-plugin \
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
for invalid_port in '' 0 1023 65536 abc '8317 '; do
  if valid_service_port "$invalid_port"; then
    printf 'invalid service port was accepted: %q\n' "$invalid_port" >&2
    exit 1
  fi
done

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 18787
[[ "$(read_service_ports "$ports_root")" == $'18317\t18787' ]]
[[ "$(python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' \
  "$(service_ports_file "$ports_root")")" == 600 ]]
[[ "$(read_service_ports "$fixture/no-ports-yet")" == $'8317\t8787' ]]
printf '{"cliproxyPort": 8787, "headroomPort": 8787}\n' \
  >"$(service_ports_file "$ports_root")"
if read_service_ports "$ports_root" >/dev/null 2>&1; then
  printf 'duplicate service ports were accepted\n' >&2
  exit 1
fi
write_service_ports "$ports_root" 18317 18787

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
[[ "$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" 0 true false)" == \
  "$occupied_port" ]]
if select_service_port \
    CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" 0 false false \
    >"$fixture/noninteractive-port.stdout" 2>"$fixture/noninteractive-port.stderr"; then
  printf 'non-interactive foreign port collision was accepted\n' >&2
  exit 1
fi
rg -Fq "port $occupied_port is occupied" "$fixture/noninteractive-port.stderr"
rg -Fq 'CLAUDEX_CLIPROXY_PORT' "$fixture/noninteractive-port.stderr"
[[ "$(select_service_port \
  CLIProxyAPI CLAUDEX_CLIPROXY_PORT "$occupied_port" 0 false true \
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
  /portable/headroom.service 18317 18787 reused reconciled)"
for summary_value in \
  '/portable/checkout' '/portable/data' '/portable/user-bin' \
  '/portable/data/bin/claudex' '/portable/data/bin/cli-proxy-api' \
  '/portable/data/headroom/bin/headroom' '/portable/user-bin/mempalace-mcp' \
  '/portable/user-bin/graphify-mcp' '/portable/cliproxy.service' \
  '/portable/headroom.service' '127.0.0.1:18317' '127.0.0.1:18787' \
  'reused' 'reconciled'
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
  'true unchanged true' \
  'false changed true' \
  'false unchanged false'
do
  headroom_transaction_active=false
  # shellcheck disable=SC2086
  reconcile_headroom_transaction $restart_case
  [[ "$headroom_restart_required" == true ]]
  [[ "$headroom_transaction_active" == true ]]
done
headroom_transaction_active=true
reconcile_headroom_transaction false unchanged true
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
      reconcile_headroom_transaction false unchanged true
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
    reconcile_headroom_transaction false unchanged true
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
rg -Fq 'snapshot_path_matches "$headroom_service_file"' "$ROOT/install.sh"
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
    'if ! CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint"; then', write
)
restore = source.index('restore_snapshot "$service_ports_path"')
disarm = source.index('endpoint_transaction_active=false', discovery)
if not snapshot < restore < write < discovery < disarm:
    raise SystemExit("endpoint publication is not rollback-safe")
if '[[ "$ports_changed" == true && -n "$prior_model_generation" ]]' not in source[discovery:disarm]:
    raise SystemExit("changed ports do not make model discovery failure fatal")
if 'CLAUDEX_DEFER_MODEL_PRUNE=1 "$discovery_entrypoint"' not in source[write:disarm]:
    raise SystemExit("installer discovery can prune endpoint rollback state")
if 'restore_model_config_generation' not in source[snapshot:write]:
    raise SystemExit("endpoint rollback does not restore model publication")
if 'prior_model_generation_snapshot' not in source[snapshot:write]:
    raise SystemExit("prior model generation is not privately snapshotted")
acquire_endpoint = source.index('acquire_endpoint_config_lock', snapshot)
capture_prior = source.index('prior_model_generation="$(readlink', acquire_endpoint)
release_endpoint = source.index('release_endpoint_config_lock', discovery)
if not acquire_endpoint < capture_prior < write < discovery < disarm < release_endpoint:
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
expected_flow = """claudex-gpt
  -> Sol routing decision
  -> selected model call: Sol (gpt-5.6-sol) | Terra (gpt-5.6-terra) | Sonnet (claude-sonnet-5) | Opus (claude-opus-4-8)
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
