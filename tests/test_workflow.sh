#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="$ROOT/bin/claudex-gpt"

bash -n "$launcher"
jq empty \
  "$ROOT/controller/settings.json" \
  "$ROOT/controller/project-context.json" \
  "$ROOT/controller/plugin/hooks/hooks.json" \
  "$ROOT/controller/plugin/.claude-plugin/plugin.json"
jq -e '.attribution == {commit: "", pr: "", sessionUrl: false}' \
  "$ROOT/controller/settings.json" >/dev/null

rg -q -- '--effort high' "$launcher"
rg -q -- '--strict-mcp-config' "$launcher"
rg -q -- '--append-system-prompt-file' "$launcher"
rg -q -- '--plugin-dir' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY=3$' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION=24$' "$launcher"
rg -q '^export CLAUDE_CODE_MAX_RETRIES=2$' "$launcher"
rg -q '^export ENABLE_TOOL_SEARCH=true$' "$launcher"
! rg -qi 'ultracode' "$launcher"

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-workflow-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
# shellcheck source=../discover-models.sh
source "$ROOT/discover-models.sh"

launcher_data="$(cd "$fixture" && pwd -P)/launcher-data"
launcher_xdg="$fixture/launcher-xdg"
launcher_tools="$fixture/launcher-tools"
launcher_service="$launcher_xdg/systemd/user/claudex-translation-proxy.service"
install -d -m 0700 "$launcher_data"
install -d "$launcher_data/bin" "$launcher_data/claude-config" \
  "$launcher_xdg/systemd/user" "$launcher_tools"
install -d -m 0700 "$launcher_data/state" "$launcher_data/state/sessions"
render_claudex_config "$launcher_data/claudex.toml" \
  gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol \
  claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8 \
  /portable/bin/claude 18317 18787 13457
migrate_legacy_model_config "$launcher_data"
write_service_ports "$launcher_data" 18317 18787 13457
render_claudex_proxy_systemd_user_unit \
  "$launcher_service" "$launcher_data" 13457
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$@"' >"$launcher_data/bin/claudex"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "${1:-}" in -s) printf "Linux\n" ;; -m) printf "aarch64\n" ;; *) printf "Linux\n" ;; esac' \
  >"$launcher_tools/uname"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *LoadState* ]]; then printf "%s\n" "${FAKE_TARGET_STATE:-loaded}"; elif [[ "$*" == *FragmentPath* ]]; then printf "%s\n" "${FAKE_SERVICE_PATH:?}"; elif [[ "$*" == *MainPID* ]]; then printf "%s\n" "${FAKE_MANAGER_PID:-4242}"; else exit 1; fi' \
  >"$launcher_tools/systemctl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf '\''LISTEN 0 128 127.0.0.1:13457 0.0.0.0:* users:(("claudex",pid=%s,fd=7))\n'\'' "${FAKE_LISTENER_PID:-4242}"' \
  >"$launcher_tools/ss"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "${FAKE_MODELS_MODE:-healthy}" in healthy) printf '\''{"object":"list","data":[{"id":"gpt-5.6-sol"},{"id":"gpt-5.6-terra"},{"id":"claude-sonnet-5"},{"id":"claude-opus-4-8"}]}\n'\'' ;; missing) printf '\''{"object":"list","data":[{"id":"gpt-5.6-terra"}]}\n'\'' ;; invalid) printf '\''not-json\n'\'' ;; *) exit 7 ;; esac' \
  >"$launcher_tools/curl"
chmod 0755 "$launcher_data/bin/claudex" "$launcher_tools"/*

launcher_session_count() {
  find "$launcher_data/state/sessions" -mindepth 1 -maxdepth 1 \
    -type d -name 'run.*' 2>/dev/null | wc -l | tr -d ' '
}

assert_launcher_proxy_rejected() {
  local case_name="$1"
  shift
  local before after status
  before="$(launcher_session_count)"
  set +e
  (
    cd "$ROOT"
    CLAUDEX_DATA_DIR="$launcher_data" \
    XDG_CONFIG_HOME="$launcher_xdg" \
    FAKE_SERVICE_PATH="$launcher_service" \
    PATH="$launcher_tools:$PATH" \
    "$@" "$launcher" marker \
      >"$fixture/launcher-$case_name.stdout" \
      2>"$fixture/launcher-$case_name.stderr"
  )
  status=$?
  set -e
  [[ "$status" -ne 0 ]]
  after="$(launcher_session_count)"
  [[ "$after" == "$before" ]]
  [[ "$(cat "$fixture/launcher-$case_name.stderr")" == \
     'ERROR: persistent Claudex proxy is not ready at 127.0.0.1:13457; run claudex-doctor' ]]
}

mv "$launcher_service" "$launcher_service.saved"
assert_launcher_proxy_rejected absent-definition env
mv "$launcher_service.saved" "$launcher_service"
assert_launcher_proxy_rejected foreign-listener \
  env FAKE_LISTENER_PID=99
assert_launcher_proxy_rejected wrong-pid \
  env FAKE_MANAGER_PID=99
assert_launcher_proxy_rejected invalid-json \
  env FAKE_MODELS_MODE=invalid
assert_launcher_proxy_rejected missing-model \
  env FAKE_MODELS_MODE=missing

before_rejected_model="$(launcher_session_count)"
set +e
(
  cd "$ROOT"
  CLAUDEX_DATA_DIR="$launcher_data" \
    XDG_CONFIG_HOME="$launcher_xdg" \
    FAKE_SERVICE_PATH="$launcher_service" \
    PATH="$launcher_tools:$PATH" \
    "$launcher" --model caller/override marker \
      >"$fixture/launcher-model.stdout" \
      2>"$fixture/launcher-model.stderr"
)
rejected_model_status=$?
set -e
[[ "$rejected_model_status" == 2 ]]
[[ "$(launcher_session_count)" == "$before_rejected_model" ]]
[[ "$(cat "$fixture/launcher-model.stderr")" == \
   'ERROR: claudex-gpt owns controller option: --model' ]]

if ! launcher_output="$(
  cd "$ROOT"
  CLAUDEX_CONFIG_FILE=/inherited/poison \
    CLAUDEX_DATA_DIR="$launcher_data" \
    XDG_CONFIG_HOME="$launcher_xdg" \
    FAKE_SERVICE_PATH="$launcher_service" \
    TMPDIR="$fixture" \
    PATH="$launcher_tools:$PATH" \
    "$launcher" marker 2>"$fixture/launcher.stderr"
)"; then
  cat "$fixture/launcher.stderr" >&2
  exit 1
fi
rg -Fxq -- '--config' <<<"$launcher_output"
rg -Fxq -- "$launcher_data/claudex.toml" <<<"$launcher_output"
rg -Fxq -- 'marker' <<<"$launcher_output"
[[ "$(rg -Fxc -- '--model' <<<"$launcher_output")" == 1 ]]
[[ "$(awk 'previous == "--model" {print; exit} {previous=$0}' \
  <<<"$launcher_output")" == 'gpt-5.6-sol' ]]
[[ "$(rg -Fxc -- '--plugin-dir' <<<"$launcher_output")" == 1 ]]
session_plugin_dir="$(awk \
  'previous == "--plugin-dir" {print; exit} {previous=$0}' \
  <<<"$launcher_output")"
[[ "$session_plugin_dir" == \
  "$launcher_data/state/sessions/run."*/plugin ]]
