#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true
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

private_data="$fixture/private-data"
private_tools="$private_data/headroom/tools"
private_bin="$private_data/headroom/bin"
private_snapshot="$fixture/private-snapshot"
host_bin="$fixture/host-bin"
install -d -m 0700 \
  "$private_tools/mempalace" "$private_tools/graphifyy" \
  "$private_tools/headroom-ai" "$private_bin" "$host_bin"
printf 'mempalace-old\n' >"$private_tools/mempalace/version"
printf 'graphify-old\n' >"$private_tools/graphifyy/version"
printf 'headroom-old\n' >"$private_tools/headroom-ai/version"
printf 'mempalace-bin-old\n' >"$private_bin/mempalace-mcp"
printf 'graphify-bin-old\n' >"$private_bin/graphify-mcp"
printf 'headroom-bin-old\n' >"$private_bin/headroom"
printf 'host-tool-unchanged\n' >"$host_bin/graphify-mcp"

snapshot_private_tool_state \
  "$private_data" "$private_tools" "$private_bin" "$private_snapshot"
inject_failure_after_all_three_upgrades() {
  printf 'mempalace-new\n' >"$private_tools/mempalace/version"
  printf 'graphify-new\n' >"$private_tools/graphifyy/version"
  printf 'headroom-new\n' >"$private_tools/headroom-ai/version"
  printf 'mempalace-bin-new\n' >"$private_bin/mempalace-mcp"
  printf 'graphify-bin-new\n' >"$private_bin/graphify-mcp"
  printf 'headroom-bin-new\n' >"$private_bin/headroom"
  printf 'new-entrypoint\n' >"$private_bin/graphify-future"
  return 72
}
if inject_failure_after_all_three_upgrades; then
  printf 'three-upgrade failure injection unexpectedly succeeded\n' >&2
  exit 1
fi
restore_private_tool_state \
  "$private_data" "$private_tools" "$private_bin" "$private_snapshot"
private_tool_state_matches \
  "$private_data" "$private_tools" "$private_bin" "$private_snapshot"
[[ "$(<"$private_tools/mempalace/version")" == mempalace-old ]]
[[ "$(<"$private_tools/graphifyy/version")" == graphify-old ]]
[[ "$(<"$private_bin/mempalace-mcp")" == mempalace-bin-old ]]
[[ "$(<"$private_bin/graphify-mcp")" == graphify-bin-old ]]
[[ ! -e "$private_bin/graphify-future" ]]
[[ "$(<"$host_bin/graphify-mcp")" == host-tool-unchanged ]]

unsafe_snapshot_data="$fixture/unsafe-snapshot-data"
unsafe_snapshot_external="$fixture/unsafe-snapshot-external"
unsafe_snapshot_before="$fixture/unsafe-snapshot-before"
install -d -m 0700 \
  "$unsafe_snapshot_data" \
  "$unsafe_snapshot_external/headroom/tools/mempalace" \
  "$unsafe_snapshot_external/headroom/tools/graphifyy" \
  "$unsafe_snapshot_external/headroom/bin"
printf 'external-mempalace\n' \
  >"$unsafe_snapshot_external/headroom/tools/mempalace/version"
printf 'external-graphify\n' \
  >"$unsafe_snapshot_external/headroom/tools/graphifyy/version"
printf 'external-bin\n' \
  >"$unsafe_snapshot_external/headroom/bin/graphify-mcp"
cp -pPR "$unsafe_snapshot_external/headroom" "$unsafe_snapshot_before"
ln -s "$unsafe_snapshot_external/headroom" \
  "$unsafe_snapshot_data/headroom"
set +e
snapshot_private_tool_state \
  "$unsafe_snapshot_data" \
  "$unsafe_snapshot_data/headroom/tools" \
  "$unsafe_snapshot_data/headroom/bin" \
  "$fixture/unsafe-snapshot" \
  2>"$fixture/unsafe-snapshot.stderr"
unsafe_snapshot_rc=$?
set -e
unsafe_layout_rejected=true
if [[ "$unsafe_snapshot_rc" -eq 0 ]]; then
  printf 'symlinked private tool snapshot layout was accepted\n' >&2
  unsafe_layout_rejected=false
fi
rg -Fq 'refusing unsafe private tool snapshot layout' \
  "$fixture/unsafe-snapshot.stderr"
if ! diff -qr -- \
    "$unsafe_snapshot_before" "$unsafe_snapshot_external/headroom" \
    >/dev/null; then
  printf 'private tool snapshot changed an external target\n' >&2
  unsafe_layout_rejected=false
fi

unsafe_restore_data="$fixture/unsafe-restore-data"
unsafe_restore_tools="$unsafe_restore_data/headroom/tools"
unsafe_restore_bin="$unsafe_restore_data/headroom/bin"
unsafe_restore_snapshot="$fixture/unsafe-restore-snapshot"
unsafe_restore_external="$fixture/unsafe-restore-external"
unsafe_restore_before="$fixture/unsafe-restore-before"
install -d -m 0700 \
  "$unsafe_restore_tools/mempalace" \
  "$unsafe_restore_tools/graphifyy" \
  "$unsafe_restore_bin"
