#!/usr/bin/env bash

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"

print_model_discovery_instruction() {
  printf 'Next: claudex-login codex; claudex-login claude; %s/discover-models.sh\n' \
    "$WORKFLOW_ROOT" >&2
}

discover_models_main_core() {
  local data_root generation_root candidate_dir models_file config_file active_generation
  local cliproxy_port headroom_port
  data_root="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || return 1
  if ! IFS=$'\t' read -r cliproxy_port headroom_port \
      < <(read_service_ports "$data_root"); then
    print_model_discovery_instruction
    return 1
  fi
  if ! install -d -m 0700 "$data_root"; then
    print_model_discovery_instruction
    return 1
  fi
  if ! migrate_legacy_model_config "$data_root"; then
    print_model_discovery_instruction
    return 1
  fi
  generation_root="$(model_config_root "$data_root")"
  if ! candidate_dir="$(mktemp -d "$generation_root/candidate.XXXXXX")"; then
    print_model_discovery_instruction
    return 1
  fi
  models_file="$candidate_dir/models.json"
  config_file="$candidate_dir/claudex.toml"

  if ! curl --fail --silent --show-error \
    "http://127.0.0.1:$cliproxy_port/v1/models" >"$models_file"; then
    rm -rf -- "$candidate_dir"
    print_model_discovery_instruction
    return 1
  fi
  if ! chmod 0600 "$models_file"; then
    rm -rf -- "$candidate_dir"
    print_model_discovery_instruction
    return 1
  fi
  if ! render_discovered_claudex_config \
      "$models_file" "$config_file" "$cliproxy_port" "$headroom_port"; then
    rm -rf -- "$candidate_dir"
    print_model_discovery_instruction
    return 1
  fi
  if ! chmod 0600 "$config_file"; then
    rm -rf -- "$candidate_dir"
    print_model_discovery_instruction
    return 1
  fi
  if ! "$data_root/bin/claudex" --config "$config_file" \
    config validate >/dev/null; then
    rm -rf -- "$candidate_dir"
    print_model_discovery_instruction
    return 1
  fi

  if ! activate_model_config_generation "$data_root" "$candidate_dir"; then
    print_model_discovery_instruction
    return 1
  fi
  active_generation="$(resolve_model_config_generation "$data_root")" || return 1

  printf 'Configured one dual-provider profile:\n'
  rg '^(default_model|haiku|sonnet|opus) = ' \
    "$active_generation/claudex.toml" || true
}

discover_models_main() (
  local data_root endpoint_lock_token
  data_root="$(validated_workflow_data_dir "$WORKFLOW_ROOT")" || return 1
  install -d -m 0700 "$(model_config_root "$data_root")" || return 1
  endpoint_lock_token="$$:$RANDOM:$RANDOM"
  acquire_endpoint_config_lock "$data_root" "$endpoint_lock_token" || return 1
  trap 'release_endpoint_config_lock "$data_root" "$endpoint_lock_token"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  discover_models_main_core
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  discover_models_main "$@"
fi