[[ -d "$session_plugin_dir" && ! -L "$session_plugin_dir" ]]
[[ "$session_plugin_dir" != "$ROOT/controller/plugin" ]]
[[ -f "${session_plugin_dir%/plugin}/effective-models.json" ]]
[[ ! -e "$fixture"/claudex-launch-models.* ]]
[[ ! -s "$fixture/launcher.stderr" ]]

sessions_before_resume="$(launcher_session_count)"
if ! resume_output="$(
  cd "$ROOT"
  CLAUDEX_DATA_DIR="$launcher_data" \
    XDG_CONFIG_HOME="$launcher_xdg" \
    FAKE_SERVICE_PATH="$launcher_service" \
    TMPDIR="$fixture" \
    PATH="$launcher_tools:$PATH" \
    "$launcher" --resume owned-resume-id \
      2>"$fixture/launcher-resume.stderr"
)"; then
  cat "$fixture/launcher-resume.stderr" >&2
  exit 1
fi
[[ "$(launcher_session_count)" == $((sessions_before_resume + 1)) ]]
rg -Fxq -- '--resume' <<<"$resume_output"
rg -Fxq -- 'owned-resume-id' <<<"$resume_output"
[[ "$(rg -Fxc -- '--model' <<<"$resume_output")" == 1 ]]
[[ "$(awk 'previous == "--model" {print; exit} {previous=$0}' \
  <<<"$resume_output")" == 'gpt-5.6-sol' ]]
[[ "$(rg -Fxc -- '--plugin-dir' <<<"$resume_output")" == 1 ]]
resume_plugin_dir="$(awk \
  'previous == "--plugin-dir" {print; exit} {previous=$0}' \
  <<<"$resume_output")"
[[ "$resume_plugin_dir" == \
  "$launcher_data/state/sessions/run."*/plugin ]]
[[ "$resume_plugin_dir" != "$session_plugin_dir" ]]
[[ -f "${resume_plugin_dir%/plugin}/effective-models.json" ]]
[[ ! -e "$fixture"/claudex-launch-models.* ]]
[[ ! -s "$fixture/launcher-resume.stderr" ]]

bash -c '
  set +e +u
  set +o pipefail
  source "$1"
  case "$-" in *e*|*u*) exit 1 ;; esac
  [[ "$(set -o | awk '\''$1 == "pipefail" {print $2}'\'')" == off ]]
' _ "$ROOT/discover-models.sh"

render_claudex_config "$fixture/claudex.toml" \
  gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol \
  claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8 \
  /portable/bin/claude 18317 18787 13457
rg -q '^claude_binary = "/portable/bin/claude"$' "$fixture/claudex.toml"
rg -q '^proxy_port = 13457$' "$fixture/claudex.toml"
rg -q '^base_url = "http://127.0.0.1:18787"$' "$fixture/claudex.toml"
rg -q '^X-Headroom-Base-Url = "http://127.0.0.1:18317"$' "$fixture/claudex.toml"
rg -q '^sonnet = "claude-sonnet-5"$' "$fixture/claudex.toml"
rg -q '^opus = "claude-opus-4-8"$' "$fixture/claudex.toml"
rg -q '^balanced = "gpt-5.6-terra"$' "$fixture/claudex.toml"

render_cliproxy_config "$fixture/cliproxy.yaml" /portable/auth 18317
rg -q '^port: 18317$' "$fixture/cliproxy.yaml"

jq -n '{data:[
  {id:"gpt-5.6-luna"},{id:"gpt-5.6-terra"},{id:"gpt-5.6-sol"},
  {id:"claude-haiku-4-5-20251001"},{id:"claude-sonnet-5"},
  {id:"claude-opus-4-8"}
]}' >"$fixture/models.json"
render_discovered_claudex_config \
  "$fixture/models.json" "$fixture/discovered.toml" 18317 18787 13457
rg -q '^default_model = "gpt-5.6-sol"$' "$fixture/discovered.toml"
rg -q '^proxy_port = 13457$' "$fixture/discovered.toml"
rg -q '^opus = "claude-opus-4-8"$' "$fixture/discovered.toml"
rg -q '^base_url = "http://127.0.0.1:18787"$' "$fixture/discovered.toml"
rg -q '^X-Headroom-Base-Url = "http://127.0.0.1:18317"$' \
  "$fixture/discovered.toml"

for runtime_consumer in \
  bin/claude-headroom discover-models.sh doctor.sh \
  controller/plugin/scripts/check-local-services.sh