printf 'owned-mempalace\n' >"$unsafe_restore_tools/mempalace/version"
printf 'owned-graphify\n' >"$unsafe_restore_tools/graphifyy/version"
printf 'owned-bin\n' >"$unsafe_restore_bin/graphify-mcp"
snapshot_private_tool_state \
  "$unsafe_restore_data" "$unsafe_restore_tools" "$unsafe_restore_bin" \
  "$unsafe_restore_snapshot"
mv "$unsafe_restore_data/headroom" \
  "$unsafe_restore_data/headroom-owned"
install -d -m 0700 \
  "$unsafe_restore_external/headroom/tools/mempalace" \
  "$unsafe_restore_external/headroom/tools/graphifyy" \
  "$unsafe_restore_external/headroom/bin"
printf 'external-mempalace\n' \
  >"$unsafe_restore_external/headroom/tools/mempalace/version"
printf 'external-graphify\n' \
  >"$unsafe_restore_external/headroom/tools/graphifyy/version"
printf 'external-bin\n' \
  >"$unsafe_restore_external/headroom/bin/graphify-mcp"
cp -pPR "$unsafe_restore_external/headroom" "$unsafe_restore_before"
ln -s "$unsafe_restore_external/headroom" \
  "$unsafe_restore_data/headroom"
set +e
restore_private_tool_state \
  "$unsafe_restore_data" \
  "$unsafe_restore_data/headroom/tools" \
  "$unsafe_restore_data/headroom/bin" \
  "$unsafe_restore_snapshot" \
  2>"$fixture/unsafe-restore.stderr"
unsafe_restore_rc=$?
set -e
if [[ "$unsafe_restore_rc" -eq 0 ]]; then
  printf 'symlinked private tool restore layout was accepted\n' >&2
  unsafe_layout_rejected=false
fi
rg -Fq 'refusing unsafe private tool restore layout' \
  "$fixture/unsafe-restore.stderr"
if ! diff -qr -- \
    "$unsafe_restore_before" "$unsafe_restore_external/headroom" \
    >/dev/null; then
  printf 'private tool restore changed an external target\n' >&2
  unsafe_layout_rejected=false
fi
[[ "$unsafe_layout_rejected" == true ]]

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
rg -Fq 'snapshot_private_tool_state' "$ROOT/install.sh"
rg -Fq 'restore_private_tool_state' "$ROOT/install.sh"
rg -Fq 'remove_orichum_python_generation' "$ROOT/install.sh"
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
restore_python = rollback.index("rollback_python_activation")
restore_private_tools = rollback.index("restore_private_tool_state")
restore_cliproxy = rollback.index(
    'restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
restore_endpoint = rollback.index("restore_model_config_generation")
restore_route = rollback.index("restore_claudex_proxy_service")
restore_headroom = rollback.index("restore_headroom_service")
if not (
    stop_route
    < restore_python
    < restore_private_tools
    < restore_cliproxy
    < restore_endpoint
    < restore_route
    < restore_headroom
):
    raise SystemExit("combined service rollback dependency order is unsafe")

if 'if [[ "$claudex_proxy_action" != pending-provider-login ]]; then' not in source:
    raise SystemExit("final Headroom readiness is not tied to usable route state")

snapshot_private_tools = source.index("snapshot_private_tool_state")
python_transaction = source.index("python_transaction_active=true")
provision_python = source.index("install_or_reuse_orichum_python")
upgrade_mempalace = source.index("uv tool install --upgrade mempalace")
upgrade_graphify = source.index("uv tool install --upgrade 'graphifyy[mcp,terraform]'")
upgrade_headroom = source.index("upgrade_headroom_distribution")
if not (
    python_transaction
    < provision_python
    < snapshot_private_tools
    and
    snapshot_private_tools
    < upgrade_mempalace
    < upgrade_graphify
    < upgrade_headroom
):
    raise SystemExit("private tool snapshot does not precede all three upgrades")
PY

for acceptance_workflow in \
    "$ROOT/.github/workflows/amd64-acceptance.yml" \
    "$ROOT/.github/workflows/macos-arm64-acceptance.yml"; do
  rg -Fq 'headroom-provider-pending.json' "$acceptance_workflow"
  rg -Fq -- "--write-out '%{http_code}'" "$acceptance_workflow"
  rg -Fq 'test "$headroom_status" = 503' "$acceptance_workflow"
  rg -Fq '.status == "unhealthy"' "$acceptance_workflow"
  rg -Fq '.ready == false' "$acceptance_workflow"
done

rg -Fq 'report_test_failure()' "$ROOT/tests/test_installer.sh"
rg -Fq 'trap report_test_failure ERR' "$ROOT/tests/test_installer.sh"
rg -Fq 'ERROR: test_installer.sh:%s exited %s: %s' \
  "$ROOT/tests/test_installer.sh"

printf 'PASS: Orichum installer rollback and port selection\n'
