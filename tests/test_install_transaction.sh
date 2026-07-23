#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-install-transaction.XXXXXX")"
fixture="$(cd "$fixture" && pwd -P)"
background_pids=("")
cleanup() {
  local process_id
  for process_id in "${background_pids[@]}"; do
    [[ -n "$process_id" ]] || continue
    kill "$process_id" 2>/dev/null || true
    wait "$process_id" 2>/dev/null || true
  done
  rm -rf -- "$fixture"
}
trap cleanup EXIT

fake_bin="$fixture/bin"
install -d "$fake_bin"
real_python="$(command -v python3)"
tool_bin="$(dirname "$(command -v rg)")"
archive_digest="$(printf archive | shasum -a 256 | awk '{print $1}')"
ca_file="$fixture/ca.pem"
printf 'fixture ca\n' >"$ca_file"

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
  'if [[ "$url" == *api.github.com/repos/router-for-me/CLIProxyAPI* ]]; then' \
  '  if [[ "$FAKE_UNAME_S" == Darwin ]]; then asset=CLIProxyAPI_1.0.0_darwin_aarch64.tar.gz; else asset=CLIProxyAPI_1.0.0_linux_aarch64.tar.gz; fi' \
  '  printf '\''{"tag_name":"v1.0.0","assets":[{"name":"%s","browser_download_url":"https://fixture/cliproxy.tar.gz","digest":"sha256:%s"}]}\n'\'' "$asset" "$FAKE_ARCHIVE_DIGEST" >"$output"' \
  'elif [[ "$url" == *api.github.com/repos/StringKe/claudex* ]]; then' \
  '  if [[ "$(cat "$FAKE_CLAUDEX_BUILD_STATE")" == 2 ]]; then version=1.0.1; else version=1.0.0; fi' \
  '  if [[ "$FAKE_UNAME_S" == Darwin ]]; then asset="claudex-v${version}-aarch64-apple-darwin.tar.gz"; else asset="claudex-v${version}-aarch64-unknown-linux-gnu.tar.gz"; fi' \
  '  printf '\''{"tag_name":"v%s","assets":[{"name":"%s","browser_download_url":"https://fixture/claudex.tar.gz","digest":"sha256:%s"}]}\n'\'' "$version" "$asset" "$FAKE_ARCHIVE_DIGEST" >"$output"' \
  'elif [[ "$url" == https://fixture/* ]]; then' \
  '  printf archive >"$output"' \
  'elif [[ "$url" == */health ]]; then' \
  '  printf '\''{"service":"headroom-proxy","status":"healthy","ready":true,"version":"1.0.0","config":{"optimize":true,"cache":false,"memory":false,"code_graph":false,"runtime_env":{"HEADROOM_OUTPUT_SHAPER":"0","HEADROOM_VERBOSITY_AUTOTUNE":"0","HEADROOM_EFFORT_ROUTER":"0"}}}\n'\''' \
  'elif [[ "$url" == */v1/models ]]; then' \
  '  endpoint="${url#*://127.0.0.1:}"' \
  '  port="${endpoint%%/*}"' \
  '  if [[ -f "$FAKE_SERVICE_STATE/preflight.port" ]] && [[ "$(cat "$FAKE_SERVICE_STATE/preflight.port")" == "$port" ]]; then' \
  '    [[ -f "$FAKE_SERVICE_STATE/preflight.ready" ]] || exit 7' \
  '    kill -0 "$(cat "$FAKE_SERVICE_STATE/preflight.pid")" 2>/dev/null || exit 7' \
  '    [[ "${FAKE_SWAP_DEFINITION_AFTER_PREFLIGHT:-0}" != 1 ]] || touch "$FAKE_SERVICE_STATE/proxy.foreign"' \
  '    case "${FAKE_MANAGER_FAILURE_AFTER_PREFLIGHT:-}" in state) touch "$FAKE_SERVICE_STATE/manager.state-fail" ;; pid) touch "$FAKE_SERVICE_STATE/manager.pid-fail"; printf "0\n" >"$FAKE_SERVICE_STATE/manager.print-count" ;; esac' \
  '    printf '\''{"object":"list","data":[{"id":"gpt-5.6-luna"},{"id":"gpt-5.6-terra"},{"id":"gpt-5.6-sol"},{"id":"claude-haiku-4-5-20251001"},{"id":"claude-sonnet-5"},{"id":"claude-opus-4-8"}]}\n'\''' \
  '  elif [[ -f "$FAKE_SERVICE_STATE/proxy.port" ]] && [[ "$(cat "$FAKE_SERVICE_STATE/proxy.port")" == "$port" ]]; then' \
  '    [[ -f "$FAKE_SERVICE_STATE/proxy.loaded" ]] || exit 7' \
  '    [[ ! -f "$FAKE_SERVICE_STATE/proxy.unready" ]] || exit 7' \
  '    printf '\''{"object":"list","data":[{"id":"gpt-5.6-luna"},{"id":"gpt-5.6-terra"},{"id":"gpt-5.6-sol"},{"id":"claude-haiku-4-5-20251001"},{"id":"claude-sonnet-5"},{"id":"claude-opus-4-8"}]}\n'\''' \
  '  elif [[ "$(cat "$FAKE_MODELS_STATE")" == full ]]; then' \
  '    printf '\''{"object":"list","data":[{"id":"gpt-5.6-luna"},{"id":"gpt-5.6-terra"},{"id":"gpt-5.6-sol"},{"id":"claude-haiku-4-5-20251001"},{"id":"claude-sonnet-5"},{"id":"claude-opus-4-8"}]}\n'\''' \
  '  else' \
  '    printf '\''{"object":"list","data":[]}\n'\''' \
  '  fi' \
  'else' \
  '  exit 22' \
  'fi' >"$fake_bin/curl"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'destination=' \
  'binary=' \
  'while (($#)); do' \
  '  case "$1" in -C) destination="$2"; shift 2 ;; -*) shift ;; *) binary="$1"; shift ;; esac' \
  'done' \
  'if [[ "$binary" == cli-proxy-api ]]; then' \
  '  printf '\''#!/usr/bin/env bash\nif [[ "${1:-}" == "--version" || "${1:-}" == "--help" ]]; then echo "CLIProxyAPI 1.0.0"; else /bin/sleep 300; fi\n'\'' >"$destination/$binary"' \
  'else' \
  '  build="$(cat "$FAKE_CLAUDEX_BUILD_STATE")"; if [[ "$build" == 2 ]]; then version=1.0.1; else version=1.0.0; fi' \
  '  printf '\''#!/usr/bin/env bash\nset -euo pipefail\n# build %s\nif [[ "${1:-}" == "--version" ]]; then echo "claudex %s"; exit 0; fi\nif [[ "$*" == *"config validate"* ]]; then printf "publish-validate\\n" >>"$FAKE_EVENT_LOG"; exit 0; fi\nport=\nwhile (($#)); do if [[ "$1" == --port ]]; then port="$2"; shift 2; else shift; fi; done\nprintf "preflight-start %%s\\n" "$port" >>"$FAKE_EVENT_LOG"\nprintf "%%s\\n" "$port" >"$FAKE_SERVICE_STATE/preflight.port"\nprintf "%%s\\n" "$$" >"$FAKE_SERVICE_STATE/preflight.pid"\ntouch "$FAKE_SERVICE_STATE/preflight.ready"\nexec /bin/sleep 300\n'\'' "$build" "$version" >"$destination/$binary"' \
  'fi' \
  'chmod 0755 "$destination/$binary"' >"$fake_bin/tar"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "${1:-}" == -c ]]; then' \
  '  case "$2" in *importlib.metadata*) printf "1.0.0\n" ;; *certifi*) printf "%s\n" "$FAKE_CA_FILE" ;; *) exit 1 ;; esac' \
  '  exit 0' \
  'fi' \
  'shift' \
  'if [[ "${1:-}" == --version ]]; then printf "headroom 1.0.0\n"; else exec /bin/sleep 300; fi' \
  >"$fake_bin/headroom-python"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'case "$*" in' \
  '  *mempalace*)' \
  '    install -d "$HOME/.local/bin"' \
  '    printf '\''#!/usr/bin/env bash\nexit 0\n'\'' >"$HOME/.local/bin/mempalace-mcp"' \
  '    chmod 0755 "$HOME/.local/bin/mempalace-mcp" ;;' \
  '  *graphifyy*)' \
  '    install -d "$HOME/.local/bin"' \
  '    printf '\''#!/usr/bin/env bash\nexit 0\n'\'' >"$HOME/.local/bin/graphify-mcp"' \
  '    chmod 0755 "$HOME/.local/bin/graphify-mcp" ;;' \
  '  *headroom-ai*)' \
  '    install -d "$UV_TOOL_BIN_DIR"' \
  '    printf '\''#!%s\n'\'' "$FAKE_HEADROOM_PYTHON" >"$UV_TOOL_BIN_DIR/headroom"' \
  '    printf '\''# fixture\n'\'' >>"$UV_TOOL_BIN_DIR/headroom"' \
  '    chmod 0755 "$UV_TOOL_BIN_DIR/headroom" ;;' \
  'esac' >"$fake_bin/uv"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *"mcp_probe.py"* ]]; then exit 0; fi' \
  'exec "$REAL_PYTHON" "$@"' >"$fake_bin/python3"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$fake_bin/claude"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'case "${1:-}" in -s) printf "%s\n" "$FAKE_UNAME_S" ;; -m) printf "aarch64\n" ;; *) printf "%s\n" "$FAKE_UNAME_S" ;; esac' \
  >"$fake_bin/uname"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$fake_bin/sleep"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "%s\n" "$*" >>"$FAKE_SERVICE_LOG"' \
  '[[ ! -f "$FAKE_SERVICE_STATE/manager.state-fail" ]] || exit 70' \
  'if [[ -f "$FAKE_SERVICE_STATE/manager.pid-fail" ]]; then count=$(( $(cat "$FAKE_SERVICE_STATE/manager.print-count") + 1 )); printf "%s\n" "$count" >"$FAKE_SERVICE_STATE/manager.print-count"; [[ "$count" -ne 4 ]] || exit 70; fi' \
  'case "${1:-}" in' \
  '  print)' \
  '    if [[ -f "$FAKE_SERVICE_STATE/proxy.registered" ]]; then' \
  '      service_pid=0; [[ -f "$FAKE_SERVICE_STATE/proxy.loaded" ]] && service_pid=4242' \
  '      service_path="$HOME/Library/LaunchAgents/com.user.claudex-translation-proxy.plist"; [[ ! -f "$FAKE_SERVICE_STATE/proxy.foreign" ]] || service_path=/foreign/claudex-proxy.plist' \
  '      printf "service = {\n  path = %s\n  pid = %s\n}\n" "$service_path" "$service_pid"' \
  '    else exit 113; fi ;;' \
  '  bootstrap)' \
  '    if [[ "$*" == *claudex-translation-proxy* ]]; then' \
  '      if [[ "${FAKE_PROXY_BOOTSTRAP_FAIL:-0}" == 1 ]] && [[ ! -f "$FAKE_SERVICE_STATE/bootstrap.failed" ]]; then' \
  '        touch "$FAKE_SERVICE_STATE/bootstrap.failed"' \
  '        printf "cutover-fail\n" >>"$FAKE_EVENT_LOG"' \
  '        exit 19' \
  '      fi' \
  '      service_file="${!#}"' \
  '      next_line=false' \
  '      while IFS= read -r line; do' \
  '        if [[ "$next_line" == true ]]; then line="${line#*<string>}"; line="${line%%</string>*}"; printf "%s\n" "$line" >"$FAKE_SERVICE_STATE/proxy.port"; break; fi' \
  '        [[ "$line" == *"<string>--port</string>"* ]] && next_line=true' \
  '      done <"$service_file"' \
  '      touch "$FAKE_SERVICE_STATE/proxy.registered"' \
  '      touch "$FAKE_SERVICE_STATE/proxy.loaded"' \
  '      printf "cutover-start\n" >>"$FAKE_EVENT_LOG"' \
  '      if [[ "${FAKE_PROXY_POST_START_FAIL:-0}" == 1 ]] && [[ ! -f "$FAKE_SERVICE_STATE/post-start.failed" ]]; then touch "$FAKE_SERVICE_STATE/post-start.failed" "$FAKE_SERVICE_STATE/proxy.unready"; printf "cutover-unready\n" >>"$FAKE_EVENT_LOG"; fi' \
  '    fi ;;' \
  '  bootout)' \
  '    if [[ "$*" == *claudex-translation-proxy* ]]; then rm -f "$FAKE_SERVICE_STATE/proxy.loaded" "$FAKE_SERVICE_STATE/proxy.registered" "$FAKE_SERVICE_STATE/proxy.unready"; printf "cutover-stop\n" >>"$FAKE_EVENT_LOG"; fi ;;' \
  'esac' >"$fake_bin/launchctl"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "%s\n" "$*" >>"$FAKE_SERVICE_LOG"' \
  'proxy_unit="$HOME/.config/systemd/user/claudex-translation-proxy.service"' \
  'if [[ "$*" == *show-environment* ]]; then exit 0; fi' \
  'if [[ "$*" == *"--property LoadState"* ]] && [[ -f "$FAKE_SERVICE_STATE/manager.state-fail" ]]; then exit 70; fi' \
  'if [[ "$*" == *"--property MainPID"* ]] && [[ -f "$FAKE_SERVICE_STATE/manager.pid-fail" ]]; then exit 70; fi' \
  'if [[ "$*" == *"--property LoadState"* ]]; then if [[ -f "$FAKE_SERVICE_STATE/proxy.registered" ]]; then printf "loaded\n"; else printf "not-found\n"; fi; exit 0; fi' \
  'if [[ "$*" == *"--property FragmentPath"* ]]; then if [[ -f "$FAKE_SERVICE_STATE/proxy.registered" ]]; then if [[ -f "$FAKE_SERVICE_STATE/proxy.foreign" ]]; then printf "/foreign/claudex-proxy.service\n"; else printf "%s\n" "$proxy_unit"; fi; fi; exit 0; fi' \
  'if [[ "$*" == *"--property MainPID"* ]]; then if [[ -f "$FAKE_SERVICE_STATE/proxy.loaded" ]]; then printf "4242\n"; else printf "0\n"; fi; exit 0; fi' \
  'action=' \
  'for argument in "$@"; do case "$argument" in daemon-reload|enable|disable|start|stop|restart) action="$argument"; break ;; esac; done' \
  'case "$action" in' \
  '  daemon-reload) if [[ -f "$proxy_unit" ]]; then touch "$FAKE_SERVICE_STATE/proxy.registered"; else rm -f "$FAKE_SERVICE_STATE/proxy.registered"; fi ;;' \
  '  start|restart)' \
  '    if [[ "$*" == *claudex-translation-proxy.service* ]]; then' \
  '      if [[ "${FAKE_PROXY_BOOTSTRAP_FAIL:-0}" == 1 ]] && [[ ! -f "$FAKE_SERVICE_STATE/bootstrap.failed" ]]; then touch "$FAKE_SERVICE_STATE/bootstrap.failed"; printf "cutover-fail\n" >>"$FAKE_EVENT_LOG"; exit 19; fi' \
  '      sed -n '\''s/.*--port \([0-9][0-9]*\).*/\1/p'\'' "$proxy_unit" >"$FAKE_SERVICE_STATE/proxy.port"' \
  '      touch "$FAKE_SERVICE_STATE/proxy.registered" "$FAKE_SERVICE_STATE/proxy.loaded"' \
  '      printf "cutover-start\n" >>"$FAKE_EVENT_LOG"' \
  '      if [[ "${FAKE_PROXY_POST_START_FAIL:-0}" == 1 ]] && [[ ! -f "$FAKE_SERVICE_STATE/post-start.failed" ]]; then touch "$FAKE_SERVICE_STATE/post-start.failed" "$FAKE_SERVICE_STATE/proxy.unready"; printf "cutover-unready\n" >>"$FAKE_EVENT_LOG"; fi' \
  '    fi ;;' \
  '  stop)' \
  '    if [[ "$*" == *claudex-translation-proxy.service* ]]; then rm -f "$FAKE_SERVICE_STATE/proxy.loaded" "$FAKE_SERVICE_STATE/proxy.unready"; printf "cutover-stop\n" >>"$FAKE_EVENT_LOG"; fi ;;' \
  'esac' >"$fake_bin/systemctl"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"$fake_bin/plutil"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  '[[ -f "$FAKE_SERVICE_STATE/proxy.loaded" ]] || exit 1' \
  'printf "4242\n"' >"$fake_bin/lsof"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  '[[ -f "$FAKE_SERVICE_STATE/proxy.loaded" ]] || exit 1' \
  'port="$(cat "$FAKE_SERVICE_STATE/proxy.port")"' \
  'printf '\''LISTEN 0 128 127.0.0.1:%s 0.0.0.0:* users:(("claudex",pid=4242,fd=7))\n'\'' "$port"' \
  >"$fake_bin/ss"