do
  rg -Fq 'read_service_ports' "$ROOT/$runtime_consumer"
done

endpoint_lock_data="$(cd "$fixture" && pwd -P)/endpoint-lock-data"
install -d "$endpoint_lock_data/model-config"
endpoint_lock_token="test:$$:$RANDOM"
acquire_endpoint_config_lock "$endpoint_lock_data" "$endpoint_lock_token"
if CLAUDEX_DATA_DIR="$endpoint_lock_data" discover_models_main \
    2>"$fixture/endpoint-lock.stderr"; then
  printf 'discovery ignored an active endpoint transaction lock\n' >&2
  exit 1
fi
rg -q 'endpoint model publication is already locked' \
  "$fixture/endpoint-lock.stderr"
release_endpoint_config_lock "$endpoint_lock_data" "$endpoint_lock_token"
[[ ! -e "$endpoint_lock_data/model-config/endpoint.lock" && \
   ! -L "$endpoint_lock_data/model-config/endpoint.lock" ]]

generation_data="$fixture/generation-data"
install -d "$generation_data"
printf 'old models\n' >"$generation_data/models.json"
printf 'old config\n' >"$generation_data/claudex.toml"
migrate_legacy_model_config "$generation_data"
old_generation="$(resolve_model_config_generation "$generation_data")"
[[ "$(cat "$old_generation/models.json")" == 'old models' ]]
[[ "$(cat "$old_generation/claudex.toml")" == 'old config' ]]
[[ -L "$generation_data/models.json" && -L "$generation_data/claudex.toml" ]]

generation_root="$generation_data/model-config"
stale_publication_lock="$generation_root/publication.lock"
mkdir "$stale_publication_lock"
printf '99999999\n' >"$stale_publication_lock/pid"
stale_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'stale models\n' >"$stale_candidate/models.json"
printf 'stale config\n' >"$stale_candidate/claudex.toml"
if activate_model_config_generation "$generation_data" "$stale_candidate" \
  2>"$fixture/stale-publication.stderr"; then
  printf 'stale publication lock was reclaimed instead of failing closed\n' >&2
  exit 1
fi
[[ -d "$stale_publication_lock" && ! -e "$stale_candidate" ]]
outside_candidate="$fixture/outside-candidate"
mkdir "$outside_candidate"
printf 'outside models\n' >"$outside_candidate/models.json"
printf 'outside config\n' >"$outside_candidate/claudex.toml"
if activate_model_config_generation "$generation_data" "$outside_candidate" \
  2>"$fixture/outside-publication.stderr"; then
  printf 'outside publication candidate was accepted\n' >&2
  exit 1
fi
[[ -d "$outside_candidate" ]]
rm -rf -- "$stale_publication_lock"

new_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'new models\n' >"$new_candidate/models.json"
printf 'new config\n' >"$new_candidate/claudex.toml"
overlapping_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'other writer still rendering\n' >"$overlapping_candidate/in-progress"
(
  : >"$fixture/generation-reader-ready"
  for _ in {1..100}; do
    observed_target="$(readlink "$generation_root/current")" || continue
    observed_models="$(cat "$generation_root/$observed_target/models.json" 2>/dev/null)" || continue
    observed_config="$(cat "$generation_root/$observed_target/claudex.toml" 2>/dev/null)" || continue
    [[ "$(readlink "$generation_root/current" 2>/dev/null || true)" == \
       "$observed_target" ]] || continue
    case "$observed_models:$observed_config" in
      'old models:old config'|'new models:new config') ;;
      *)
        printf '%s|%s\n' "$observed_models" "$observed_config" \
          >"$fixture/mixed-generation"
        exit 1
        ;;
    esac
  done
) &
generation_reader_pid=$!
while [[ ! -e "$fixture/generation-reader-ready" ]]; do :; done
activate_model_config_generation "$generation_data" "$new_candidate"
wait "$generation_reader_pid"
[[ ! -e "$fixture/mixed-generation" ]]
[[ "$(cat "$overlapping_candidate/in-progress")" == \
   'other writer still rendering' ]]
