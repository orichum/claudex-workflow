#!/usr/bin/env bash
set -euo pipefail

tmp_dir=""
headroom_response=""
headroom_error=""
models_response=""
models_error=""

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
if ! (umask 077
  : >"$headroom_response"
  : >"$headroom_error"
  : >"$models_response"
  : >"$models_error"
  chmod 0600 \
    "$headroom_response" "$headroom_error" \
    "$models_response" "$models_error"
) 2>/dev/null; then
  emit_warning "Claudex health warning: local service health check could not secure response files."
  exit 0
fi

curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
  http://127.0.0.1:8787/health \
  >"$headroom_response" 2>"$headroom_error" &
headroom_pid=$!
curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
  http://127.0.0.1:8317/v1/models \
  >"$models_response" 2>"$models_error" &
models_pid=$!

headroom_status=0
models_status=0
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

warning=""
if [[ "$headroom_status" -ne 0 || "$models_status" -ne 0 ]]; then
  warning="Claudex health warning: a bounded local Headroom or CLIProxyAPI request failed."
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

if [[ -n "$warning" ]]; then
  emit_warning "$warning"
fi
exit 0