chmod 0755 "$fake_bin"/*

invoke_install() {
  local home="$1" models="$2" output="$3" platform="$4"
  local build="${5:-1}" bootstrap_fail="${6:-0}"
  local post_start_fail="${7:-0}" proxy_port_override="${8:-}"
  local swap_definition="${9:-0}"
  local manager_failure="${10:-}"
  install -d "$home" "$home/service-state"
  printf '%s\n' "$models" >"$home/models.state"
  printf '%s\n' "$build" >"$home/claudex-build.state"
  HOME="$home" \
  CLAUDEX_DATA_DIR="$home/data" \
  USER_BIN_DIR="$home/user-bin" \
  PATH="$fake_bin:$tool_bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  REAL_PYTHON="$real_python" \
  FAKE_ARCHIVE_DIGEST="$archive_digest" \
  FAKE_UNAME_S="$platform" \
  FAKE_MODELS_STATE="$home/models.state" \
  FAKE_CLAUDEX_BUILD_STATE="$home/claudex-build.state" \
  FAKE_HEADROOM_PYTHON="$fake_bin/headroom-python" \
  FAKE_CA_FILE="$ca_file" \
  FAKE_SERVICE_STATE="$home/service-state" \
  FAKE_SERVICE_LOG="$home/service.log" \
  FAKE_EVENT_LOG="$home/events.log" \
  FAKE_PROXY_BOOTSTRAP_FAIL="$bootstrap_fail" \
  FAKE_PROXY_POST_START_FAIL="$post_start_fail" \
  FAKE_SWAP_DEFINITION_AFTER_PREFLIGHT="$swap_definition" \
  FAKE_MANAGER_FAILURE_AFTER_PREFLIGHT="$manager_failure" \
  CLAUDEX_PROXY_PORT="$proxy_port_override" \
  "$ROOT/install.sh" >"$output" 2>&1
}

run_install() {
  local home="$1" models="$2" output="$3" platform="$4"
  local build="${5:-1}"
  invoke_install "$home" "$models" "$output" "$platform" "$build" || {
    sed -n '1,220p' "$output" >&2
    return 1
  }
}

proxy_mutation_count() {
  awk '$0 == "cutover-start" || $0 == "cutover-stop" {count++} END {print count + 0}' \
    "$1"
}

assert_one_restart() {
  local before="$1" event_log="$2"
  local after
  after="$(proxy_mutation_count "$event_log")"
  [[ $((after - before)) -eq 2 ]]
}

exercise_platform() {
  local platform="$1" platform_name="$2"
  local daily_home="$fixture/$platform_name-daily"
  local unknown_home="$fixture/$platform_name-unknown"
  local proxy_service before

  install -d "$unknown_home/service-state"
  touch "$unknown_home/service-state/proxy.registered"
  if invoke_install "$unknown_home" full "$fixture/$platform_name-unknown.log" \
      "$platform" 1; then
    printf 'loaded unknown %s target was accepted\n' "$platform" >&2
    return 1
  fi
  rg -Fq 'refusing to replace loaded unknown Claudex proxy target' \
    "$fixture/$platform_name-unknown.log"
  [[ ! -e "$unknown_home/data/service-ports.json" ]]
  [[ ! -s "$unknown_home/events.log" ]]

  run_install "$daily_home" empty "$fixture/$platform_name-pending.log" \
    "$platform" 1
  rg -Fq pending-provider-login "$fixture/$platform_name-pending.log"
  rg -Fq "Next: claudex-login codex; claudex-login claude; $ROOT/install.sh" \
    "$fixture/$platform_name-pending.log"
  [[ ! -e "$daily_home/service-state/proxy.loaded" ]]
  if [[ "$platform" == Darwin ]]; then
    proxy_service="$daily_home/Library/LaunchAgents/com.user.claudex-translation-proxy.plist"
  else
    proxy_service="$daily_home/.config/systemd/user/claudex-translation-proxy.service"
  fi
  [[ ! -e "$proxy_service" ]]

  : >"$daily_home/events.log"
  run_install "$daily_home" full "$fixture/$platform_name-activated.log" \
    "$platform" 1
  [[ -f "$proxy_service" ]]
  [[ -f "$daily_home/service-state/proxy.loaded" ]]
  rg -Fq 'Claudex:     installed' "$fixture/$platform_name-activated.log"
  python3 - "$daily_home/events.log" <<'PY'
import sys

events = open(sys.argv[1], encoding="utf-8").read().splitlines()
publication = events.index("publish-validate")
preflight = next(i for i, event in enumerate(events) if event.startswith("preflight-start "))
cutover = events.index("cutover-start")
if not publication < preflight < cutover:
    raise SystemExit("model publication, preflight, and cutover were out of order")
PY

  before="$(proxy_mutation_count "$daily_home/events.log")"
  run_install "$daily_home" full "$fixture/$platform_name-reused.log" \
    "$platform" 1
  [[ "$(proxy_mutation_count "$daily_home/events.log")" == "$before" ]]
  rg -Fq 'Claudex:     reused' "$fixture/$platform_name-reused.log"

  before="$(proxy_mutation_count "$daily_home/events.log")"
  run_install "$daily_home" full "$fixture/$platform_name-binary.log" \
    "$platform" 2
  assert_one_restart "$before" "$daily_home/events.log"

  printf '\n' >>"$proxy_service"
  for manager_failure in state pid; do
    before="$(proxy_mutation_count "$daily_home/events.log")"
    if invoke_install "$daily_home" full \
        "$fixture/$platform_name-$manager_failure-query.log" \
        "$platform" 2 0 0 '' 0 "$manager_failure"; then
      printf 'post-preflight %s %s query failure was accepted\n' \
        "$platform" "$manager_failure" >&2
      return 1
    fi
    [[ "$(proxy_mutation_count "$daily_home/events.log")" == "$before" ]]
    [[ -f "$daily_home/service-state/proxy.loaded" ]]
    rg -Fq 'refusing to stop ownership-drifted Claudex proxy runtime' \
      "$fixture/$platform_name-$manager_failure-query.log"
    rm -f "$daily_home/service-state/manager.state-fail" \
      "$daily_home/service-state/manager.pid-fail" \
      "$daily_home/service-state/manager.print-count"
  done

  before="$(proxy_mutation_count "$daily_home/events.log")"
  run_install "$daily_home" full "$fixture/$platform_name-service.log" \
    "$platform" 2
  assert_one_restart "$before" "$daily_home/events.log"

  "$real_python" - "$daily_home/data/model-config/current/claudex.toml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(
    text.replace(
        'default_model = "gpt-5.6-sol"',
        'default_model = "gpt-5.6-terra"',
        1,
    ),
    encoding="utf-8",
)
PY
  before="$(proxy_mutation_count "$daily_home/events.log")"
  run_install "$daily_home" full "$fixture/$platform_name-model.log" \
    "$platform" 2
  assert_one_restart "$before" "$daily_home/events.log"

  touch "$daily_home/service-state/proxy.unready"
  before="$(proxy_mutation_count "$daily_home/events.log")"
  run_install "$daily_home" full "$fixture/$platform_name-readiness.log" \
    "$platform" 2
  assert_one_restart "$before" "$daily_home/events.log"

  prior_service_content="$(cat "$proxy_service")"
  prior_ports_content="$(cat "$daily_home/data/service-ports.json")"
  prior_generation="$(readlink "$daily_home/data/model-config/current")"
  prior_model_content="$(cat "$daily_home/data/model-config/current/claudex.toml")"
  prior_binary_digest="$(shasum -a 256 "$daily_home/data/bin/claudex" | awk '{print $1}')"
  prior_proxy_port="$(jq -r .claudexProxyPort "$daily_home/data/service-ports.json")"
  rollback_proxy_port="$(python3 - "$prior_proxy_port" \
    "$(jq -r .cliproxyPort "$daily_home/data/service-ports.json")" \
    "$(jq -r .headroomPort "$daily_home/data/service-ports.json")" <<'PY'
import socket
import sys

reserved = {int(value) for value in sys.argv[1:]}
while True:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    if port not in reserved:
        print(port)
        break
PY
)"
  rollback_event_start="$(wc -l <"$daily_home/events.log" | tr -d ' ')"
  if invoke_install "$daily_home" full "$fixture/$platform_name-rollback.log" \
      "$platform" 2 0 1 "$rollback_proxy_port"; then
    printf 'injected %s proxy readiness failure reported success\n' \
      "$platform" >&2
    return 1
  fi
  [[ "$(cat "$proxy_service")" == "$prior_service_content" ]]
  [[ "$(cat "$daily_home/data/service-ports.json")" == "$prior_ports_content" ]]
  [[ "$(readlink "$daily_home/data/model-config/current")" == "$prior_generation" ]]
  [[ "$(cat "$daily_home/data/model-config/current/claudex.toml")" == "$prior_model_content" ]]
  [[ "$(shasum -a 256 "$daily_home/data/bin/claudex" | awk '{print $1}')" == \
     "$prior_binary_digest" ]]
  [[ -f "$daily_home/service-state/proxy.loaded" ]]
  [[ ! -f "$daily_home/service-state/proxy.unready" ]]
  python3 - "$daily_home/events.log" "$rollback_event_start" <<'PY'
import sys

events = open(sys.argv[1], encoding="utf-8").read().splitlines()[int(sys.argv[2]):]
start = events.index("cutover-start")
unready = events.index("cutover-unready")
stop = events.index("cutover-stop", unready + 1)
recovery = events.index("cutover-start", start + 1)
if not start < unready < stop < recovery:
    raise SystemExit("post-start failure did not stop before prior-service recovery")
PY

  printf '\n' >>"$proxy_service"
  before="$(proxy_mutation_count "$daily_home/events.log")"
  if invoke_install "$daily_home" full "$fixture/$platform_name-race.log" \
      "$platform" 2 0 0 '' 1; then
    printf 'post-preflight %s definition swap was accepted\n' "$platform" >&2
    return 1
  fi
  [[ "$(proxy_mutation_count "$daily_home/events.log")" == "$before" ]]
  [[ -f "$daily_home/service-state/proxy.loaded" ]]
  rg -Fq 'refusing to stop ownership-drifted Claudex proxy runtime' \
    "$fixture/$platform_name-race.log"
}

exercise_platform Darwin darwin
exercise_platform Linux linux

foreign_home="$fixture/foreign"
install -d "$foreign_home"
listener_port_file="$foreign_home/listener.port"
python3 - "$listener_port_file" <<'PY' &
import socket
import sys
import time
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
open(sys.argv[1], "w", encoding="utf-8").write(str(listener.getsockname()[1]))
time.sleep(300)
PY
foreign_listener_pid=$!
background_pids+=("$foreign_listener_pid")
for _ in {1..50}; do [[ -s "$listener_port_file" ]] && break; sleep 0.05; done
foreign_port="$(cat "$listener_port_file")"
install -d "$foreign_home/data"
printf '{"claudexProxyPort":%s,"cliproxyPort":8317,"headroomPort":8787}\n' \
  "$foreign_port" >"$foreign_home/data/service-ports.json"
run_install "$foreign_home" full "$fixture/foreign.log" Darwin 1
kill -0 "$foreign_listener_pid"
selected_proxy_port="$(jq -r .claudexProxyPort "$foreign_home/data/service-ports.json")"
[[ "$selected_proxy_port" != "$foreign_port" ]]

printf 'PASS: installer proxy transaction\n'