rm -rf -- "$overlapping_candidate"
active_generation="$(resolve_model_config_generation "$generation_data")"
[[ "$(cat "$active_generation/models.json")" == 'new models' ]]
[[ "$(cat "$active_generation/claudex.toml")" == 'new config' ]]
[[ "$(find "$generation_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]

transaction_data="$fixture/transaction-generation-data"
install -d "$transaction_data"
printf 'rollback models\n' >"$transaction_data/models.json"
printf 'rollback config\n' >"$transaction_data/claudex.toml"
migrate_legacy_model_config "$transaction_data"
transaction_root="$transaction_data/model-config"
rollback_target="$(readlink "$transaction_root/current")"
deferred_candidate="$(mktemp -d "$transaction_root/candidate.XXXXXX")"
printf 'deferred models\n' >"$deferred_candidate/models.json"
printf 'deferred config\n' >"$deferred_candidate/claudex.toml"
CLAUDEX_DEFER_MODEL_PRUNE=1 \
  activate_model_config_generation "$transaction_data" "$deferred_candidate"
[[ "$(find "$transaction_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 2 ]]
rollback_snapshot="$fixture/prior-model-generation"
cp -pPR "$transaction_root/$rollback_target" "$rollback_snapshot"
rm -rf -- "$transaction_root/$rollback_target"
restore_model_config_generation \
  "$transaction_data" "$rollback_target" "$rollback_snapshot"
[[ "$(readlink "$transaction_root/current")" == "$rollback_target" ]]
[[ "$(find "$transaction_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]

deferred_candidate="$(mktemp -d "$transaction_root/candidate.XXXXXX")"
printf 'committed models\n' >"$deferred_candidate/models.json"
printf 'committed config\n' >"$deferred_candidate/claudex.toml"
CLAUDEX_DEFER_MODEL_PRUNE=1 \
  activate_model_config_generation "$transaction_data" "$deferred_candidate"
prune_model_config_generations "$transaction_data"
[[ "$(find "$transaction_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]

failure_current_target="$(readlink "$generation_root/current")"
failure_peer_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'peer writer in progress\n' >"$failure_peer_candidate/in-progress"
install -d "$fixture/generation-failure-bin"

mv_failure_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'mv failure models\n' >"$mv_failure_candidate/models.json"
printf 'mv failure config\n' >"$mv_failure_candidate/claudex.toml"
mv_failure_generation="$generation_root/generation.${mv_failure_candidate##*.}"
real_mv="$(command -v mv)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == "${FAIL_MODEL_CANDIDATE:-}" ]]; then' \
  '  "$REAL_MV" "$@"' \
  '  exit 31' \
  'fi' \
  'exec "$REAL_MV" "$@"' >"$fixture/generation-failure-bin/mv"
chmod 0755 "$fixture/generation-failure-bin/mv"
if FAIL_MODEL_CANDIDATE="$mv_failure_candidate" REAL_MV="$real_mv" \
  PATH="$fixture/generation-failure-bin:$PATH" \
  activate_model_config_generation "$generation_data" "$mv_failure_candidate"; then
  printf 'candidate rename failure reported success\n' >&2
  exit 1
fi
[[ ! -e "$mv_failure_candidate" && ! -e "$mv_failure_generation" ]]
[[ ! -e "$generation_root/publication.lock" && \
   ! -L "$generation_root/publication.lock" ]]
[[ "$(readlink "$generation_root/current")" == "$failure_current_target" ]]
[[ "$(cat "$failure_peer_candidate/in-progress")" == 'peer writer in progress' ]]
rm -f -- "$fixture/generation-failure-bin/mv"

ln_failure_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'ln failure models\n' >"$ln_failure_candidate/models.json"
printf 'ln failure config\n' >"$ln_failure_candidate/claudex.toml"
ln_failure_generation="$generation_root/generation.${ln_failure_candidate##*.}"
real_ln="$(command -v ln)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == -s && "$2" == generation.* ]]; then exit 32; fi' \
  'exec "$REAL_LN" "$@"' >"$fixture/generation-failure-bin/ln"
chmod 0755 "$fixture/generation-failure-bin/ln"
if REAL_LN="$real_ln" PATH="$fixture/generation-failure-bin:$PATH" \
  activate_model_config_generation "$generation_data" "$ln_failure_candidate"; then
  printf 'generation symlink creation failure reported success\n' >&2
  exit 1
fi
[[ ! -e "$ln_failure_candidate" && ! -e "$ln_failure_generation" ]]
[[ "$(readlink "$generation_root/current")" == "$failure_current_target" ]]
[[ "$(cat "$failure_peer_candidate/in-progress")" == 'peer writer in progress' ]]
rm -f -- "$fixture/generation-failure-bin/ln"

interrupted_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'interrupted models\n' >"$interrupted_candidate/models.json"
printf 'interrupted config\n' >"$interrupted_candidate/claudex.toml"
interrupted_generation="$generation_root/generation.${interrupted_candidate##*.}"
install -d "$fixture/generation-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "${3:-}" == "$GENERATION_CURRENT" ]]; then exit 25; fi' \
  'exec "$REAL_PYTHON3" "$@"' >"$fixture/generation-bin/python3"
chmod 0755 "$fixture/generation-bin/python3"
if GENERATION_CURRENT="$generation_root/current" \
  REAL_PYTHON3="$(command -v python3)" \
  PATH="$fixture/generation-bin:$PATH" \
  activate_model_config_generation "$generation_data" "$interrupted_candidate"; then
  printf 'interrupted generation pointer swap reported success\n' >&2
  exit 1
fi
active_generation="$(resolve_model_config_generation "$generation_data")"
[[ "$(cat "$active_generation/models.json")" == 'new models' ]]
[[ "$(cat "$active_generation/claudex.toml")" == 'new config' ]]
[[ "$(find "$generation_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]
[[ ! -e "$interrupted_candidate" && ! -e "$interrupted_generation" ]]
[[ "$(readlink "$generation_root/current")" == "$failure_current_target" ]]
[[ "$(cat "$failure_peer_candidate/in-progress")" == 'peer writer in progress' ]]

replace_error_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'replace error models\n' >"$replace_error_candidate/models.json"
printf 'replace error config\n' >"$replace_error_candidate/claudex.toml"
replace_error_generation="$generation_root/generation.${replace_error_candidate##*.}"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "${3:-}" == "$GENERATION_CURRENT" ]]; then' \
  '  "$REAL_PYTHON3" -c '\''import os, sys; os.replace(sys.argv[1], sys.argv[2])'\'' "$2" "$3"' \
  '  exit 26' \
  'fi' \
  'exec "$REAL_PYTHON3" "$@"' >"$fixture/generation-bin/python3"
chmod 0755 "$fixture/generation-bin/python3"
if ! GENERATION_CURRENT="$generation_root/current" \
  REAL_PYTHON3="$(command -v python3)" \
  PATH="$fixture/generation-bin:$PATH" \
  activate_model_config_generation "$generation_data" "$replace_error_candidate"; then
  printf 'replace-then-error publication was not recognized as successful\n' >&2
  exit 1
fi
replace_error_target="$(basename "$replace_error_generation")"
[[ "$(readlink "$generation_root/current")" == "$replace_error_target" ]]
[[ -d "$replace_error_generation" && ! -e "$replace_error_candidate" ]]
active_generation="$(resolve_model_config_generation "$generation_data")"
[[ "$(cat "$active_generation/models.json")" == 'replace error models' ]]
[[ "$(cat "$active_generation/claudex.toml")" == 'replace error config' ]]
[[ "$(cat "$failure_peer_candidate/in-progress")" == 'peer writer in progress' ]]
[[ "$(find "$generation_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]
rm -rf -- "$failure_peer_candidate"

publisher_one="$(mktemp -d "$generation_root/candidate.XXXXXX")"
publisher_two="$(mktemp -d "$generation_root/candidate.XXXXXX")"
printf 'publisher one models\n' >"$publisher_one/models.json"
printf 'publisher one config\n' >"$publisher_one/claudex.toml"
printf 'publisher two models\n' >"$publisher_two/models.json"
printf 'publisher two config\n' >"$publisher_two/claudex.toml"
install -d "$fixture/publisher-bin"
real_mv="$(command -v mv)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$1" == "$BLOCKED_PUBLISHER" ]]; then' \
  '  : >"$PUBLISHER_LOCKED"' \
  '  while [[ ! -e "$PUBLISHER_RELEASE" ]]; do :; done' \
  'fi' \
  'exec "$REAL_MV" "$@"' >"$fixture/publisher-bin/mv"
chmod 0755 "$fixture/publisher-bin/mv"
BLOCKED_PUBLISHER="$publisher_one" \
  PUBLISHER_LOCKED="$fixture/publisher-locked" \
  PUBLISHER_RELEASE="$fixture/publisher-release" REAL_MV="$real_mv" \
  PATH="$fixture/publisher-bin:$PATH" \
  activate_model_config_generation "$generation_data" "$publisher_one" &
publisher_one_pid=$!
while [[ ! -e "$fixture/publisher-locked" ]]; do :; done
if activate_model_config_generation "$generation_data" "$publisher_two" \
  2>"$fixture/publisher-two.stderr"; then
  printf 'concurrent publisher was not rejected\n' >&2
  exit 1
fi
[[ ! -e "$publisher_two" ]]
active_during_publication="$(resolve_model_config_generation "$generation_data")"
[[ -d "$active_during_publication" ]]
: >"$fixture/publisher-release"
wait "$publisher_one_pid"
active_generation="$(resolve_model_config_generation "$generation_data")"
[[ "$(cat "$active_generation/models.json")" == 'publisher one models' ]]
[[ "$(find "$generation_root" -mindepth 1 -maxdepth 1 -type d \
  -name 'generation.*' | wc -l | tr -d ' ')" == 1 ]]
[[ ! -e "$generation_root/publication.lock" && \
   ! -L "$generation_root/publication.lock" ]]

case "$(uname -s)" in
  Darwin)
    [[ -x /bin/bash ]]
    signal_test_bash=/bin/bash
    ;;
  *) signal_test_bash="$(command -v bash)" ;;
