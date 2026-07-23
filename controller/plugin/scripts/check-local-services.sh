#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="${CLAUDEX_WORKFLOW_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
# shellcheck source=../../../lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
WORKFLOW_DATA_ROOT="$(workflow_data_dir)"
if ! IFS=$'\t' read -r CLIPROXY_PORT HEADROOM_PORT CLAUDEX_PROXY_PORT \
    < <(read_service_ports "$WORKFLOW_DATA_ROOT"); then
  jq -cn '{systemMessage:"Claudex health warning: service port configuration is invalid."}'
  exit 0
fi

tmp_dir=""
headroom_response=""
headroom_error=""
models_response=""
models_error=""
claudex_response=""
claudex_error=""

# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap.
cleanup() {
  if [[ -n "$headroom_response" ]]; then
    rm -f -- "$headroom_response" || :
  fi
  if [[ -n "$headroom_error" ]]; then
    rm -f -- "$headroom_error" || :
  fi
  if [[ -n "$models_response" ]]; then
    rm -f -- "$models_response" || :
  fi
  if [[ -n "$models_error" ]]; then
    rm -f -- "$models_error" || :
  fi
  if [[ -n "$claudex_response" ]]; then
    rm -f -- "$claudex_response" || :
  fi
  if [[ -n "$claudex_error" ]]; then
    rm -f -- "$claudex_error" || :
  fi
  if [[ -n "$tmp_dir" ]]; then
    rmdir "$tmp_dir" 2>/dev/null || :
  fi
}
trap cleanup EXIT

emit_warning() {
  jq -cn --arg warning "$1" '{systemMessage:$warning}'
}

if ! tmp_dir="$(umask 077; mktemp -d "${TMPDIR:-/tmp}/claudex-health.XXXXXX" 2>/dev/null)"; then
  emit_warning "Claudex health warning: local service health check could not create private temporary state."
  exit 0
fi
if ! chmod 0700 "$tmp_dir" 2>/dev/null; then
  emit_warning "Claudex health warning: local service health check could not secure private temporary state."
  exit 0
fi

headroom_response="$tmp_dir/headroom.response"
headroom_error="$tmp_dir/headroom.error"
models_response="$tmp_dir/models.response"
models_error="$tmp_dir/models.error"
claudex_response="$tmp_dir/claudex.response"
claudex_error="$tmp_dir/claudex.error"
if ! (umask 077
  : >"$headroom_response"
  : >"$headroom_error"
  : >"$models_response"
  : >"$models_error"
  : >"$claudex_response"
  : >"$claudex_error"
  chmod 0600 \
    "$headroom_response" "$headroom_error" \
    "$models_response" "$models_error" \
    "$claudex_response" "$claudex_error"
) 2>/dev/null; then
  emit_warning "Claudex health warning: local service health check could not secure response files."
  exit 0
fi

curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
  "http://127.0.0.1:$HEADROOM_PORT/health" \
  >"$headroom_response" 2>"$headroom_error" &
headroom_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
  "http://127.0.0.1:$CLIPROXY_PORT/v1/models" \
  >"$models_response" 2>"$models_error" &
models_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
  "http://127.0.0.1:$CLAUDEX_PROXY_PORT/v1/models" \
  >"$claudex_response" 2>"$claudex_error" &
claudex_pid=$!

headroom_status=0
models_status=0
claudex_status=0
if wait "$headroom_pid"; then
  :
else
  headroom_status=$?
fi
if wait "$models_pid"; then
  :
else
  models_status=$?
fi
if wait "$claudex_pid"; then
  :
else
  claudex_status=$?
fi

warning=""
if [[ "$headroom_status" -ne 0 || "$models_status" -ne 0 || \
      "$claudex_status" -ne 0 ]]; then
  warning="Claudex health warning: a bounded local Headroom, CLIProxyAPI, or Claudex proxy request failed."
elif ! jq -e '
  .service == "headroom-proxy" and
  .status == "healthy" and
  .ready == true
' "$headroom_response" >/dev/null 2>&1; then
  warning="Claudex health warning: Headroom is not healthy and ready."
else
  missing_models=""
  if ! jq -e '
    (.data | type == "array") and
    any(.data[]?; .id == "gpt-5.6-sol")
  ' "$models_response" >/dev/null 2>&1; then
    missing_models="gpt-5.6-sol"
  fi
  if ! jq -e '
    (.data | type == "array") and
    any(.data[]?; .id == "claude-opus-4-8")
  ' "$models_response" >/dev/null 2>&1; then
    if [[ -n "$missing_models" ]]; then
      missing_models="$missing_models, claude-opus-4-8"
    else
      missing_models="claude-opus-4-8"
    fi
  fi
  if [[ -n "$missing_models" ]]; then
    warning="Claudex health warning: required model missing: $missing_models."
  fi
fi

if [[ -z "$warning" ]]; then
  claudex_config_file="$(model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml)"
  claudex_controller_model="$(claudex_config_default_model \
    "$claudex_config_file" 2>/dev/null || true)"
  if [[ -z "$claudex_controller_model" ]] || \
     ! claudex_proxy_models_response_is_ready \
       "$claudex_response" "$claudex_controller_model"; then
    warning="Claudex health warning: persistent Claudex proxy does not expose the configured controller model."
  fi
fi

if [[ -n "$warning" ]]; then
  emit_warning "$warning"
fi
exit 0
