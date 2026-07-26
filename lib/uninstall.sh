#!/usr/bin/env bash

orichum_uninstall_validate_private_root() {
  local candidate="$1"
  local checkout_root="$2"
  local label="$3"
  workflow_python - "$candidate" "$HOME" "$checkout_root" "$label" <<'PY'
import os
import stat
import sys
from pathlib import Path

raw, home_raw, checkout_raw, label = sys.argv[1:]
if not os.path.isabs(raw):
    raise SystemExit(f"{label} must be an absolute path")

normalized = Path(os.path.normpath(raw))
cursor = Path(normalized.anchor)
for component in normalized.parts[1:]:
    cursor /= component
    try:
        value = os.lstat(cursor)
    except FileNotFoundError:
        break
    except OSError as error:
        raise SystemExit(f"{label} existing ancestor is inaccessible") from error
    if stat.S_ISLNK(value.st_mode):
        raise SystemExit(f"{label} existing ancestors must not be symlinks")
    if cursor != normalized and not stat.S_ISDIR(value.st_mode):
        raise SystemExit(f"{label} existing ancestor is not a directory")

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
if candidate in (root, home, checkout) or inside_checkout:
    raise SystemExit(f"refusing unsafe {label}")
if candidate.exists():
    value = candidate.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid():
        raise SystemExit(f"{label} is not a private current-user directory")
print(candidate, end="")
PY
}

orichum_uninstall_preflight_runtime() {
  local data_root="$1"
  workflow_python - "$data_root" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.exists():
    raise SystemExit(0)
for name, expected_directory in (
    ("bin", True),
    ("python", True),
    ("tools", True),
    ("logs", True),
    ("cliproxy.yaml", False),
    ("cliproxy-management.key", False),
):
    path = root / name
    try:
        value = path.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(value.st_mode) or value.st_uid != os.getuid():
        raise SystemExit(f"managed runtime path is unsafe: {path}")
    if expected_directory != stat.S_ISDIR(value.st_mode):
        raise SystemExit(f"managed runtime path has unexpected type: {path}")
PY
}