esac
if rg -Fq '$BASHPID' "$ROOT/lib/workflow.sh"; then
  printf 'publication signal handling requires newer-than-system Bash\n' >&2
  exit 1
fi
install -d "$fixture/signal-acquisition-bin"
real_mkdir="$(command -v mkdir)"
real_python3="$(command -v python3)"
for wrapped_command in mkdir python3; do
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'set -euo pipefail' \
    'if [[ "$SIGNAL_ACQUIRE_PHASE" == before ]]; then' \
    '  : >"$SIGNAL_ACQUIRE_BLOCKED"' \
    '  while [[ ! -e "$SIGNAL_ACQUIRE_RELEASE" ]]; do :; done' \
    '  exit 77' \
    'fi' \
    'if [[ "${0##*/}" == mkdir ]]; then "$REAL_MKDIR" "$@"; else "$REAL_PYTHON3" "$@"; fi' \
    ': >"$SIGNAL_ACQUIRE_BLOCKED"' \
    'while [[ ! -e "$SIGNAL_ACQUIRE_RELEASE" ]]; do :; done' \
    'exit 0' >"$fixture/signal-acquisition-bin/$wrapped_command"
  chmod 0755 "$fixture/signal-acquisition-bin/$wrapped_command"
done
for acquisition_phase in before after; do
  for publication_signal in HUP INT TERM; do
    signal_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
    printf 'signal models\n' >"$signal_candidate/models.json"
    printf 'signal config\n' >"$signal_candidate/claudex.toml"
    signal_prefix="$fixture/publication-$acquisition_phase-$publication_signal"
    case "$publication_signal" in
      HUP) signal_expected_status=129 ;;
      INT) signal_expected_status=130 ;;
      TERM) signal_expected_status=143 ;;
    esac
    (
      while [[ ! -e "$signal_prefix-worker-pid" || \
               ! -e "$signal_prefix-blocked" ]]; do :; done
      kill -"$publication_signal" "$(cat "$signal_prefix-worker-pid")"
      : >"$signal_prefix-release"
    ) &
    signal_sender_pid=$!
    set +e
    SIGNAL_NAME="$publication_signal" \
      SIGNAL_EXPECTED_STATUS="$signal_expected_status" \
      SIGNAL_CHAIN_MARKER="$signal_prefix-chained" \
      SIGNAL_WORKER_PID="$signal_prefix-worker-pid" \
      SIGNAL_ACQUIRE_PHASE="$acquisition_phase" \
      SIGNAL_ACQUIRE_BLOCKED="$signal_prefix-blocked" \
      SIGNAL_ACQUIRE_RELEASE="$signal_prefix-release" \
      REAL_MKDIR="$real_mkdir" REAL_PYTHON3="$real_python3" \
      PATH="$fixture/signal-acquisition-bin:$PATH" \
      "$signal_test_bash" -c '
        set -euo pipefail
        source "$1"
        if [[ "$SIGNAL_NAME" == INT ]]; then
          acquire_model_publication_lock() {
            local data_root="$1" lock_token="$2" lock_dir
            lock_dir="$(model_config_root "$data_root")/publication.lock"
            if [[ "$SIGNAL_ACQUIRE_PHASE" == after ]]; then
              "$REAL_PYTHON3" - "$lock_token" "$lock_dir" <<'\''PY'\''
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
              MODEL_PUBLICATION_LOCK_DIR="$lock_dir"
              MODEL_PUBLICATION_LOCK_IDENTITY="$lock_token"
            fi
            : >"$SIGNAL_ACQUIRE_BLOCKED"
            while [[ ! -e "$SIGNAL_ACQUIRE_RELEASE" ]]; do :; done
            [[ "$SIGNAL_ACQUIRE_PHASE" == after ]]
          }
        fi
        trap "printf chained >\"$SIGNAL_CHAIN_MARKER\"; exit $SIGNAL_EXPECTED_STATUS" "$SIGNAL_NAME"
        "$BASH" -c '\''printf "%s" "$PPID" >"$1"'\'' _ "$SIGNAL_WORKER_PID"
        activate_model_config_generation "$2" "$3"
      ' _ "$ROOT/lib/workflow.sh" "$generation_data" "$signal_candidate"
    signal_status=$?
    set -e
    wait "$signal_sender_pid"
    [[ "$signal_status" == "$signal_expected_status" ]]
    [[ -e "$signal_prefix-chained" ]] || {
      printf 'restored %s trap did not run during %s acquisition\n' \
        "$publication_signal" "$acquisition_phase" >&2
      exit 1
    }
    [[ ! -e "$generation_root/publication.lock" && \
       ! -L "$generation_root/publication.lock" ]]
    rm -rf -- "$signal_candidate"
  done
