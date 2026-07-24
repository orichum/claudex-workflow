#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-transaction.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

snapshot="$fixture/snapshot"
install -d -m 0700 "$snapshot" "$fixture/bin"

launcher="$fixture/bin/orichum"
prior="$fixture/prior-orichum"
printf '#!/usr/bin/env bash\nexit 0\n' >"$prior"
chmod 0755 "$prior"
ln -s "$prior" "$launcher"

snapshot_path "$launcher" "$snapshot" launcher
rm "$launcher"
printf 'partial install\n' >"$launcher"
restore_snapshot "$launcher" "$snapshot" launcher
snapshot_path_matches "$launcher" "$snapshot" launcher
[[ -L "$launcher" && "$(readlink "$launcher")" == "$prior" ]]

absent="$fixture/bin/absent"
snapshot_path "$absent" "$snapshot" absent
printf 'partial install\n' >"$absent"
restore_snapshot "$absent" "$snapshot" absent
snapshot_path_matches "$absent" "$snapshot" absent
[[ ! -e "$absent" && ! -L "$absent" ]]

python3 - "$fixture/occupied.port" <<'PY' &
import socket
import sys
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
with open(sys.argv[1], "w", encoding="ascii") as handle:
    handle.write(str(listener.getsockname()[1]))
while True:
    time.sleep(1)
PY
listener_pid=$!
trap 'kill "$listener_pid" 2>/dev/null || true; wait "$listener_pid" 2>/dev/null || true; rm -rf -- "$fixture"' EXIT
for _ in {1..100}; do
  [[ -s "$fixture/occupied.port" ]] && break
  sleep 0.01
done
occupied="$(cat "$fixture/occupied.port")"
selected="$(
  select_service_port 'Route proxy' TEST_PORT "$occupied" false false
)"
[[ "$selected" != "$occupied" ]]
valid_service_port "$selected"
port_is_available "$selected"

TEST_PORT="$occupied"
if select_service_port 'Route proxy' TEST_PORT "$occupied" false false \
    >"$fixture/override.stdout" 2>"$fixture/override.stderr"; then
  printf 'explicit occupied port was silently replaced\n' >&2
  exit 1
fi
rg -Fq 'from TEST_PORT is unavailable' "$fixture/override.stderr"

rg -Fq 'snapshot_path "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'orichum_launcher_mutated=true' "$ROOT/install.sh"
rg -Fq 'restore_snapshot "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'managed_listener_is_owned' "$ROOT/install.sh"
rg -Fq 'managed_target_matches_definition_or_absent' "$ROOT/install.sh"
settings_line="$(rg -n -F 'install -m 0600 "$WORKFLOW_ROOT/controller/settings.json"' \
  "$ROOT/install.sh" | cut -d: -f1)"
transaction_end_line="$(rg -n -F 'WORKFLOW_TRANSACTION_ACTIVE=false' \
  "$ROOT/install.sh" | tail -1 | cut -d: -f1)"
[[ "$settings_line" -gt "$transaction_end_line" ]]

python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
start = source.index("rollback_install_transaction()")
end = source.index("WORKFLOW_ROLLBACK_HANDLER=", start)
rollback = source[start:end]

stop_route = rollback.index("claudex_proxy_runtime_mutated")
restore_cliproxy = rollback.index(
    'restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
restore_endpoint = rollback.index("restore_model_config_generation")
restore_route = rollback.index("restore_claudex_proxy_service")
restore_headroom = rollback.index("restore_headroom_service")
if not (
    stop_route
    < restore_cliproxy
    < restore_endpoint
    < restore_route
    < restore_headroom
):
    raise SystemExit("combined service rollback dependency order is unsafe")

if 'if [[ "$claudex_proxy_action" != pending-provider-login ]]; then' not in source:
    raise SystemExit("final Headroom readiness is not tied to usable route state")
PY

printf 'PASS: Orichum installer rollback and port selection\n'