orichum_uninstall_launcher_is_owned() {
  local launcher="$1"
  local expected="$2"
  local target target_absolute
  if [[ ! -e "$launcher" && ! -L "$launcher" ]]; then
    return 0
  fi
  [[ -L "$launcher" ]] || return 1
  target="$(readlink "$launcher")" || return 1
  case "$target" in
    /*) target_absolute="$target" ;;
    *) target_absolute="$(dirname "$launcher")/$target" ;;
  esac
  target_absolute="$(workflow_python - "$target_absolute" <<'PY'
import os
import sys
print(os.path.normpath(sys.argv[1]), end="")
PY
  )" || return 1
  if [[ "$target_absolute" == "$expected" ]]; then
    return 0
  fi
  [[ -f "$target_absolute" && ! -L "$target_absolute" && \
     "$(basename "$target_absolute")" == orichum && \
     "$(basename "$(dirname "$target_absolute")")" == bin ]] || return 1
  rg -Fq 'ORICHUM_WORKFLOW_ROOT' "$target_absolute" 2>/dev/null || \
    rg -Fq 'integrations.common.orichum_cli' "$target_absolute" 2>/dev/null
}

orichum_uninstall_service_identity() {
  local platform="$1"
  local kind="$2"
  case "$platform:$kind" in
    darwin:cliproxy)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.cliproxy.plist" \
        io.orichum.cliproxy -
      ;;
    darwin:route)
      printf '%s\t%s\t%s\n' \
        "$HOME/Library/LaunchAgents/io.orichum.route-proxy.plist" \
        io.orichum.route-proxy -
      ;;
    systemd:cliproxy)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-cliproxy.service" \
        - orichum-cliproxy.service
      ;;
    systemd:route)
      printf '%s\t%s\t%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-route-proxy.service" \
        - orichum-route-proxy.service
      ;;
    *) return 1 ;;
  esac
}

orichum_uninstall_preflight_service() {
  local platform="$1"
  local kind="$2"
  local service_file="$3"
  local service_label="$4"
  local service_unit="$5"
  local data_root="$6"
  local workflow_root="$7"
  local target_state loaded_definition

  if [[ -e "$service_file" || -L "$service_file" ]]; then
    case "$kind" in
      cliproxy)
        cliproxy_service_is_owned "$service_file" "$data_root"
        ;;
      route)
        claudex_proxy_service_is_owned \
          "$service_file" "$data_root" "$workflow_root"
        ;;
      *) return 1 ;;
    esac || {
      workflow_die "refusing unknown Orichum service: $service_file"
      return 1
    }
  fi

  target_state="$(managed_service_target_state \
    "$platform" "$service_label" "$service_unit")" || {
      workflow_die "Orichum service target could not be inspected safely"
      return 1
    }
  if [[ "$target_state" == loaded ]]; then
    [[ -f "$service_file" && ! -L "$service_file" ]] || {
      workflow_die "refusing loaded Orichum target without an owned definition"
      return 1
    }
    loaded_definition="$(managed_service_definition_path \
      "$platform" "$service_label" "$service_unit" 2>/dev/null)" || {
      workflow_die "loaded Orichum service definition could not be inspected"
      return 1
    }
    [[ "$loaded_definition" == "$service_file" ]] || {
      workflow_die "refusing loaded Orichum target with a foreign definition"
      return 1
    }
  fi
  printf '%s\n' "$target_state"
}

orichum_uninstall_remove_service() {
  local platform="$1"
  local service_file="$2"
  local service_label="$3"
  local service_unit="$4"
  local target_state="$5"
  case "$platform" in
    darwin)
      if [[ "$target_state" == loaded ]]; then
        launchctl bootout "gui/$(id -u)" "$service_file" >/dev/null
      fi
      ;;
    systemd)
      if [[ "$target_state" == loaded ]]; then
        systemctl --user stop "$service_unit" >/dev/null
      fi
      if [[ -e "$service_file" || -L "$service_file" || \
            "$target_state" == loaded ]]; then
        systemctl --user disable "$service_unit" >/dev/null
      fi
      ;;
    *) return 1 ;;
  esac
  rm -f -- "$service_file"
}

orichum_uninstall_remove_legacy_headroom() {
  local platform="$1"
  local data_root="$2"
  local index
  local -a files labels units modes
  [[ -d "$data_root" && ! -L "$data_root" ]] || return 0
  if [[ "$platform" == darwin ]]; then
    files=(
      "$HOME/Library/LaunchAgents/io.orichum.headroom.plist"
      "$HOME/Library/LaunchAgents/com.user.claudex-headroom.plist"
      "$HOME/Library/LaunchAgents/com.user.headroom-proxy.plist"
    )
    labels=(
      io.orichum.headroom
      com.user.claudex-headroom
      com.user.headroom-proxy
    )
    units=(- - -)
  else
    files=(
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/orichum-headroom.service"
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claudex-headroom.service"
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/headroom-proxy.service"
    )
    labels=(- - -)
    units=(
      orichum-headroom.service
      claudex-headroom.service
      headroom-proxy.service
    )
  fi
  modes=(new legacy legacy)
  for index in "${!files[@]}"; do
    if [[ -e "${files[$index]}" || -L "${files[$index]}" ]]; then
      remove_owned_headroom_installation \
        "$platform" "$data_root" "${files[$index]}" \
        "${labels[$index]}" "${units[$index]}" "${modes[$index]}" || \
        return 1
    fi
  done
  if [[ -e "$data_root/headroom" || -L "$data_root/headroom" ]]; then
    remove_owned_headroom_installation \
      "$platform" "$data_root" "${files[0]}" \
      "${labels[0]}" "${units[0]}" "${modes[0]}"
  fi
}

orichum_uninstall() {
  local purge="${1:-false}"
  local workflow_root="${WORKFLOW_ROOT:?}"
  local user_bin_dir="${USER_BIN_DIR:-$HOME/.local/bin}"
  local data_root config_root platform
  local cliproxy_file cliproxy_label cliproxy_unit cliproxy_state
  local route_file route_label route_unit route_state
  local launcher="$user_bin_dir/orichum"
  local systemd_reload=false
  local -a runtime_paths

  data_root="$(validated_workflow_data_dir "$workflow_root")" || \
    workflow_die "refusing unsafe ORICHUM_DATA_HOME"
  config_root="$(
    orichum_uninstall_validate_private_root \
      "${ORICHUM_CONFIG_HOME:-${XDG_CONFIG_HOME:-$HOME/.config}/orichum}" \
      "$workflow_root" ORICHUM_CONFIG_HOME
  )" || return 1
  data_root="$(
    orichum_uninstall_validate_private_root \
      "$data_root" "$workflow_root" ORICHUM_DATA_HOME
  )" || return 1
  case "$(uname -s)" in
    Darwin) platform=darwin ;;
    Linux) platform=systemd ;;
    *)
      workflow_die "supported platforms are macOS, Linux, and WSL2"
      return 1
      ;;
  esac

  IFS=$'\t' read -r cliproxy_file cliproxy_label cliproxy_unit \
    < <(orichum_uninstall_service_identity "$platform" cliproxy)
  IFS=$'\t' read -r route_file route_label route_unit \
    < <(orichum_uninstall_service_identity "$platform" route)

  cliproxy_state="$(orichum_uninstall_preflight_service \
    "$platform" cliproxy "$cliproxy_file" "$cliproxy_label" \
    "$cliproxy_unit" "$data_root" "$workflow_root")" || return 1
  route_state="$(orichum_uninstall_preflight_service \
    "$platform" route "$route_file" "$route_label" \
    "$route_unit" "$data_root" "$workflow_root")" || return 1
  preflight_owned_headroom_installation "$platform" "$data_root" || return 1
  orichum_uninstall_preflight_runtime "$data_root" || {
    workflow_die "refusing unsafe Orichum runtime layout"
    return 1
  }
  orichum_uninstall_launcher_is_owned \
    "$launcher" "$workflow_root/bin/orichum" || {
      workflow_die "refusing unknown launcher: $launcher"
      return 1
    }

  orichum_uninstall_remove_service \
    "$platform" "$route_file" "$route_label" "$route_unit" "$route_state"
  orichum_uninstall_remove_service \
    "$platform" "$cliproxy_file" "$cliproxy_label" \
    "$cliproxy_unit" "$cliproxy_state"
  if [[ "$platform" == systemd ]]; then
    systemd_reload=true
  fi
  orichum_uninstall_remove_legacy_headroom "$platform" "$data_root"
  if [[ "$systemd_reload" == true ]]; then
    systemctl --user daemon-reload >/dev/null
  fi
  rm -f -- "$launcher"

  if [[ "$purge" == true ]]; then
    if [[ "$config_root" == "$data_root" || \
          "$config_root" == "$data_root/"* ]]; then
      rm -rf -- "$data_root"
    elif [[ "$data_root" == "$config_root/"* ]]; then
      rm -rf -- "$config_root"
    else
      rm -rf -- "$data_root" "$config_root"
    fi
    printf '%s\n' \
      'Purged Orichum.' \
      "  Removed data:   $data_root" \
      "  Removed config: $config_root" \
      "  Preserved checkout: $workflow_root" \
      '  Standalone third-party installations were not changed.'
    return
  fi

  runtime_paths=(
    "$data_root/bin"
    "$data_root/python"
    "$data_root/tools"
    "$data_root/logs"
    "$data_root/cliproxy.yaml"
    "$data_root/cliproxy-management.key"
  )
  rm -rf -- "${runtime_paths[@]}"
  printf '%s\n' \
    'Uninstalled Orichum runtime.' \
    "  Removed launcher: $launcher" \
    "  Preserved data:   $data_root" \
    "  Preserved config: $config_root" \
    '  Accounts, sessions, project context, graphs, and Mempalace palaces were preserved.' \
    '  Standalone third-party installations were not changed.' \
    "  Reinstall with: $workflow_root/install.sh"
}