done

# Bash 3.2 keeps $$ bound to the outer shell in a parenthesized context. Signal
# re-delivery must target the context running publication, not its controller.
for publication_signal in HUP INT TERM; do
  subshell_candidate="$(mktemp -d "$generation_root/candidate.XXXXXX")"
  printf 'subshell signal models\n' >"$subshell_candidate/models.json"
  printf 'subshell signal config\n' >"$subshell_candidate/claudex.toml"
  subshell_prefix="$fixture/publication-subshell-$publication_signal"
  case "$publication_signal" in
    HUP) subshell_expected_status=129 ;;
    INT) subshell_expected_status=130 ;;
    TERM) subshell_expected_status=143 ;;
  esac
  set +e
  SIGNAL_NAME="$publication_signal" \
    SIGNAL_EXPECTED_STATUS="$subshell_expected_status" \
    SIGNAL_CHAIN_MARKER="$subshell_prefix-chained" \
    SIGNAL_PARENT_SURVIVED="$subshell_prefix-parent-survived" \
    SIGNAL_CHILD_STATUS="$subshell_prefix-child-status" \
    SIGNAL_WORKER_PID="$subshell_prefix-worker-pid" \
    SIGNAL_ACQUIRE_PHASE=after \
    SIGNAL_ACQUIRE_BLOCKED="$subshell_prefix-blocked" \
    SIGNAL_ACQUIRE_RELEASE="$subshell_prefix-release" \
    REAL_MKDIR="$real_mkdir" REAL_PYTHON3="$real_python3" \
    PATH="$fixture/signal-acquisition-bin:$PATH" \
    "$signal_test_bash" -c '
      set -euo pipefail
      source "$1"
      if [[ "$SIGNAL_NAME" == INT ]]; then
        acquire_model_publication_lock() {
          local data_root="$1" lock_token="$2" lock_dir
          lock_dir="$(model_config_root "$data_root")/publication.lock"
          if [[ "$SIGNAL_ACQUIRE_PHASE" == after ]]; then
            "$REAL_PYTHON3" - "$lock_token" "$lock_dir" <<'\''PY'\''
import os
import sys
os.symlink(sys.argv[1], sys.argv[2])
PY
            MODEL_PUBLICATION_LOCK_DIR="$lock_dir"
            MODEL_PUBLICATION_LOCK_IDENTITY="$lock_token"
          fi
          : >"$SIGNAL_ACQUIRE_BLOCKED"
          while [[ ! -e "$SIGNAL_ACQUIRE_RELEASE" ]]; do :; done
          [[ "$SIGNAL_ACQUIRE_PHASE" == after ]]
        }
      fi
      (
        while [[ ! -e "$SIGNAL_WORKER_PID" || \
                 ! -e "$SIGNAL_ACQUIRE_BLOCKED" ]]; do :; done
        kill -"$SIGNAL_NAME" "$(cat "$SIGNAL_WORKER_PID")"
        : >"$SIGNAL_ACQUIRE_RELEASE"
      ) &
      signal_sender=$!
      set +e
      (
        trap "printf chained >\"$SIGNAL_CHAIN_MARKER\"; exit $SIGNAL_EXPECTED_STATUS" "$SIGNAL_NAME"
        "$BASH" -c '\''printf "%s" "$PPID" >"$1"'\'' _ "$SIGNAL_WORKER_PID"
        activate_model_config_generation "$2" "$3"
      )
      child_status=$?
      set -e
      wait "$signal_sender"
      printf "%s" "$child_status" >"$SIGNAL_CHILD_STATUS"
      : >"$SIGNAL_PARENT_SURVIVED"
      [[ "$child_status" == "$SIGNAL_EXPECTED_STATUS" ]]
    ' _ "$ROOT/lib/workflow.sh" "$generation_data" "$subshell_candidate"
  signal_controller_status=$?
  set -e
  [[ "$signal_controller_status" == 0 ]]
  [[ -e "$subshell_prefix-parent-survived" ]]
  [[ -e "$subshell_prefix-chained" ]] || {
    printf 'restored %s trap did not run in publication subshell\n' \
      "$publication_signal" >&2
    exit 1
  }
  [[ "$(cat "$subshell_prefix-child-status")" == \
     "$subshell_expected_status" ]]
  [[ ! -e "$generation_root/publication.lock" && \
     ! -L "$generation_root/publication.lock" ]]
  rm -rf -- "$subshell_candidate"
done

rg -Fq 'model_config_file "$WORKFLOW_DATA_ROOT" claudex.toml' \
  "$ROOT/bin/claudex-gpt" "$ROOT/doctor.sh"
rg -Fq 'mktemp -d "$generation_root/candidate.XXXXXX"' \
  "$ROOT/discover-models.sh"
