#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-proxy-test.XXXXXX")"
fixture="$(cd -P "$fixture" && pwd)"
proxy_pid=""
client_a_pid=""
client_b_pid=""
client_a_runtime_pid=""
client_b_runtime_pid=""
cleanup() {
  local test_status=$?
  local process_pid
  for process_pid in \
    "$client_a_runtime_pid" "$client_b_runtime_pid" \
    "$client_a_pid" "$client_b_pid" "$proxy_pid"; do
    if [[ "$process_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$process_pid" 2>/dev/null; then
      kill "$process_pid" 2>/dev/null || :
      wait "$process_pid" 2>/dev/null || :
    fi
  done
  if [[ "$test_status" -ne 0 ]]; then
    for client_log in "$fixture"/client-*.log; do
      [[ -f "$client_log" ]] || continue
      printf '%s\n' "--- $(basename "$client_log")" >&2
      tail -40 "$client_log" >&2 || :
    done
  fi
  rm -rf -- "$fixture"
}
trap cleanup EXIT

data_root="$fixture/data"
xdg_root="$fixture/xdg"
tools_root="$fixture/tools"
client_state="$fixture/clients"
service_file="$xdg_root/systemd/user/claudex-translation-proxy.service"
proxy_pid_file="$fixture/proxy.pid"
proxy_port_file="$fixture/proxy.port"
test_home="${HOME:?}"
install -d -m 0700 \
  "$data_root" "$data_root/state" "$data_root/state/sessions" \
  "$client_state" "$tools_root"
install -d -m 0755 "$data_root/bin" "$data_root/claude-config" \
  "$(dirname "$service_file")"

python3 - "$proxy_port_file" <<'PY'
import socket
import sys

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
listener.close()
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(f"{port}\n")
PY
proxy_port="$(cat "$proxy_port_file")"
valid_service_port "$proxy_port"
write_service_ports "$data_root" 18317 18787 "$proxy_port"
render_claudex_config "$data_root/claudex.toml" \
  gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol \
  claude-haiku-4-5-20251001 claude-sonnet-5 claude-opus-4-8 \
  /portable/bin/claude 18317 18787 "$proxy_port"
migrate_legacy_model_config "$data_root"

cat >"$data_root/bin/claudex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" != *"run gpt"* ]]; then
  exit 2
fi
curl -fsS --connect-timeout 1 --max-time 2 \
  "http://127.0.0.1:$TEST_PROXY_PORT/v1/models" >/dev/null
printf '%s\n' "$$" >"$TEST_CLIENT_STATE/$TEST_CLIENT_NAME.pid"
trap 'exit 0' INT TERM
while :; do
  /bin/sleep 1
done
EOF
chmod 0755 "$data_root/bin/claudex"
HOME="$test_home" render_claudex_proxy_systemd_user_unit \
  "$service_file" "$data_root" "$proxy_port"

cat >"$tools_root/uname" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  -s) printf '%s\n' Linux ;;
  -m) printf '%s\n' aarch64 ;;
  *) printf '%s\n' Linux ;;
esac
EOF
cat >"$tools_root/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *LoadState* ]]; then
  printf '%s\n' loaded
elif [[ "$*" == *FragmentPath* ]]; then
  printf '%s\n' "$TEST_SERVICE_FILE"
elif [[ "$*" == *MainPID* ]]; then
  cat "$TEST_PROXY_PID_FILE"
else
  exit 1
fi
EOF
cat >"$tools_root/ss" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'LISTEN 0 128 127.0.0.1:%s 0.0.0.0:* users:(("claudex",pid=%s,fd=7))\n' \
  "$TEST_PROXY_PORT" "$(cat "$TEST_PROXY_PID_FILE")"
EOF
chmod 0755 "$tools_root"/*

cat >"$fixture/proxy.py" <<'PY'
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({
            "object": "list",
            "data": [{"id": "gpt-5.6-sol"}],
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
python3 "$fixture/proxy.py" "$proxy_port" \
  >"$fixture/proxy.log" 2>&1 &
proxy_pid=$!
printf '%s\n' "$proxy_pid" >"$proxy_pid_file"
for _ in {1..50}; do
  if curl -fsS --connect-timeout 1 --max-time 1 \
      "http://127.0.0.1:$proxy_port/v1/models" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.05
done
curl -fsS --connect-timeout 1 --max-time 1 \
  "http://127.0.0.1:$proxy_port/v1/models" >/dev/null

start_client() {
  local client_name="$1"
  (
    cd "$fixture"
    HOME="$test_home" \
    CLAUDEX_DATA_DIR="$data_root" \
    XDG_CONFIG_HOME="$xdg_root" \
    TEST_SERVICE_FILE="$service_file" \
    TEST_PROXY_PID_FILE="$proxy_pid_file" \
    TEST_PROXY_PORT="$proxy_port" \
    TEST_CLIENT_STATE="$client_state" \
    TEST_CLIENT_NAME="$client_name" \
    PATH="$tools_root:$PATH" \
      "$ROOT/bin/claudex-gpt" "session-$client_name" \
      >"$fixture/client-$client_name.log" 2>&1
  ) &
}

start_client a
client_a_pid=$!
start_client b
client_b_pid=$!
for _ in {1..200}; do
  [[ -s "$client_state/a.pid" && -s "$client_state/b.pid" ]] && break
  /bin/sleep 0.05
done
[[ -s "$client_state/a.pid" && -s "$client_state/b.pid" ]]
client_a_runtime_pid="$(cat "$client_state/a.pid")"
client_b_runtime_pid="$(cat "$client_state/b.pid")"
[[ "$client_a_runtime_pid" =~ ^[1-9][0-9]*$ ]]
[[ "$client_b_runtime_pid" =~ ^[1-9][0-9]*$ ]]
kill -0 "$client_a_runtime_pid"
kill -0 "$client_b_runtime_pid"
kill -0 "$proxy_pid"
[[ "$(cat "$proxy_pid_file")" == "$proxy_pid" ]]

kill "$client_a_runtime_pid"
for _ in {1..50}; do
  kill -0 "$client_a_runtime_pid" 2>/dev/null || break
  /bin/sleep 0.02
done
if kill -0 "$client_a_runtime_pid" 2>/dev/null; then
  printf 'client A runtime remained alive after termination\n' >&2
  exit 1
fi
wait "$client_a_pid" 2>/dev/null || :
client_a_runtime_pid=""
client_a_pid=""
kill -0 "$proxy_pid"
curl -fsS --connect-timeout 1 --max-time 1 \
  "http://127.0.0.1:$proxy_port/v1/models" >/dev/null
kill -0 "$client_b_pid"
kill -0 "$client_b_runtime_pid"

printf 'PASS: shared Claudex proxy outlives an individual client\n'
