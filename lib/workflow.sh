#!/usr/bin/env bash

workflow_die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

physical_pwd() {
  pwd -P
}

workflow_data_dir() {
  local data_root="${CLAUDEX_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/claudex-workflow}"
  case "$data_root" in
    /*) printf '%s' "$data_root" ;;
    *) workflow_die "CLAUDEX_DATA_DIR must be an absolute path" ;;
  esac
}

service_ports_file() {
  printf '%s/service-ports.json' "$1"
}

valid_service_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  ((10#$port >= 1024 && 10#$port <= 65535))
}

read_service_ports() {
  local data_root="$1"
  local ports_file
  ports_file="$(service_ports_file "$data_root")"
  if [[ ! -e "$ports_file" ]]; then
    printf '8317\t8787\n'
    return 0
  fi
  [[ -f "$ports_file" && ! -L "$ports_file" ]] || return 1
  jq -er '
    select(type == "object" and keys == ["cliproxyPort", "headroomPort"]) |
    select(.cliproxyPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(.headroomPort | type == "number" and floor == . and
      . >= 1024 and . <= 65535) |
    select(.cliproxyPort != .headroomPort) |
    [.cliproxyPort, .headroomPort] | @tsv
  ' "$ports_file"
}

write_service_ports() {
  local data_root="$1"
  local cliproxy_port="$2"
  local headroom_port="$3"
  local ports_file temporary
  valid_service_port "$cliproxy_port" || return 1
  valid_service_port "$headroom_port" || return 1
  [[ "$cliproxy_port" != "$headroom_port" ]] || return 1
  install -d -m 0700 "$data_root" || return 1
  ports_file="$(service_ports_file "$data_root")"
  [[ ! -L "$ports_file" ]] || return 1
  temporary="$(mktemp "$data_root/.service-ports.XXXXXX")" || return 1
  if ! jq -n --argjson cliproxy "$cliproxy_port" \
      --argjson headroom "$headroom_port" \
      '{cliproxyPort: $cliproxy, headroomPort: $headroom}' >"$temporary" || \
     ! chmod 0600 "$temporary" || ! mv -f "$temporary" "$ports_file"; then
    rm -f -- "$temporary"
    return 1
  fi
}

port_is_available() {
  local port="$1"
  valid_service_port "$port" || return 1
  python3 - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
listener = socket.socket()
try:
    listener.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    listener.close()
PY
}

next_available_port() {
  local occupied_port="$1"
  local reserved_port="${2:-0}"
  valid_service_port "$occupied_port" || return 1
  if [[ "$reserved_port" != 0 ]]; then
    valid_service_port "$reserved_port" || return 1
  fi
  python3 - "$occupied_port" "$reserved_port" <<'PY'
import socket
import sys

start, reserved = map(int, sys.argv[1:])
ports = range(start + 1, 65536)
for port in ports:
    if port == reserved:
        continue
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        listener.close()
    print(port)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

select_service_port() {
  local service_name="$1"
  local override_name="$2"
  local desired_port="$3"
  local reserved_port="$4"
  local owned_listener="$5"
  local interactive="$6"
  local suggested_port selected_port

  valid_service_port "$desired_port" || return 1
  if [[ "$reserved_port" != 0 ]]; then
    valid_service_port "$reserved_port" || return 1
    [[ "$desired_port" != "$reserved_port" ]] || return 1
  fi
  if port_is_available "$desired_port" || [[ "$owned_listener" == true ]]; then
    printf '%s\n' "$desired_port"
    return 0
  fi
  suggested_port="$(next_available_port "$desired_port" "$reserved_port")" || {
    workflow_die "$service_name port $desired_port is occupied and no later port is available"
    return 1
  }
  if [[ "$interactive" != true ]]; then
    workflow_die "$service_name port $desired_port is occupied; set $override_name to an available port (suggested: $suggested_port)"
    return 1
  fi

  while true; do
    printf '%s port %s is occupied. Port to use [%s]: ' \
      "$service_name" "$desired_port" "$suggested_port" >&2
    IFS= read -r selected_port || return 1
    selected_port="${selected_port:-$suggested_port}"
    if ! valid_service_port "$selected_port"; then
      printf 'Port must be an integer from 1024 through 65535.\n' >&2
      continue
    fi
    if [[ "$selected_port" == "$reserved_port" ]]; then
      printf 'Port %s is reserved by the other Claudex service.\n' \
        "$selected_port" >&2
      continue
    fi
    if ! port_is_available "$selected_port"; then
      printf 'Port %s is also occupied.\n' "$selected_port" >&2
      continue
    fi
    printf '%s\n' "$selected_port"
    return 0
  done
}

print_install_summary() {
  local workflow_root="$1"
  local data_root="$2"
  local user_bin_dir="$3"
  local claudex_binary="$4"
  local cliproxy_binary="$5"
  local headroom_binary="$6"
  local mempalace_binary="$7"
  local graphify_binary="$8"
  local cliproxy_service_file="$9"
  local headroom_service_file="${10}"
  local cliproxy_port="${11}"
  local headroom_port="${12}"
  local cliproxy_action="${13}"
  local headroom_action="${14}"

  printf '%s\n' \
    '' \
    'Installation locations' \
    "  Workflow checkout: $workflow_root" \
    "  Workflow data:     $data_root" \
    "  Launcher links:    $user_bin_dir -> $workflow_root/bin" \
    "  Claudex runtime:   $claudex_binary" \
    "  CLIProxyAPI:       $cliproxy_binary" \
    "  Headroom:          $headroom_binary" \
    "  MemPalace MCP:     $mempalace_binary" \
    "  Graphify MCP:      $graphify_binary" \
    '' \
    'Services' \
    "  CLIProxyAPI: $cliproxy_action at 127.0.0.1:$cliproxy_port" \
    "    $cliproxy_service_file" \
    "  Headroom:    $headroom_action at 127.0.0.1:$headroom_port" \
    "    $headroom_service_file"
}

service_definition_is_owned() {
  local service_file="$1"
  local data_root="$2"
  local service_kind="$3"
  local ownership_mode="${4:-either}"
  [[ -f "$service_file" && ! -L "$service_file" ]] || return 1
  python3 - "$service_file" "$data_root" "$service_kind" "$ownership_mode" <<'PY'
import os
import plistlib
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
data_root = sys.argv[2]
kind = sys.argv[3]
mode = sys.argv[4]


def valid_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return False
    return str(port) == str(value) and 1024 <= port <= 65535


def headroom_arguments_owned(arguments):
    if not isinstance(arguments, list) or len(arguments) != 12:
        return False
    executable = arguments[0]
    if not os.path.isabs(executable) or os.path.basename(executable) != "headroom":
        return False
    if mode == "new" and executable != f"{data_root}/headroom/bin/headroom":
        return False
    port = arguments[5]
    return valid_port(port) and arguments[1:] == [
        "proxy",
        "--host", "127.0.0.1",
        "--port", port,
        "--mode", "token",
        "--no-cache",
        "--intercept-tool-results",
        "--lossless",
        "--code-aware",
    ]


raw = path.read_bytes()
if b"<plist" in raw[:500]:
    document = plistlib.loads(raw)
    arguments = document.get("ProgramArguments")
    if kind == "cliproxy":
        owned = (
            document.get("Label") == "com.user.claudex-cliproxy"
            and arguments == [
                f"{data_root}/bin/cli-proxy-api",
                "--config",
                f"{data_root}/cliproxy.yaml",
            ]
        )
    else:
        labels = {
            "new": {"com.user.claudex-headroom"},
            "legacy": {"com.user.headroom-proxy"},
            "either": {"com.user.claudex-headroom", "com.user.headroom-proxy"},
        }
        environment = document.get("EnvironmentVariables")
        owned = (
            document.get("Label") in labels[mode]
            and headroom_arguments_owned(arguments)
            and isinstance(environment, dict)
            and environment.get("HEADROOM_CONFIG_DIR") == f"{data_root}/headroom/config"
            and environment.get("HEADROOM_WORKSPACE_DIR") == f"{data_root}/headroom/state"
        )
    raise SystemExit(0 if owned else 1)

lines = [
    line.strip() for line in raw.decode("utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith(("#", ";"))
]


def decoded_words(value):
    return shlex.split(value.replace("%%", "%").replace("$$", "$"))


exec_lines = [line[len("ExecStart="):] for line in lines if line.startswith("ExecStart=")]
if len(exec_lines) != 1:
    raise SystemExit(1)
try:
    arguments = decoded_words(exec_lines[0])
except ValueError:
    raise SystemExit(1)

if kind == "cliproxy":
    expected = [
        f"{data_root}/bin/cli-proxy-api",
        "--config",
        f"{data_root}/cliproxy.yaml",
    ]
    raise SystemExit(0 if arguments == expected else 1)

descriptions = [line for line in lines if line.startswith("Description=")]
expected_descriptions = {
    "new": {"Description=Claudex Headroom proxy"},
    "legacy": {"Description=Headroom proxy for Claudex"},
    "either": {
        "Description=Claudex Headroom proxy",
        "Description=Headroom proxy for Claudex",
    },
}
environment = {}
for line in lines:
    if not line.startswith("Environment="):
        continue
    try:
        values = decoded_words(line[len("Environment="):])
    except ValueError:
        raise SystemExit(1)
    for value in values:
        if "=" in value:
            key, item = value.split("=", 1)
            environment[key] = item
owned = (
    len(descriptions) == 1
    and descriptions[0] in expected_descriptions[mode]
    and headroom_arguments_owned(arguments)
    and environment.get("HEADROOM_CONFIG_DIR") == f"{data_root}/headroom/config"
    and environment.get("HEADROOM_WORKSPACE_DIR") == f"{data_root}/headroom/state"
)
raise SystemExit(0 if owned else 1)
PY
}

cliproxy_service_is_owned() {
  service_definition_is_owned "$1" "$2" cliproxy
}

headroom_service_is_owned() {
  service_definition_is_owned "$1" "$2" headroom "${3:-either}"
}

validated_workflow_data_dir() {
  local checkout_root="$1"
  local data_root="${CLAUDEX_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/claudex-workflow}"
  python3 - "$data_root" "$HOME" "$checkout_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw, home_raw, checkout_raw = sys.argv[1:]
if not os.path.isabs(raw):
    raise SystemExit("CLAUDEX_DATA_DIR must be an absolute path")

normalized = Path(os.path.normpath(raw))
cursor = Path(normalized.anchor)
parts = normalized.parts[1:]
for index, component in enumerate(parts):
    cursor /= component
    try:
        value = os.lstat(cursor)
    except FileNotFoundError:
        break
    except OSError as error:
        raise SystemExit("CLAUDEX_DATA_DIR existing ancestor is inaccessible") from error
    if stat.S_ISLNK(value.st_mode):
        raise SystemExit("CLAUDEX_DATA_DIR existing ancestors must not be symlinks")
    if index < len(parts) - 1 and not stat.S_ISDIR(value.st_mode):
        raise SystemExit("CLAUDEX_DATA_DIR existing ancestor is not a directory")

candidate = normalized.resolve(strict=False)
home = Path(home_raw).resolve(strict=False)
checkout = Path(checkout_raw).resolve(strict=True)
root = Path(candidate.anchor)
try:
    candidate.relative_to(checkout)
except ValueError:
    inside_checkout = False
else:
    inside_checkout = True
if candidate in (root, home) or inside_checkout:
    raise SystemExit("refusing unsafe CLAUDEX_DATA_DIR")
print(candidate, end="")
PY
}

workflow_cleanup_init() {
  WORKFLOW_CLEANUP_PATHS=()
  WORKFLOW_LOCK_DIR=
  WORKFLOW_LOCK_IDENTITY=
  WORKFLOW_LOCK_GUARD_DIR=
  WORKFLOW_LOCK_GUARD_IDENTITY=
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=false
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  WORKFLOW_LOCK_QUARANTINE_DIR=
  WORKFLOW_LOCK_QUARANTINE_CANONICAL=
  WORKFLOW_TRANSACTION_ACTIVE=false
  WORKFLOW_ROLLBACK_HANDLER=
}

register_cleanup_path() {
  local cleanup_path="$1"
  case "$cleanup_path" in
    ''|/) workflow_die "refusing unsafe cleanup path" ;;
    *) WORKFLOW_CLEANUP_PATHS+=("$cleanup_path") ;;
  esac
}

workflow_cleanup() {
  local status="${1:-0}"
  local cleanup_path rollback_status=0 quarantine_status=0
  trap - EXIT INT TERM HUP

  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]]; then
    resolve_workflow_lock_quarantine || quarantine_status=$?
  fi

  if [[ "${WORKFLOW_TRANSACTION_ACTIVE:-false}" == true ]] && \
     [[ -n "${WORKFLOW_ROLLBACK_HANDLER:-}" ]]; then
    "$WORKFLOW_ROLLBACK_HANDLER" || rollback_status=$?
  fi

  for cleanup_path in "${WORKFLOW_CLEANUP_PATHS[@]:-}"; do
    [[ -n "$cleanup_path" ]] && rm -rf -- "$cleanup_path"
  done
  if ((quarantine_status == 0)); then
    release_workflow_lock "${WORKFLOW_LOCK_DIR:-}" || true
    release_workflow_lock_guard "${WORKFLOW_LOCK_GUARD_DIR:-}" || true
  else
    printf 'ERROR: installer lock quarantine recovery failed; acquisition guard retained (fail-closed)\n' >&2
  fi

  if ((status == 0 && rollback_status != 0)); then
    status="$rollback_status"
  fi
  if ((status == 0 && quarantine_status != 0)); then
    status="$quarantine_status"
  fi
  exit "$status"
}

clear_workflow_lock_quarantine() {
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=false
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  WORKFLOW_LOCK_QUARANTINE_DIR=
  WORKFLOW_LOCK_QUARANTINE_CANONICAL=
}

resolve_workflow_lock_quarantine() {
  local quarantine_dir="${WORKFLOW_LOCK_QUARANTINE_DIR:-}"
  local canonical_dir="${WORKFLOW_LOCK_QUARANTINE_CANONICAL:-}"
  local guard_dir="$canonical_dir.guard"
  local quarantined_pid quarantined_identity
  local restored_pid restored_identity

  [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]] || return 0
  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" != "$guard_dir" ]] || \
     [[ ! -d "$guard_dir" ]] || \
     [[ "$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)" != "$$" ]] || \
     [[ "$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)" != \
        "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]]; then
    workflow_die "cannot resolve installer lock quarantine without its owned acquisition guard"
    return 1
  fi
  if [[ "${WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED:-false}" != true ]]; then
    if [[ -n "$quarantine_dir" ]]; then
      rm -rf -- "$quarantine_dir" || {
        workflow_die "could not remove verified stale lock quarantine $quarantine_dir"
        return 1
      }
    fi
    clear_workflow_lock_quarantine
    return 0
  fi

  if [[ ! -d "$quarantine_dir" ]]; then
    workflow_die "quarantined installer lock is missing; acquisition guard retained"
    return 1
  fi
  if [[ -e "$canonical_dir" || -L "$canonical_dir" ]]; then
    workflow_die "canonical installer lock is occupied; quarantined owner and acquisition guard retained"
    return 1
  fi

  quarantined_pid="$(sed -n '1p' "$quarantine_dir/pid" 2>/dev/null || true)"
  quarantined_identity="$(sed -n '1p' "$quarantine_dir/identity" 2>/dev/null || true)"
  quarantined_identity="${quarantined_identity:-legacy-pid-only}"
  if ! mv "$quarantine_dir" "$canonical_dir" 2>/dev/null; then
    workflow_die "could not restore quarantined installer lock; acquisition guard retained"
    return 1
  fi

  restored_pid="$(sed -n '1p' "$canonical_dir/pid" 2>/dev/null || true)"
  restored_identity="$(sed -n '1p' "$canonical_dir/identity" 2>/dev/null || true)"
  restored_identity="${restored_identity:-legacy-pid-only}"
  if [[ "$restored_pid" != "$quarantined_pid" ]] || \
     [[ "$restored_identity" != "$quarantined_identity" ]]; then
    workflow_die "restored installer lock identity could not be verified; acquisition guard retained"
    return 1
  fi

  clear_workflow_lock_quarantine
}

acquire_workflow_lock_guard() {
  local guard_dir="$1"
  local owner_pid guard_identity

  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" == "$guard_dir" ]] && \
     [[ -d "$guard_dir" ]] && \
     [[ "$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)" == "$$" ]] && \
     [[ "$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)" == \
        "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]]; then
    return 0
  fi

  if ! mkdir "$guard_dir" 2>/dev/null; then
    owner_pid="$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && kill -0 "$owner_pid" 2>/dev/null; then
      workflow_die \
        "installer lock acquisition guard is busy (pid $owner_pid); retry"
    else
      workflow_die \
        "stale installer lock acquisition guard found at $guard_dir; remove it manually and retry"
    fi
    return 1
  fi

  guard_identity="$$:$RANDOM:$RANDOM"
  printf '%s\n' "$guard_identity" >"$guard_dir/identity"
  printf '%s\n' "$$" >"$guard_dir/pid"
  WORKFLOW_LOCK_GUARD_DIR="$guard_dir"
  WORKFLOW_LOCK_GUARD_IDENTITY="$guard_identity"
}

release_workflow_lock_guard() {
  local guard_dir="${1:-}"
  local owner_pid owner_identity
  [[ -n "$guard_dir" && -d "$guard_dir" ]] || return 0
  owner_pid="$(sed -n '1p' "$guard_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$guard_dir/identity" 2>/dev/null || true)"
  [[ "$owner_pid" == "$$" ]] || return 0
  [[ -n "${WORKFLOW_LOCK_GUARD_IDENTITY:-}" ]] || return 0
  [[ "$owner_identity" == "$WORKFLOW_LOCK_GUARD_IDENTITY" ]] || return 0
  rm -rf -- "$guard_dir"
  if [[ "${WORKFLOW_LOCK_GUARD_DIR:-}" == "$guard_dir" ]]; then
    WORKFLOW_LOCK_GUARD_DIR=
    WORKFLOW_LOCK_GUARD_IDENTITY=
  fi
}

acquire_workflow_lock() {
  local lock_dir="$1"
  local guard_dir="$lock_dir.guard"
  local owner_pid owner_identity stale_lock
  local quarantined_pid quarantined_identity lock_identity reclamation_error
  local failed_move_pid failed_move_identity

  acquire_workflow_lock_guard "$guard_dir" || return 1
  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]]; then
    if ! resolve_workflow_lock_quarantine; then
      workflow_die "installer lock remains fail-closed; acquisition guard retained"
      return 1
    fi
  fi

  if mkdir "$lock_dir" 2>/dev/null; then
    lock_identity="$$:$RANDOM:$RANDOM"
    printf '%s\n' "$lock_identity" >"$lock_dir/identity"
    printf '%s\n' "$$" >"$lock_dir/pid"
    WORKFLOW_LOCK_DIR="$lock_dir"
    WORKFLOW_LOCK_IDENTITY="$lock_identity"
    release_workflow_lock_guard "$guard_dir"
    return 0
  fi

  owner_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
  owner_identity="${owner_identity:-legacy-pid-only}"
  if [[ ! "$owner_pid" =~ ^[0-9]+$ ]]; then
    release_workflow_lock_guard "$guard_dir" || true
    workflow_die "installer lock has no valid owner: $lock_dir"
    return 1
  fi
  if kill -0 "$owner_pid" 2>/dev/null; then
    release_workflow_lock_guard "$guard_dir" || true
    workflow_die "another installer owns $lock_dir (pid $owner_pid)"
    return 1
  fi

  stale_lock="$lock_dir.stale.$$.$RANDOM"
  while [[ -e "$stale_lock" || -L "$stale_lock" ]]; do
    stale_lock="$lock_dir.stale.$$.$RANDOM"
  done
  WORKFLOW_LOCK_QUARANTINE_ACTIVE=true
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=true
  WORKFLOW_LOCK_QUARANTINE_DIR="$stale_lock"
  WORKFLOW_LOCK_QUARANTINE_CANONICAL="$lock_dir"
  if ! mv "$lock_dir" "$stale_lock" 2>/dev/null; then
    if [[ -d "$stale_lock" ]]; then
      if resolve_workflow_lock_quarantine; then
        release_workflow_lock_guard "$guard_dir" || true
        workflow_die "installer lock rename reported failure after quarantine; owner restored"
      else
        workflow_die "installer lock rename/restoration ambiguous; acquisition guard retained (fail-closed)"
      fi
      return 1
    fi

    if [[ -d "$lock_dir" ]] && [[ ! -e "$stale_lock" && ! -L "$stale_lock" ]]; then
      failed_move_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
      failed_move_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
      failed_move_identity="${failed_move_identity:-legacy-pid-only}"
      if { [[ "$failed_move_pid" == "$owner_pid" ]] && \
           [[ "$failed_move_identity" == "$owner_identity" ]]; } || \
         { [[ "$failed_move_pid" =~ ^[0-9]+$ ]] && \
           kill -0 "$failed_move_pid" 2>/dev/null; }; then
        clear_workflow_lock_quarantine
        release_workflow_lock_guard "$guard_dir" || true
        workflow_die "installer lock rename failed without moving the observed owner"
        return 1
      fi
    fi

    workflow_die "installer lock rename result is ambiguous; acquisition guard retained (fail-closed)"
    return 1
  fi
  quarantined_pid="$(sed -n '1p' "$stale_lock/pid" 2>/dev/null || true)"
  quarantined_identity="$(sed -n '1p' "$stale_lock/identity" 2>/dev/null || true)"
  quarantined_identity="${quarantined_identity:-legacy-pid-only}"
  if [[ "$quarantined_pid" != "$owner_pid" ]] || \
     [[ "$quarantined_identity" != "$owner_identity" ]] || \
     kill -0 "$quarantined_pid" 2>/dev/null; then
    if resolve_workflow_lock_quarantine; then
      reclamation_error="installer lock owner changed during stale reclamation"
      release_workflow_lock_guard "$guard_dir" || true
    else
      reclamation_error="installer lock restoration failed; acquisition guard retained (fail-closed)"
    fi
    workflow_die "$reclamation_error"
    return 1
  fi
  if ! mkdir "$lock_dir" 2>/dev/null; then
    if resolve_workflow_lock_quarantine; then
      release_workflow_lock_guard "$guard_dir" || true
      workflow_die "could not claim canonical installer lock; quarantined owner restored"
    else
      workflow_die "could not claim or restore installer lock; acquisition guard retained (fail-closed)"
    fi
    return 1
  fi
  lock_identity="$$:$RANDOM:$RANDOM"
  printf '%s\n' "$lock_identity" >"$lock_dir/identity"
  printf '%s\n' "$$" >"$lock_dir/pid"
  WORKFLOW_LOCK_DIR="$lock_dir"
  WORKFLOW_LOCK_IDENTITY="$lock_identity"
  WORKFLOW_LOCK_QUARANTINE_RESTORE_REQUIRED=false
  rm -rf -- "$stale_lock"
  clear_workflow_lock_quarantine
  release_workflow_lock_guard "$guard_dir"
}

release_workflow_lock() {
  local lock_dir="${1:-}"
  local guard_dir
  local owner_pid owner_identity
  [[ -n "$lock_dir" ]] || return 0
  guard_dir="$lock_dir.guard"
  acquire_workflow_lock_guard "$guard_dir" || return 1
  if [[ "${WORKFLOW_LOCK_QUARANTINE_ACTIVE:-false}" == true ]] && \
     ! resolve_workflow_lock_quarantine; then
    workflow_die "installer lock remains fail-closed; acquisition guard retained"
    return 1
  fi
  if [[ ! -d "$lock_dir" ]]; then
    release_workflow_lock_guard "$guard_dir"
    return 0
  fi
  owner_pid="$(sed -n '1p' "$lock_dir/pid" 2>/dev/null || true)"
  owner_identity="$(sed -n '1p' "$lock_dir/identity" 2>/dev/null || true)"
  if [[ "$owner_pid" == "$$" ]] && \
     [[ -n "${WORKFLOW_LOCK_IDENTITY:-}" ]] && \
     [[ "$owner_identity" == "$WORKFLOW_LOCK_IDENTITY" ]]; then
    rm -rf -- "$lock_dir"
    if [[ "${WORKFLOW_LOCK_DIR:-}" == "$lock_dir" ]]; then
      WORKFLOW_LOCK_DIR=
      WORKFLOW_LOCK_IDENTITY=
    fi
  fi
  release_workflow_lock_guard "$guard_dir"
}

file_change_state() {
  local desired_path="$1"
  local current_path="$2"
  if [[ -e "$current_path" || -L "$current_path" ]] && \
     cmp -s "$desired_path" "$current_path"; then
    printf '%s' unchanged
  else
    printf '%s' changed
  fi
}

service_restart_required() {
  local version_changed="$1"
  local service_change_state="$2"
  local health_ok="$3"
  [[ "$version_changed" == true ]] || \
    [[ "$service_change_state" == changed ]] || \
    [[ "$health_ok" != true ]]
}

upgrade_headroom_distribution() {
  local tool_dir="${1:-}"
  local bin_dir="${2:-}"
  headroom_transaction_active=true
  if [[ -n "$tool_dir" && -n "$bin_dir" ]]; then
    PATH="$bin_dir:$PATH" UV_TOOL_DIR="$tool_dir" UV_TOOL_BIN_DIR="$bin_dir" \
      uv tool install --upgrade 'headroom-ai[all]'
  else
    uv tool install --upgrade 'headroom-ai[all]'
  fi
}

reconcile_headroom_transaction() {
  local version_changed="$1"
  local service_change_state="$2"
  local health_ok="$3"
  headroom_restart_required=false
  if service_restart_required "$version_changed" \
    "$service_change_state" "$health_ok"; then
    headroom_restart_required=true
    headroom_transaction_active=true
  else
    headroom_transaction_active=false
  fi
}

run_rollback_if_active() {
  local transaction_active="$1"
  local rollback_handler="$2"
  [[ "$transaction_active" == true ]] || return 0
  "$rollback_handler"
}

snapshot_path() {
  local source_path="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  install -d -m 0700 "$snapshot_dir"
  rm -f -- "$snapshot_dir/$snapshot_name.data" \
    "$snapshot_dir/$snapshot_name.present" "$snapshot_dir/$snapshot_name.absent"
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -pPR "$source_path" "$snapshot_dir/$snapshot_name.data"
    : >"$snapshot_dir/$snapshot_name.present"
  else
    : >"$snapshot_dir/$snapshot_name.absent"
  fi
}

restore_snapshot() {
  local destination="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  rm -f -- "$destination"
  if [[ -f "$snapshot_dir/$snapshot_name.present" ]]; then
    cp -pPR "$snapshot_dir/$snapshot_name.data" "$destination"
  elif [[ ! -f "$snapshot_dir/$snapshot_name.absent" ]]; then
    workflow_die "missing snapshot state for $destination"
    return 1
  fi
}

snapshot_path_matches() {
  local destination="$1"
  local snapshot_dir="$2"
  local snapshot_name="$3"
  if [[ -f "$snapshot_dir/$snapshot_name.present" ]]; then
    [[ -e "$destination" || -L "$destination" ]] && \
      cmp -s "$snapshot_dir/$snapshot_name.data" "$destination" && \
      [[ "$(path_mode "$snapshot_dir/$snapshot_name.data")" == \
         "$(path_mode "$destination")" ]]
  elif [[ -f "$snapshot_dir/$snapshot_name.absent" ]]; then
    [[ ! -e "$destination" && ! -L "$destination" ]]
  else
    workflow_die "missing snapshot state for $destination"
  fi
}

path_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

model_config_root() {
  printf '%s/model-config' "$1"
}

endpoint_config_lock_path() {
  printf '%s/endpoint.lock' "$(model_config_root "$1")"
}

acquire_endpoint_config_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_path
  lock_path="$(endpoint_config_lock_path "$data_root")"
  if ! python3 - "$lock_token" "$lock_path" 2>/dev/null <<'PY'
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
  then
    workflow_die "endpoint model publication is already locked (busy or stale)"
    return 1
  fi
}

release_endpoint_config_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_path
  lock_path="$(endpoint_config_lock_path "$data_root")"
  if [[ ! -L "$lock_path" ]] || \
     [[ "$(readlink "$lock_path" 2>/dev/null || true)" != "$lock_token" ]]; then
    workflow_die "endpoint model publication lock ownership changed (fail-closed)"
    return 1
  fi
  rm -f -- "$lock_path"
}

acquire_model_publication_lock() {
  local data_root="$1"
  local lock_token="$2"
  local lock_dir
  lock_dir="$(model_config_root "$data_root")/publication.lock"
  if ! python3 - "$lock_token" "$lock_dir" 2>/dev/null <<'PY'
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
  then
    workflow_die "model config publication is already locked (busy or stale)"
    return 1
  fi
  MODEL_PUBLICATION_LOCK_DIR="$lock_dir"
  MODEL_PUBLICATION_LOCK_IDENTITY="$lock_token"
}

release_model_publication_lock() {
  local lock_dir="${1:-${MODEL_PUBLICATION_LOCK_DIR:-}}"
  local lock_token="${2:-${MODEL_PUBLICATION_LOCK_IDENTITY:-}}"
  [[ -n "$lock_dir" ]] || return 0
  if [[ ! -L "$lock_dir" ]] || \
     [[ "$(readlink "$lock_dir" 2>/dev/null || true)" != "$lock_token" ]]; then
    workflow_die "model config publication lock ownership changed (fail-closed)"
    return 1
  fi
  rm -f -- "$lock_dir" || return 1
  MODEL_PUBLICATION_LOCK_DIR=
  MODEL_PUBLICATION_LOCK_IDENTITY=
}

atomic_replace_path() {
  python3 - "$1" "$2" <<'PY'
import os
import sys
os.replace(sys.argv[1], sys.argv[2])
PY
}

resolve_model_config_generation() {
  local data_root="$1"
  local config_root current_target generation
  config_root="$(model_config_root "$data_root")"
  [[ -L "$config_root/current" ]] || return 1
  current_target="$(readlink "$config_root/current")" || return 1
  case "$current_target" in
    ''|/*|*'/'*) return 1 ;;
  esac
  generation="$config_root/$current_target"
  [[ -d "$generation" && -f "$generation/models.json" && \
     -f "$generation/claudex.toml" ]] || return 1
  printf '%s' "$generation"
}

model_config_file() {
  local data_root="$1"
  local config_name="$2"
  local generation
  case "$config_name" in
    models.json|claudex.toml) ;;
    *) workflow_die "unsupported model config file: $config_name"; return 1 ;;
  esac
  if generation="$(resolve_model_config_generation "$data_root")"; then
    printf '%s/model-config/current/%s' "$data_root" "$config_name"
  else
    printf '%s/%s' "$data_root" "$config_name"
  fi
}

ensure_model_config_compat_links() {
  local data_root="$1"
  local config_name desired_target temporary_link
  for config_name in models.json claudex.toml; do
    desired_target="model-config/current/$config_name"
    if [[ -L "$data_root/$config_name" ]] && \
       [[ "$(readlink "$data_root/$config_name")" == "$desired_target" ]]; then
      continue
    fi
    temporary_link="$data_root/.$config_name.$$.$RANDOM"
    rm -f -- "$temporary_link"
    ln -s "$desired_target" "$temporary_link" || return 1
    atomic_replace_path "$temporary_link" "$data_root/$config_name" || {
      rm -f -- "$temporary_link"
      return 1
    }
  done
}

_activate_model_config_generation() {
  local data_root="$1"
  local candidate="$2"
  local config_root candidate_name generation generation_name
  local owned_path pointer_candidate active_name stale_generation
  local prior_generation observed_generation observed_target
  config_root="$(model_config_root "$data_root")"
  [[ "$(dirname "$candidate")" == "$config_root" ]] || \
    { workflow_die "model config generation is outside its workflow root"; return 1; }
  candidate_name="$(basename "$candidate")"
  case "$candidate_name" in candidate.*) ;; *) return 1 ;; esac
  owned_path="$candidate"
  if [[ ! -f "$candidate/models.json" || ! -f "$candidate/claudex.toml" ]]; then
    rm -rf -- "$owned_path"
    return 1
  fi

  generation_name="generation.${candidate_name#candidate.}"
  generation="$config_root/$generation_name"
  if [[ -e "$generation" || -L "$generation" ]]; then
    rm -rf -- "$owned_path"
    return 1
  fi
  prior_generation=
  if observed_generation="$(resolve_model_config_generation "$data_root")"; then
    prior_generation="$observed_generation"
  fi
  if ! mv "$candidate" "$generation"; then
    if [[ ! -e "$candidate" && ! -L "$candidate" ]] && \
       [[ -e "$generation" || -L "$generation" ]]; then
      owned_path="$generation"
    fi
    rm -rf -- "$owned_path"
    return 1
  fi
  owned_path="$generation"

  pointer_candidate="$config_root/.current.$$.$RANDOM"
  if ! ln -s "$generation_name" "$pointer_candidate"; then
    rm -rf -- "$owned_path"
    return 1
  fi
  if ! atomic_replace_path "$pointer_candidate" "$config_root/current"; then
    rm -f -- "$pointer_candidate"
    observed_target="$(readlink "$config_root/current" 2>/dev/null || true)"
    if [[ "$observed_target" != "$generation_name" ]]; then
      observed_generation=
      if observed_generation="$(resolve_model_config_generation "$data_root")" && \
         [[ -n "$prior_generation" ]] && \
         [[ "$observed_generation" == "$prior_generation" ]]; then
        rm -rf -- "$owned_path"
      fi
      return 1
    fi
  fi
  [[ "$(readlink "$config_root/current")" == "$generation_name" ]] || return 1
  ensure_model_config_compat_links "$data_root" || \
    printf 'WARN: model config compatibility links could not be refreshed\n' >&2

  if [[ "${CLAUDEX_DEFER_MODEL_PRUNE:-0}" != 1 ]]; then
    active_name="$(readlink "$config_root/current")"
    for stale_generation in "$config_root"/generation.*; do
      [[ -d "$stale_generation" ]] || continue
      [[ "$(basename "$stale_generation")" == "$active_name" ]] || \
        rm -rf -- "$stale_generation"
    done
  fi
}

restore_model_config_generation() {
  local data_root="$1"
  local prior_target="$2"
  local prior_snapshot="${3:-}"
  local config_root current_target current_generation pointer_candidate config_name
  local lock_token restore_candidate
  config_root="$(model_config_root "$data_root")"
  case "$prior_target" in
    ''|generation.*) ;;
    *) return 1 ;;
  esac
  if [[ -n "$prior_snapshot" ]] && \
     [[ ! -f "$prior_snapshot/models.json" || \
        ! -f "$prior_snapshot/claudex.toml" ]]; then
    return 1
  fi
  lock_token="$$:$RANDOM:$RANDOM"
  acquire_model_publication_lock "$data_root" "$lock_token" || return 1
  if [[ -n "$prior_target" ]] && \
     [[ ! -f "$config_root/$prior_target/models.json" || \
        ! -f "$config_root/$prior_target/claudex.toml" ]]; then
    [[ -n "$prior_snapshot" ]] || {
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    if [[ -e "$config_root/$prior_target" || \
          -L "$config_root/$prior_target" ]]; then
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    fi
    restore_candidate="$config_root/.generation.rollback.$$.$RANDOM"
    cp -pPR "$prior_snapshot" "$restore_candidate" || {
      rm -rf -- "$restore_candidate"
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    if ! mv "$restore_candidate" "$config_root/$prior_target"; then
      rm -rf -- "$restore_candidate"
      if [[ ! -f "$config_root/$prior_target/models.json" || \
            ! -f "$config_root/$prior_target/claudex.toml" ]]; then
        release_model_publication_lock \
          "$config_root/publication.lock" "$lock_token" || true
        return 1
      fi
    fi
  fi
  current_target="$(readlink "$config_root/current" 2>/dev/null || true)"
  if [[ "$current_target" == "$prior_target" ]] && \
     { [[ -n "$prior_target" ]] || \
       [[ ! -e "$config_root/current" && ! -L "$config_root/current" ]]; }; then
    release_model_publication_lock "$config_root/publication.lock" "$lock_token"
    return
  fi
  current_generation=
  case "$current_target" in
    generation.*) current_generation="$config_root/$current_target" ;;
  esac
  if [[ -n "$prior_target" ]]; then
    pointer_candidate="$config_root/.current.rollback.$$.$RANDOM"
    ln -s "$prior_target" "$pointer_candidate" || {
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    atomic_replace_path "$pointer_candidate" "$config_root/current" || {
      rm -f -- "$pointer_candidate"
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token" || true
      return 1
    }
    ensure_model_config_compat_links "$data_root" || true
  else
    rm -f -- "$config_root/current"
    for config_name in models.json claudex.toml; do
      if [[ -L "$data_root/$config_name" ]] && \
         [[ "$(readlink "$data_root/$config_name")" == \
            "model-config/current/$config_name" ]]; then
        rm -f -- "$data_root/$config_name"
      fi
    done
  fi
  if [[ -n "$current_generation" && \
        "$(basename "$current_generation")" != "$prior_target" ]]; then
    rm -rf -- "$current_generation"
  fi
  release_model_publication_lock "$config_root/publication.lock" "$lock_token"
}

prune_model_config_generations() {
  local data_root="$1"
  local config_root active_name stale_generation lock_token
  config_root="$(model_config_root "$data_root")"
  lock_token="$$:$RANDOM:$RANDOM"
  acquire_model_publication_lock "$data_root" "$lock_token" || return 1
  active_name="$(readlink "$config_root/current" 2>/dev/null || true)"
  case "$active_name" in
    generation.*) ;;
    *)
      release_model_publication_lock \
        "$config_root/publication.lock" "$lock_token"
      return 0
      ;;
  esac
  for stale_generation in "$config_root"/generation.*; do
    [[ -d "$stale_generation" ]] || continue
    [[ "$(basename "$stale_generation")" == "$active_name" ]] || \
      rm -rf -- "$stale_generation"
  done
  release_model_publication_lock "$config_root/publication.lock" "$lock_token"
}

restore_model_publication_signal_traps() {
  trap - HUP INT TERM
  [[ -z "${publication_saved_hup:-}" ]] || eval "$publication_saved_hup"
  [[ -z "${publication_saved_int:-}" ]] || eval "$publication_saved_int"
  [[ -z "${publication_saved_term:-}" ]] || eval "$publication_saved_term"
}

redeliver_model_publication_signal() {
  local signal_name="$1"
  # In Bash 3.2, $$ remains the outer shell PID inside background and
  # parenthesized contexts. A freshly exec'd helper observes the actual Bash
  # execution context as its parent.
  "$BASH" -c 'kill -s "$1" "$PPID"' claudex-signal "$signal_name"
}

handle_model_publication_signal() {
  local signal_name="$1"
  local signal_status="$2"
  if [[ "$publication_lock_owned" == true ]] || \
     { [[ -L "$publication_lock_dir" ]] && \
       [[ "$(readlink "$publication_lock_dir" 2>/dev/null || true)" == \
          "$publication_lock_token" ]]; }; then
    release_model_publication_lock \
      "$publication_lock_dir" "$publication_lock_token" || true
  fi
  restore_model_publication_signal_traps
  if redeliver_model_publication_signal "$signal_name"; then
    return 0
  fi
  return "$signal_status"
}

_activate_model_config_generation_locked() {
  local data_root="$1"
  local candidate="$2"
  local publication_lock_dir publication_lock_token publication_trap_capture
  local status=0
  local publication_lock_owned=false
  local publication_saved_hup publication_saved_int publication_saved_term
  publication_lock_dir="$(model_config_root "$data_root")/publication.lock"
  publication_lock_token="$$:$RANDOM:$RANDOM"
  publication_trap_capture="$candidate/.publication-trap.capture"
  trap -p HUP >"$publication_trap_capture"
  publication_saved_hup=
  IFS= read -r -d '' publication_saved_hup \
    <"$publication_trap_capture" || true
  trap -p INT >"$publication_trap_capture"
  publication_saved_int=
  IFS= read -r -d '' publication_saved_int \
    <"$publication_trap_capture" || true
  trap -p TERM >"$publication_trap_capture"
  publication_saved_term=
  IFS= read -r -d '' publication_saved_term \
    <"$publication_trap_capture" || true
  rm -f -- "$publication_trap_capture"
  trap 'handle_model_publication_signal HUP 129' HUP
  trap 'handle_model_publication_signal INT 130' INT
  trap 'handle_model_publication_signal TERM 143' TERM
  if ! acquire_model_publication_lock "$data_root" "$publication_lock_token"; then
    restore_model_publication_signal_traps
    rm -rf -- "$candidate"
    return 1
  fi
  publication_lock_owned=true
  _activate_model_config_generation "$data_root" "$candidate" || status=$?
  if ! release_model_publication_lock \
    "$publication_lock_dir" "$publication_lock_token"; then
    restore_model_publication_signal_traps
    return 1
  fi
  publication_lock_owned=false
  restore_model_publication_signal_traps
  return "$status"
}

activate_model_config_generation() {
  local data_root="$1"
  local candidate="$2"
  local config_root candidate_name
  config_root="$(model_config_root "$data_root")"
  [[ "$(dirname "$candidate")" == "$config_root" ]] || {
    workflow_die "model config generation is outside its workflow root"
    return 1
  }
  candidate_name="$(basename "$candidate")"
  case "$candidate_name" in candidate.*) ;; *) return 1 ;; esac
  _activate_model_config_generation_locked "$data_root" "$candidate"
}

migrate_legacy_model_config() {
  local data_root="$1"
  local config_root candidate
  config_root="$(model_config_root "$data_root")"
  install -d -m 0700 "$config_root"
  if resolve_model_config_generation "$data_root" >/dev/null 2>&1; then
    ensure_model_config_compat_links "$data_root" || \
      printf 'WARN: model config compatibility links could not be refreshed\n' >&2
    return
  fi
  [[ ! -e "$config_root/current" && ! -L "$config_root/current" ]] || return 1
  [[ -f "$data_root/models.json" && ! -L "$data_root/models.json" && \
     -f "$data_root/claudex.toml" && ! -L "$data_root/claudex.toml" ]] || return 0

  candidate="$(mktemp -d "$config_root/candidate.XXXXXX")" || return 1
  if ! cp -p "$data_root/models.json" "$candidate/models.json" || \
     ! cp -p "$data_root/claudex.toml" "$candidate/claudex.toml"; then
    rm -rf -- "$candidate"
    return 1
  fi
  activate_model_config_generation "$data_root" "$candidate"
}

cliproxy_models_response_is_ready() {
  jq -e '.data | type == "array"' "$1" >/dev/null 2>&1
}

assert_owned_session() {
  local workflow_root="$1"
  local data_root="$2"
  local run_dir="$3"
  local context_sha256="$4"
  (
    cd "$workflow_root" || exit 1
    python3 -m integrations.common.session_config verify \
      --workflow-root "$workflow_root" \
      --data-root "$data_root" \
      --run-dir "$run_dir" \
      --context-sha256 "$context_sha256"
  )
}

remove_managed_claude_base_url() {
  local input_file="$1"
  local output_file="$2"
  jq --indent 2 'if (.env? | type) == "object" then del(.env.ANTHROPIC_BASE_URL) else . end' \
    "$input_file" >"$output_file"
}

render_claudex_config() {
  local output_file="$1"
  local default_model="$2"
  local fast_model="$3"
  local balanced_model="$4"
  local powerful_model="$5"
  local haiku_model="$6"
  local sonnet_model="$7"
  local opus_model="$8"
  local claude_binary="${9:-}"
  local cliproxy_port="${10:-8317}"
  local headroom_port="${11:-8787}"

  valid_service_port "$cliproxy_port" || return 1
  valid_service_port "$headroom_port" || return 1
  [[ "$cliproxy_port" != "$headroom_port" ]] || return 1

  if [[ -z "$claude_binary" ]]; then
    claude_binary="$(command -v claude)" || {
      workflow_die "claude is not installed or not on PATH"
      return 1
    }
  fi

  printf '%s\n' \
    "claude_binary = \"$claude_binary\"" \
    'proxy_port = 13456' \
    'proxy_host = "127.0.0.1"' \
    'log_level = "info"' \
    'hyperlinks = "auto"' \
    '' \
    '[model_aliases]' \
    "fast = \"$fast_model\"" \
    "balanced = \"$balanced_model\"" \
    "powerful = \"$powerful_model\"" \
    '' \
    '[[profiles]]' \
    'name = "gpt"' \
    'provider_type = "DirectAnthropic"' \
    "base_url = \"http://127.0.0.1:$headroom_port\"" \
    'api_key = "claudex-passthrough"' \
    "default_model = \"$default_model\"" \
    'enabled = true' \
    'priority = 100' \
    '' \
    '[profiles.models]' \
    "haiku = \"$haiku_model\"" \
    "sonnet = \"$sonnet_model\"" \
    "opus = \"$opus_model\"" \
    '' \
    '[profiles.custom_headers]' \
    "X-Headroom-Base-Url = \"http://127.0.0.1:$cliproxy_port\"" \
    '' \
    '[router]' \
    'enabled = false' \
    '' \
    '[context.compression]' \
    'enabled = false' \
    '' \
    '[context.sharing]' \
    'enabled = false' \
    '' \
    '[context.rag]' \
    'enabled = false' >"$output_file"
}

render_cliproxy_config() {
  local output_file="$1"
  local auth_dir="$2"
  local cliproxy_port="${3:-8317}"

  valid_service_port "$cliproxy_port" || return 1

  printf '%s\n' \
    'host: "127.0.0.1"' \
    "port: $cliproxy_port" \
    'tls:' \
    '  enable: false' \
    'remote-management:' \
    '  allow-remote: false' \
    '  secret-key: ""' \
    '  disable-control-panel: true' \
    "auth-dir: \"$auth_dir\"" \
    'api-keys: []' \
    'debug: false' \
    'pprof:' \
    '  enable: false' \
    'plugins:' \
    '  enabled: false' \
    'commercial-mode: true' \
    'logging-to-file: false' \
    'usage-statistics-enabled: false' \
    'passthrough-headers: true' \
    'request-retry: 1' \
    'max-retry-credentials: 1' \
    'max-retry-interval: 10' >"$output_file"
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

select_first_available() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$available_models" | head -1
}

select_required_model() {
  local available_models="$1"
  shift
  local candidate
  for candidate in "$@"; do
    if printf '%s\n' "$available_models" | rg -Fxq "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  workflow_die "none of the required models are available: $*"
}

login_flag_for_provider() {
  case "$1" in
    codex) printf '%s' '--codex-login' ;;
    claude) printf '%s' '--claude-login' ;;
    *) workflow_die "login provider must be 'codex' or 'claude'" ;;
  esac
}

render_discovered_claudex_config() {
  local models_json="$1"
  local output_file="$2"
  local cliproxy_port="${3:-8317}"
  local headroom_port="${4:-8787}"
  local model_ids gpt_models claude_models
  local fast_model balanced_model powerful_model
  local haiku_model sonnet_model opus_model

  model_ids="$(jq -r '.data[]?.id // empty' "$models_json")" || return 1
  gpt_models="$(printf '%s\n' "$model_ids" | rg '^gpt-' | rg -v '^gpt-image-' || true)"
  claude_models="$(printf '%s\n' "$model_ids" | rg '^claude-' || true)"

  fast_model="$(select_required_model "$gpt_models" gpt-5.6-luna)" || return 1
  balanced_model="$(select_required_model "$gpt_models" gpt-5.6-terra)" || return 1
  powerful_model="$(select_required_model "$gpt_models" gpt-5.6-sol)" || return 1
  haiku_model="$(select_required_model "$claude_models" \
    claude-haiku-4-5-20251001 claude-haiku-4-5)" || return 1
  sonnet_model="$(select_required_model "$claude_models" \
    claude-sonnet-5 claude-sonnet-4-6 claude-sonnet-4-5)" || return 1
  opus_model="$(select_required_model "$claude_models" claude-opus-4-8)" || return 1

  render_claudex_config "$output_file" \
    "$powerful_model" "$fast_model" "$balanced_model" "$powerful_model" \
    "$haiku_model" "$sonnet_model" "$opus_model" "" \
    "$cliproxy_port" "$headroom_port"
}

extract_semver() {
  printf '%s\n' "$1" | rg -o -m1 '[0-9]+\.[0-9]+\.[0-9]+' | head -1
}

headroom_distribution_version() {
  local headroom_binary="$1"
  local tool_interpreter
  tool_interpreter="$(sed -n '1s/^#!//p' "$headroom_binary")"
  [[ -x "$tool_interpreter" ]] || return 1
  "$tool_interpreter" -c \
    'from importlib.metadata import version; print(version("headroom-ai"))'
}

distribution_version_changed() {
  [[ "$1" != "$2" ]]
}

restore_headroom_distribution() {
  local prior_version="$1"
  local tool_dir="${2:-}"
  local bin_dir="${3:-}"
  local restored_binary restored_version
  if [[ -n "$tool_dir" && -n "$bin_dir" ]]; then
    UV_TOOL_DIR="$tool_dir" UV_TOOL_BIN_DIR="$bin_dir" \
      uv tool install --force "headroom-ai[all]==$prior_version" || return 1
    restored_binary="$bin_dir/headroom"
  else
    uv tool install --force "headroom-ai[all]==$prior_version" || return 1
    restored_binary="$(command -v headroom)" || return 1
  fi
  restored_version="$(headroom_distribution_version "$restored_binary")" || return 1
  [[ "$restored_version" == "$prior_version" ]]
}

binary_reports_semver() {
  local binary="$1"
  local expected="$2"
  local binary_dir binary_name expected_semver reported_output reported_semver
  local version_probe=--version
  expected_semver="$(extract_semver "$expected")" || return 1
  binary_dir="$(dirname "$binary")"
  binary_name="$(basename "$binary")"
  if [[ "$binary_name" == cli-proxy-api ]]; then
    version_probe=--help
  fi
  reported_output="$(
    cd "$binary_dir" || exit 1
    "./$binary_name" "$version_probe" 2>&1
  )" || return 1
  reported_semver="$(extract_semver "$reported_output")" || return 1
  [[ "$reported_semver" == "$expected_semver" ]]
}

semver_at_least() {
  local current="$1"
  local minimum="$2"
  local current_major current_minor current_patch
  local minimum_major minimum_minor minimum_patch

  [[ "$current" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$minimum" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1

  IFS=. read -r current_major current_minor current_patch <<<"$current"
  IFS=. read -r minimum_major minimum_minor minimum_patch <<<"$minimum"

  ((10#$current_major > 10#$minimum_major)) && return 0
  ((10#$current_major < 10#$minimum_major)) && return 1
  ((10#$current_minor > 10#$minimum_minor)) && return 0
  ((10#$current_minor < 10#$minimum_minor)) && return 1
  ((10#$current_patch >= 10#$minimum_patch))
}

remove_managed_symlink() {
  local link_path="$1"
  local expected_target="$2"
  if [[ -L "$link_path" ]] && [[ "$(readlink "$link_path")" == "$expected_target" ]]; then
    unlink "$link_path"
  fi
}

stage_latest_github_binary() {
  local repository="$1"
  local prefix="$2"
  local suffix="$3"
  local archive_binary="$4"
  local destination="$5"
  local staging_dir="$6"
  local metadata archive row url digest asset version actual_sha staged_binary

  install -d -m 0700 "$staging_dir"
  metadata="$staging_dir/release.json"
  archive="$staging_dir/release.tar.gz"
  staged_binary="$staging_dir/$archive_binary"
  curl --fail --location --silent --show-error \
    "https://api.github.com/repos/$repository/releases/latest" --output "$metadata"
  row="$(jq -er --arg prefix "$prefix" --arg suffix "$suffix" '
    [.assets[] | select(.name | startswith($prefix) and endswith($suffix))] |
    if length == 1 then .[0] else error("expected exactly one release asset") end |
    [.browser_download_url, .digest, .name] | @tsv
  ' "$metadata")"
  IFS=$'\t' read -r url digest asset <<<"$row"
  version="$(jq -er '.tag_name | sub("^v"; "")' "$metadata")"
  if [[ "$digest" != sha256:* ]]; then
    workflow_die "GitHub did not publish a SHA-256 digest for $asset"
    return 1
  fi

  if [[ -x "$destination" ]] && binary_reports_semver "$destination" "$version"; then
    jq -cn --arg version "$version" \
      '{version: $version, changed: false, staged_path: null}'
    return 0
  fi

  curl --fail --location --silent --show-error "$url" --output "$archive"
  actual_sha="$(sha256_file "$archive")"
  if [[ "$actual_sha" != "${digest#sha256:}" ]]; then
    workflow_die "checksum mismatch for $asset"
    return 1
  fi
  tar -xzf "$archive" -C "$staging_dir" "$archive_binary"
  chmod 0755 "$staged_binary"
  if ! binary_reports_semver "$staged_binary" "$version"; then
    workflow_die "staged $asset did not report version $version"
    return 1
  fi
  jq -cn --arg version "$version" --arg staged_path "$staged_binary" \
    '{version: $version, changed: true, staged_path: $staged_path}'
}

activate_staged_file() {
  local staged_path="$1"
  local destination="$2"
  local mode="$3"
  install -m "$mode" "$staged_path" "$destination"
}

backup_path() {
  local source_path="$1"
  local backup_dir="$2"
  local backup_name="$3"
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    cp -pPR "$source_path" "$backup_dir/$backup_name"
  fi
}

render_launch_agent() {
  local output_file="$1"
  local data_root="$2"
  local escaped_binary escaped_config escaped_log escaped_home
  escaped_binary="$(xml_escape "$data_root/bin/cli-proxy-api")"
  escaped_config="$(xml_escape "$data_root/cliproxy.yaml")"
  escaped_log="$(xml_escape "$data_root/logs/cliproxy.log")"
  escaped_home="$(xml_escape "$HOME")"
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>com.user.claudex-cliproxy</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$escaped_binary</string>" \
    '    <string>--config</string>' \
    "    <string>$escaped_config</string>" \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <true/>' \
    '  <key>ProcessType</key>' \
    '  <string>Background</string>' \
    '  <key>Umask</key>' \
    '  <integer>63</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$escaped_home</string>" \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\%/%%}"
  value="${value//\$/\$\$}"
  printf '"%s"' "$value"
}

systemd_environment_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\%/%%}"
  printf '"%s"' "$value"
}

xml_escape() {
  printf '%s' "$1" | sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\\\&apos;/g"
}

render_systemd_user_unit() {
  local output_file="$1"
  local data_root="$2"
  local executable config log
  executable="$(systemd_quote "$data_root/bin/cli-proxy-api")"
  config="$(systemd_quote "$data_root/cliproxy.yaml")"
  log="$(systemd_quote "append:$data_root/logs/cliproxy.log")"
  printf '%s\n' \
    '[Unit]' \
    'Description=Claudex CLIProxyAPI' \
    'StartLimitIntervalSec=60' \
    'StartLimitBurst=3' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable --config $config" \
    'Restart=on-failure' \
    'RestartSec=5' \
    "StandardOutput=$log" \
    "StandardError=$log" \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}

render_headroom_launch_agent() {
  local output_file="$1"
  local data_root="$2"
  local headroom_binary="$3"
  local ca_bundle="$4"
  local headroom_port="${5:-8787}"
  local escaped_binary escaped_log escaped_home
  local escaped_config escaped_workspace escaped_ca
  escaped_binary="$(xml_escape "$headroom_binary")"
  escaped_log="$(xml_escape "$data_root/logs/headroom.log")"
  escaped_home="$(xml_escape "$HOME")"
  escaped_config="$(xml_escape "$data_root/headroom/config")"
  escaped_workspace="$(xml_escape "$data_root/headroom/state")"
  escaped_ca="$(xml_escape "$ca_bundle")"
  valid_service_port "$headroom_port" || return 1
  printf '%s\n' \
    '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0">' \
    '<dict>' \
    '  <key>Label</key>' \
    '  <string>com.user.claudex-headroom</string>' \
    '  <key>ProgramArguments</key>' \
    '  <array>' \
    "    <string>$escaped_binary</string>" \
    '    <string>proxy</string>' \
    '    <string>--host</string>' \
    '    <string>127.0.0.1</string>' \
    '    <string>--port</string>' \
    "    <string>$headroom_port</string>" \
    '    <string>--mode</string>' \
    '    <string>token</string>' \
    '    <string>--no-cache</string>' \
    '    <string>--intercept-tool-results</string>' \
    '    <string>--lossless</string>' \
    '    <string>--code-aware</string>' \
    '  </array>' \
    '  <key>RunAtLoad</key>' \
    '  <true/>' \
    '  <key>KeepAlive</key>' \
    '  <dict><key>SuccessfulExit</key><false/></dict>' \
    '  <key>ThrottleInterval</key>' \
    '  <integer>3</integer>' \
    '  <key>StandardOutPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>StandardErrorPath</key>' \
    "  <string>$escaped_log</string>" \
    '  <key>EnvironmentVariables</key>' \
    '  <dict>' \
    '    <key>HOME</key>' \
    "    <string>$escaped_home</string>" \
    '    <key>HEADROOM_CONFIG_DIR</key>' \
    "    <string>$escaped_config</string>" \
    '    <key>HEADROOM_WORKSPACE_DIR</key>' \
    "    <string>$escaped_workspace</string>" \
    '    <key>SSL_CERT_FILE</key>' \
    "    <string>$escaped_ca</string>" \
    '    <key>HEADROOM_CACHE_ENABLED</key><string>0</string>' \
    '    <key>HEADROOM_MEMORY_ENABLED</key><string>0</string>' \
    '    <key>HEADROOM_OUTPUT_SHAPER</key><string>0</string>' \
    '    <key>HEADROOM_VERBOSITY_AUTOTUNE</key><string>0</string>' \
    '    <key>HEADROOM_EFFORT_ROUTER</key><string>0</string>' \
    '    <key>HEADROOM_LOG_MESSAGES</key><string>0</string>' \
    '  </dict>' \
    '</dict>' \
    '</plist>' >"$output_file"
}

render_headroom_systemd_user_unit() {
  local output_file="$1"
  local data_root="$2"
  local headroom_binary="$3"
  local ca_bundle="$4"
  local headroom_port="${5:-8787}"
  local executable log config_environment workspace_environment ca_environment
  executable="$(systemd_quote "$headroom_binary")"
  log="$(systemd_quote "append:$data_root/logs/headroom.log")"
  config_environment="$(systemd_environment_quote \
    "HEADROOM_CONFIG_DIR=$data_root/headroom/config")"
  workspace_environment="$(systemd_environment_quote \
    "HEADROOM_WORKSPACE_DIR=$data_root/headroom/state")"
  ca_environment="$(systemd_environment_quote "SSL_CERT_FILE=$ca_bundle")"
  valid_service_port "$headroom_port" || return 1
  printf '%s\n' \
    '[Unit]' \
    'Description=Claudex Headroom proxy' \
    'After=network-online.target' \
    '' \
    '[Service]' \
    'Type=exec' \
    "ExecStart=$executable proxy --host 127.0.0.1 --port $headroom_port --mode token --no-cache --intercept-tool-results --lossless --code-aware" \
    'Restart=on-failure' \
    'RestartSec=3' \
    "Environment=$config_environment" \
    "Environment=$workspace_environment" \
    "Environment=$ca_environment" \
    'Environment="HEADROOM_CACHE_ENABLED=0"' \
    'Environment="HEADROOM_MEMORY_ENABLED=0"' \
    'Environment="HEADROOM_OUTPUT_SHAPER=0"' \
    'Environment="HEADROOM_VERBOSITY_AUTOTUNE=0"' \
    'Environment="HEADROOM_EFFORT_ROUTER=0"' \
    'Environment="HEADROOM_LOG_MESSAGES=0"' \
    "StandardOutput=$log" \
    "StandardError=$log" \
    '' \
    '[Install]' \
    'WantedBy=default.target' >"$output_file"
}