if rg -q '\$WORKFLOW_DATA_ROOT/claudex\.toml' \
  "$ROOT/bin/claudex-gpt" "$ROOT/doctor.sh"; then
  printf 'local consumer bypasses the model-config generation pointer\n' >&2
  exit 1
fi

fixture_physical="$(cd "$fixture" && pwd -P)"
discovery_data="$fixture_physical/discovery-data"
install -d "$discovery_data/bin" "$fixture/discovery-bin"
printf 'old models\n' >"$discovery_data/models.json"
printf 'old mapping\n' >"$discovery_data/claudex.toml"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "curl unexpectedly ran while sourcing discovery\n" >"$DISCOVERY_SOURCE_MARKER"' \
  'exit 1' >"$fixture/discovery-bin/curl"
chmod 0755 "$fixture/discovery-bin/curl"
DISCOVERY_SOURCE_MARKER="$fixture/discovery-source-called" \
  PATH="$fixture/discovery-bin:$PATH" source "$ROOT/discover-models.sh"
[[ ! -e "$fixture/discovery-source-called" ]]

install -d "$fixture/discovery-validation-bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "%s\n" "$*" >"$DISCOVERY_INSTALL_MARKER"' \
  'exit 88' >"$fixture/discovery-validation-bin/install"
chmod 0755 "$fixture/discovery-validation-bin/install"
discovery_checkout_alias="$fixture/discovery-checkout-alias"
ln -s "$ROOT" "$discovery_checkout_alias"
for unsafe_discovery_root in \
  / // /tmp/.. "$fixture/discovery-home/." "$ROOT" "$ROOT/nested" \
  "$discovery_checkout_alias/nested"
do
  discovery_install_marker="$fixture/discovery-install-marker"
  rm -f -- "$discovery_install_marker"
  if HOME="$fixture/discovery-home" CLAUDEX_DATA_DIR="$unsafe_discovery_root" \
    DISCOVERY_INSTALL_MARKER="$discovery_install_marker" \
    PATH="$fixture/discovery-validation-bin:$PATH" \
    discover_models_main >/dev/null 2>&1; then
    printf 'unsafe standalone discovery root was accepted: %s\n' \
      "$unsafe_discovery_root" >&2
    exit 1
  fi
  [[ ! -e "$discovery_install_marker" ]]
done
safe_discovery_parent="$fixture_physical/safe-discovery"
install -d "$safe_discovery_parent/existing"
safe_discovery_marker="$fixture/safe-discovery-install-marker"
HOME="$fixture/discovery-home" \
  CLAUDEX_DATA_DIR="$safe_discovery_parent/existing/../existing/new/child" \
  DISCOVERY_INSTALL_MARKER="$safe_discovery_marker" \
  PATH="$fixture/discovery-validation-bin:$PATH" \
  discover_models_main >/dev/null 2>&1 || true
rg -Fq -- "-d -m 0700 $safe_discovery_parent/existing/new/child" \
  "$safe_discovery_marker"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'output=' \
  'while (($#)); do' \
  '  case "$1" in' \
  '    --output) output="$2"; shift 2 ;;' \
  '    *) shift ;;' \
  '  esac' \
  'done' \
  'cp "$DISCOVERY_MODELS_FIXTURE" "${output:-/dev/stdout}"' \
  >"$fixture/discovery-bin/curl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'exit 23' >"$discovery_data/bin/claudex"
chmod 0755 "$fixture/discovery-bin/curl" "$discovery_data/bin/claudex"
if CLAUDEX_DATA_DIR="$discovery_data" \
  DISCOVERY_MODELS_FIXTURE="$fixture/models.json" \
  PATH="$fixture/discovery-bin:$PATH" \
  discover_models_main 2>"$fixture/discovery-validation.stderr"; then
  printf 'invalid discovered mapping was activated\n' >&2
  exit 1
fi
[[ "$(cat "$discovery_data/models.json")" == 'old models' ]]
[[ "$(cat "$discovery_data/claudex.toml")" == 'old mapping' ]]
rg -q 'claudex-login codex.*claudex-login claude.*install.sh' \
  "$fixture/discovery-validation.stderr"

printf '{"data":[]}\n' >"$fixture/discovery-empty.json"
if CLAUDEX_DATA_DIR="$discovery_data" \
  DISCOVERY_MODELS_FIXTURE="$fixture/discovery-empty.json" \
  PATH="$fixture/discovery-bin:$PATH" \
  discover_models_main 2>"$fixture/discovery-generation.stderr"; then
  printf 'incomplete discovered model set was activated\n' >&2
  exit 1
fi
[[ "$(cat "$discovery_data/models.json")" == 'old models' ]]
[[ "$(cat "$discovery_data/claudex.toml")" == 'old mapping' ]]
rg -q 'claudex-login codex.*claudex-login claude.*install.sh' \
  "$fixture/discovery-generation.stderr"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'exit 0' >"$discovery_data/bin/claudex"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "${3:-}" == "$DISCOVERY_CONFIG_DEST" ]]; then' \
  '  exit 24' \
  'fi' \
  'exec "$REAL_PYTHON3" "$@"' >"$fixture/discovery-bin/python3"
chmod 0755 "$discovery_data/bin/claudex" "$fixture/discovery-bin/python3"
if CLAUDEX_DATA_DIR="$discovery_data" \
  DISCOVERY_MODELS_FIXTURE="$fixture/models.json" \
  DISCOVERY_CONFIG_DEST="$discovery_data/model-config/current" \
  REAL_PYTHON3="$(command -v python3)" \
  PATH="$fixture/discovery-bin:$PATH" \
  discover_models_main 2>"$fixture/discovery-activation.stderr"; then
  printf 'partially activated discovered mapping reported success\n' >&2
  exit 1
fi
[[ "$(cat "$discovery_data/models.json")" == 'old models' ]]
[[ "$(cat "$discovery_data/claudex.toml")" == 'old mapping' ]]
rg -q 'claudex-login codex.*claudex-login claude.*install.sh' \
  "$fixture/discovery-activation.stderr"

rg -q 'Graphify is present' "$ROOT/controller/controller-policy.md"
rg -q 'Use MemPalace automatically' "$ROOT/controller/controller-policy.md"
rg -q 'Docker MCP profile selected' "$ROOT/controller/controller-policy.md"
rg -q 'Lead user-facing updates and final responses with the outcome' \
  "$ROOT/controller/controller-policy.md"
rg -q 'give one concise opening update' \
  "$ROOT/controller/controller-policy.md"
rg -q 'update only on a material' \
  "$ROOT/controller/controller-policy.md"
rg -q 'approval need, or wait beyond 60 seconds' \
  "$ROOT/controller/controller-policy.md"
rg -q 'tool calls, interface activity, or repeat plans already visible' \
  "$ROOT/controller/controller-policy.md"
rg -q 'For multi-repository or ordered handoffs' \
  "$ROOT/controller/controller-policy.md"
rg -q 'numbered headings, consistent semantic markers, and syntax-highlighted command blocks' \
  "$ROOT/controller/controller-policy.md"
rg -q 'Never rely on color alone or emit raw ANSI escapes' \
  "$ROOT/controller/controller-policy.md"
rg -q 'Number only ordered steps' "$ROOT/controller/controller-policy.md"
rg -q 'Omit unrelated sidebars and speculative work' \
  "$ROOT/controller/controller-policy.md"
rg -q 'State what changed' "$ROOT/controller/controller-policy.md"
rg -q 'Give a next action only when the user must act' \
  "$ROOT/controller/controller-policy.md"
rg -q 'Never omit blockers, uncertainty, validation, safety, rollback' \
  "$ROOT/controller/controller-policy.md"
rg -q 'self-contained handoff' "$ROOT/controller/controller-policy.md"
rg -q 'Commit attribution is disabled' "$ROOT/controller/controller-policy.md"
rg -q 'without attribution trailers. Never require a Co-Authored-By, Claude-Session,' \
  "$ROOT/controller/controller-policy.md"
rg -q 'or AI/tool attribution trailer' \
  "$ROOT/controller/controller-policy.md"
test ! -d "$ROOT/integrations/docker"

guard="$ROOT/controller/plugin/scripts/guard-orchestration.sh"

invoke_agent_guard() {
  local agent_type="$1"
  local isolation="${2:-}"
  jq -cn \
    --arg agent_type "$agent_type" \
    --arg isolation "$isolation" \
    '{
      tool_name: "Agent",
      tool_input: (
        {subagent_type: $agent_type} +
        (if $isolation == "" then {} else {isolation: $isolation} end)
      )
    }' |
    CLAUDE_PLUGIN_ROOT="$ROOT/controller/plugin" "$guard"
}

generic_denial="$(invoke_agent_guard Explore)"
jq -e '
  .hookSpecificOutput.permissionDecision == "deny" and
  (.hookSpecificOutput.permissionDecisionReason |
    contains("Do not retry") and contains("do not escalate"))
' >/dev/null <<<"$generic_denial"

for read_only_agent in \
  claudex-controller:repository-explorer \
  claudex-controller:repository-verifier \
  claudex-controller:correctness-critic \
  claudex-controller:architecture-advisor
do
  isolation_denial="$(invoke_agent_guard "$read_only_agent" worktree)"
  jq -e '
    .hookSpecificOutput.permissionDecision == "deny" and
    (.hookSpecificOutput.permissionDecisionReason |
      contains("read-only") and contains("current checkout"))
  ' >/dev/null <<<"$isolation_denial"
done

[[ -z "$(invoke_agent_guard claudex-controller:repository-explorer)" ]]
[[ -z "$(invoke_agent_guard claudex-controller:implementation-worker worktree)" ]]

for role in \
  repository-explorer \
  repository-verifier \
  correctness-critic \
  architecture-advisor \
  implementation-worker
do
  agent_file="$ROOT/controller/plugin/agents/$role.md"
  rg -q "^name: $role$" "$agent_file"
  rg -q '^model: inherit$' "$agent_file"
done

rg -q 'Never invoke generic `Explore`, `Plan`, `general-purpose`, or `Bash` agents' \
  "$ROOT/controller/controller-policy.md"
rg -Uq 'A rejected orchestration call is\s+not evidence' \
  "$ROOT/controller/controller-policy.md"
rg -q 'Only implementation-worker may request worktree isolation' \
  "$ROOT/controller/controller-policy.md"
rg -q 'allowed bounded replacement for generic repository exploration' \
  "$ROOT/controller/plugin/agents/repository-explorer.md"
rg -q 'not a replacement for generic planning' \
  "$ROOT/controller/plugin/agents/architecture-advisor.md"

# Sol receives complete independent evidence directly; an extra mandatory
# synthesis turn would add latency and token burn without increasing worker
# strength. Generous string ceilings only stop pathological repetition.
test ! -e "$ROOT/controller/plugin/agents/sonnet-synthesizer.md"
! rg -q 'sonnet-synthesizer|SYNTHESIS_SCHEMA|phase: .Synthesize' \
  "$ROOT/controller/plugin/workflows/investigate.js" \
  "$ROOT/controller/plugin/scripts/guard-orchestration.sh"
rg -q "maxLength: 16000" "$ROOT/controller/plugin/workflows/investigate.js"
rg -q "truncated: true" "$ROOT/controller/plugin/workflows/investigate.js"
rg -q "originalLength: sanitized.length" "$ROOT/controller/plugin/workflows/investigate.js"
rg -q "truncated: true" "$ROOT/controller/plugin/workflows/review.js"
rg -q "originalLength: sanitized.length" "$ROOT/controller/plugin/workflows/review.js"
rg -Fq "return { status, missingAgents, question, scope, evidence, adjudication }" \
  "$ROOT/controller/plugin/workflows/investigate.js"

printf 'PASS: controller and mixed-model workflow\n'
