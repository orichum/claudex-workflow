#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-transaction.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

python3 - "$ROOT/install.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
routing_decision_start = source.index("routing_decision=upgraded")
routing_decision_end = source.index(
    "\nfi\n\nmempalace_recorded_version", routing_decision_start
)
routing_decision = source[routing_decision_start:routing_decision_end]
early_runtime_samples = (
    "cliproxy_listener_owned",
    "cliproxy_ready_before",
    "claudex_proxy_listener_owned",
)
if any(sample in routing_decision for sample in early_runtime_samples):
    raise SystemExit(
        "routing status still treats an early runtime sample as a completed "
        "repair"
    )
route_action_start = source.index("claudex_proxy_action=pending-provider-login")
route_action_end = source.index(
    "\nif [[ \"$endpoint_lock_owned\" == true ]]", route_action_start
)
route_action = source[route_action_start:route_action_end]
if (
    '[[ "$routing_action" == reused ]]' not in route_action
    or '[[ "$claudex_proxy_action" == reconciled ]]' not in route_action
    or "routing_action=repaired" not in route_action
):
    raise SystemExit(
        "routing status does not report an actual route-proxy reconciliation"
    )
cliproxy_action_start = source.index("cliproxy_action=reused")
cliproxy_action_end = source.index(
    "\nprint_component_status_table", cliproxy_action_start
)
cliproxy_action = source[cliproxy_action_start:cliproxy_action_end]
if (
    '[[ "$routing_action" == reused ]]' not in cliproxy_action
    or '[[ "$cliproxy_action" == reconciled ]]' not in cliproxy_action
    or "routing_action=repaired" not in cliproxy_action
):
    raise SystemExit(
        "routing status does not report an actual CLIProxyAPI reconciliation"
    )

start = source.index('elif [[ -n "$prior_model_generation" ]]')
end = source.index('routing_action=reused', start)
fallback = source[start:end]
required = (
    '[[ "$cliproxy_binary_changed" == false ]]',
    '[[ "$cliproxy_config_changed" == unchanged ]]',
    '[[ "$cliproxy_service_changed" == unchanged ]]',
    '[[ "$cliproxy_listener_owned" == true ]]',
    '[[ "$cliproxy_ready_before" == true ]]',
)
missing = [condition for condition in required if condition not in fallback]
if missing:
    raise SystemExit(
        "model-discovery fallback lacks CLIProxy invariants: "
        + ", ".join(missing)
    )
PY

model_file_data="$fixture/model-file-data"
model_file_generation="$model_file_data/model-config/generation.test"
install -d -m 0700 "$model_file_generation"
printf '{}\n' >"$model_file_generation/models.json"
printf 'default_model = "test"\n' \
  >"$model_file_generation/claudex.toml"
printf '{}\n' >"$model_file_generation/effective-models.json"
ln -s generation.test "$model_file_data/model-config/current"
[[ "$(model_config_file \
  "$model_file_data" effective-models.json)" == \
  "$model_file_data/model-config/current/effective-models.json" ]]

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

install_state_dir="$fixture/install-state"
install_state_snapshot="$fixture/install-state-snapshot"
install_state_file="$install_state_dir/install-state.json"
install -d -m 0700 "$install_state_dir" "$install_state_snapshot"
printf '{"prior":true}\n' >"$install_state_file"
chmod 0600 "$install_state_file"
snapshot_path \
  "$install_state_file" "$install_state_snapshot" install-state
printf '{"partial":true}\n' >"$install_state_file"
restore_snapshot \
  "$install_state_file" "$install_state_snapshot" install-state
snapshot_path_matches \
  "$install_state_file" "$install_state_snapshot" install-state
[[ "$(<"$install_state_file")" == '{"prior":true}' ]]

private_data="$fixture/private-data"
private_tools="$private_data/tools/uv"
private_bin="$private_data/tools/bin"
private_snapshot="$fixture/private-snapshot"
host_bin="$fixture/host-bin"
install -d -m 0700 \
  "$private_tools/mempalace" "$private_tools/graphifyy" \
  "$private_bin" "$host_bin"
printf 'mempalace-old\n' >"$private_tools/mempalace/version"
printf 'graphify-old\n' >"$private_tools/graphifyy/version"
printf 'mempalace-bin-old\n' >"$private_bin/mempalace-mcp"
printf 'graphify-bin-old\n' >"$private_bin/graphify-mcp"
printf 'host-tool-unchanged\n' >"$host_bin/graphify-mcp"

snapshot_private_tool_state \
  "$private_data" "$private_tools" "$private_bin" "$private_snapshot"
inject_failure_after_both_tool_upgrades() {
  printf 'mempalace-new\n' >"$private_tools/mempalace/version"
  printf 'graphify-new\n' >"$private_tools/graphifyy/version"
  printf 'mempalace-bin-new\n' >"$private_bin/mempalace-mcp"
  printf 'graphify-bin-new\n' >"$private_bin/graphify-mcp"
  printf 'new-entrypoint\n' >"$private_bin/graphify-future"
  return 72
}
if inject_failure_after_both_tool_upgrades; then
  printf 'two-upgrade failure injection unexpectedly succeeded\n' >&2
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

legacy_data="$fixture/legacy-private-data"
legacy_tools="$legacy_data/headroom/tools"
legacy_bin="$legacy_data/headroom/bin"
migrated_tools="$legacy_data/tools/uv"
migrated_bin="$legacy_data/tools/bin"
legacy_snapshot="$fixture/legacy-private-snapshot"
install -d -m 0700 \
  "$legacy_tools/mempalace/bin" "$legacy_tools/graphifyy/bin" \
  "$legacy_bin" "$migrated_tools" "$migrated_bin"
printf 'legacy-mempalace\n' >"$legacy_tools/mempalace/version"
printf 'legacy-graphify\n' >"$legacy_tools/graphifyy/version"
printf '#!/usr/bin/env bash\nexit 0\n' \
  >"$legacy_tools/mempalace/bin/mempalace-mcp"
printf '#!/usr/bin/env bash\nexit 0\n' \
  >"$legacy_tools/graphifyy/bin/graphify-mcp"
chmod 0755 \
  "$legacy_tools/mempalace/bin/mempalace-mcp" \
  "$legacy_tools/graphifyy/bin/graphify-mcp"
ln -s "$legacy_tools/mempalace/bin/mempalace-mcp" \
  "$legacy_bin/mempalace-mcp"
ln -s "$legacy_tools/graphifyy/bin/graphify-mcp" \
  "$legacy_bin/graphify-mcp"
ln -s "$legacy_tools/graphifyy/bin/graphify-mcp" \
  "$legacy_bin/headroom"

migrate_legacy_private_tools \
  "$legacy_data" "$migrated_tools" "$migrated_bin"
snapshot_private_tool_state \
  "$legacy_data" "$migrated_tools" "$migrated_bin" "$legacy_snapshot"
inject_failure_after_legacy_cleanup() {
  rm -rf -- "$legacy_data/headroom"
  printf 'failed-upgrade\n' >"$migrated_tools/mempalace/version"
  printf 'failed-upgrade\n' >"$migrated_tools/graphifyy/version"
  return 73
}
if inject_failure_after_legacy_cleanup; then
  printf 'legacy cleanup failure injection unexpectedly succeeded\n' >&2
  exit 1
fi
restore_private_tool_state \
  "$legacy_data" "$migrated_tools" "$migrated_bin" "$legacy_snapshot"
private_tool_state_matches \
  "$legacy_data" "$migrated_tools" "$migrated_bin" "$legacy_snapshot"
[[ "$(<"$migrated_tools/mempalace/version")" == legacy-mempalace ]]
[[ "$(<"$migrated_tools/graphifyy/version")" == legacy-graphify ]]
[[ -x "$migrated_bin/mempalace-mcp" ]]
[[ -x "$migrated_bin/graphify-mcp" ]]
[[ "$(readlink "$migrated_bin/mempalace-mcp")" == \
   "$migrated_tools/mempalace/bin/mempalace-mcp" ]]
[[ "$(readlink "$migrated_bin/graphify-mcp")" == \
   "$migrated_tools/graphifyy/bin/graphify-mcp" ]]
[[ ! -e "$migrated_bin/headroom" && ! -L "$migrated_bin/headroom" ]]
[[ ! -e "$legacy_data/headroom" && ! -L "$legacy_data/headroom" ]]

partial_data="$fixture/partial-legacy-data"
partial_legacy_tools="$partial_data/headroom/tools"
partial_legacy_bin="$partial_data/headroom/bin"
partial_tools="$partial_data/tools/uv"
partial_bin="$partial_data/tools/bin"
install -d -m 0700 \
  "$partial_legacy_tools/mempalace" \
  "$partial_legacy_tools/graphifyy" \
  "$partial_legacy_bin" \
  "$partial_tools/mempalace" \
  "$partial_tools/graphifyy" \
  "$partial_bin"
printf 'legacy-mempalace\n' \
  >"$partial_legacy_tools/mempalace/version"
printf 'legacy-graphify\n' \
  >"$partial_legacy_tools/graphifyy/version"
printf 'partial-mempalace\n' >"$partial_tools/mempalace/version"
printf 'partial-graphify\n' >"$partial_tools/graphifyy/version"
if migrate_legacy_private_tools \
    "$partial_data" "$partial_tools" "$partial_bin" \
    2>"$fixture/partial-legacy.stderr"; then
  printf 'partial private tool migration destinations were accepted\n' >&2
  exit 1
fi
[[ "$(<"$partial_legacy_tools/mempalace/version")" == \
   legacy-mempalace ]]
[[ "$(<"$partial_legacy_tools/graphifyy/version")" == legacy-graphify ]]
[[ "$(<"$partial_tools/mempalace/version")" == partial-mempalace ]]
[[ "$(<"$partial_tools/graphifyy/version")" == partial-graphify ]]

stale_data="$fixture/stale-legacy-data"
stale_legacy_tools="$stale_data/headroom/tools"
stale_legacy_bin="$stale_data/headroom/bin"
stale_tools="$stale_data/tools/uv"
stale_bin="$stale_data/tools/bin"
install -d -m 0700 \
  "$stale_legacy_tools/mempalace/bin" \
  "$stale_legacy_tools/graphifyy/bin" \
  "$stale_legacy_bin" "$stale_tools" "$stale_bin"
printf '#!/usr/bin/env bash\nexit 0\n' \
  >"$stale_legacy_tools/mempalace/bin/mempalace-mcp"
printf '#!/usr/bin/env bash\nexit 0\n' \
  >"$stale_legacy_tools/graphifyy/bin/graphify-mcp"
chmod 0755 \
  "$stale_legacy_tools/mempalace/bin/mempalace-mcp" \
  "$stale_legacy_tools/graphifyy/bin/graphify-mcp"
cp -pPR "$stale_legacy_tools/mempalace" "$stale_tools/mempalace"
cp -pPR "$stale_legacy_tools/graphifyy" "$stale_tools/graphifyy"
ln -s "$stale_legacy_tools/mempalace/bin/mempalace-mcp" \
  "$stale_legacy_bin/mempalace-mcp"
ln -s "$stale_legacy_tools/graphifyy/bin/graphify-mcp" \
  "$stale_legacy_bin/graphify-mcp"
ln -s "$stale_legacy_tools/mempalace/bin/mempalace-mcp" \
  "$stale_bin/mempalace-mcp"
ln -s "$stale_legacy_tools/graphifyy/bin/graphify-mcp" \
  "$stale_bin/graphify-mcp"
if migrate_legacy_private_tools \
    "$stale_data" "$stale_tools" "$stale_bin" \
    2>"$fixture/stale-legacy.stderr"; then
  printf 'stale private tool migration entrypoints were accepted\n' >&2
  exit 1
fi
[[ "$(readlink "$stale_bin/mempalace-mcp")" == \
   "$stale_legacy_tools/mempalace/bin/mempalace-mcp" ]]
[[ "$(readlink "$stale_bin/graphify-mcp")" == \
   "$stale_legacy_tools/graphifyy/bin/graphify-mcp" ]]

unsafe_preflight_data="$fixture/unsafe-preflight-data"
unsafe_preflight_external="$fixture/unsafe-preflight-external"
install -d -m 0700 \
  "$unsafe_preflight_data" "$unsafe_preflight_external"
printf 'external-unchanged\n' >"$unsafe_preflight_external/owner-marker"
ln -s "$unsafe_preflight_external" "$unsafe_preflight_data/tools"
declare -F preflight_private_tool_layout >/dev/null
if preflight_private_tool_layout "$unsafe_preflight_data" \
    2>"$fixture/unsafe-preflight.stderr"; then
  printf 'symlinked private tools root passed early preflight\n' >&2
  exit 1
fi
rg -Fq 'private tools root is unsafe' "$fixture/unsafe-preflight.stderr"
[[ "$(<"$unsafe_preflight_external/owner-marker")" == external-unchanged ]]
[[ ! -e "$unsafe_preflight_external/bin" ]]
[[ ! -e "$unsafe_preflight_external/uv" ]]

endpoint_normalization_failed=false
for endpoint_fixture in \
    production-three-file:legacy-three \
    production-three-file:legacy-four \
    legacy-two-file:legacy-three \
    legacy-two-file:legacy-four; do
  IFS=: read -r generation_case endpoint_case <<<"$endpoint_fixture"
  endpoint_data="$fixture/$generation_case-$endpoint_case-data"
  endpoint_snapshot="$fixture/$generation_case-$endpoint_case-snapshot"
  endpoint_generation="$endpoint_data/model-config/generation.legacy"
  endpoint_generation_snapshot="$endpoint_snapshot/prior-model-generation"
  install -d -m 0700 \
    "$endpoint_data/headroom" "$endpoint_generation" "$endpoint_snapshot"
  case "$endpoint_case" in
    legacy-three)
      printf '%s\n' \
        '{"cliproxyPort":8317,"headroomPort":8787,"routeProxyPort":13457}' \
        >"$endpoint_data/service-ports.json"
      ;;
    legacy-four)
      printf '%s\n' \
        '{"claudexProxyPort":13456,"cliproxyPort":8317,"headroomPort":8787,"routeProxyPort":13457}' \
        >"$endpoint_data/service-ports.json"
      ;;
  esac
  cat >"$endpoint_generation/models.json" <<'JSON'
{
  "object": "list",
  "data": [
    {"id": "oc-r-0000000000000001/gpt-5.6-sol", "object": "model"},
    {"id": "oc-r-0000000000000001/gpt-5.6-terra", "object": "model"},
    {"id": "oc-r-0000000000000002/claude-sonnet-5", "object": "model"},
    {"id": "oc-r-0000000000000002/claude-opus-4-8", "object": "model"}
  ]
}
JSON
  if [[ "$generation_case" == production-three-file ]]; then
    cat >"$endpoint_generation/effective-models.json" <<'JSON'
{
  "schemaVersion": 1,
  "stack": "balanced",
  "controller": "oc-r-0000000000000001/gpt-5.6-sol",
  "agents": {
    "repository-explorer": "oc-r-0000000000000001/gpt-5.6-terra",
    "repository-verifier": "oc-r-0000000000000001/gpt-5.6-terra",
    "correctness-critic": "oc-r-0000000000000002/claude-sonnet-5",
    "architecture-advisor": "oc-r-0000000000000002/claude-opus-4-8",
    "implementation-worker": "oc-r-0000000000000001/gpt-5.6-sol"
  }
}
JSON
    expected_default_model=oc-r-0000000000000001/gpt-5.6-sol
    expected_fast_alias=oc-r-0000000000000001/gpt-5.6-terra
    expected_balanced_alias=oc-r-0000000000000001/gpt-5.6-terra
    expected_powerful_alias=oc-r-0000000000000001/gpt-5.6-sol
  else
    expected_default_model=legacy-controller-model
    expected_fast_alias=legacy-fast-alias
    expected_balanced_alias=legacy-balanced-alias
    expected_powerful_alias=legacy-powerful-alias
  fi
  cat >"$endpoint_generation/claudex.toml" <<'TOML'
claude_binary = "/usr/bin/true"
proxy_port = 13456
proxy_host = "127.0.0.1"
log_level = "info"
hyperlinks = "auto"

[model_aliases]
fast = "legacy-fast-alias"
balanced = "legacy-balanced-alias"
powerful = "legacy-powerful-alias"

[[profiles]]
name = "gpt"
provider_type = "DirectAnthropic"
base_url = "http://127.0.0.1:8787"
api_key = "claudex-passthrough"
default_model = "legacy-controller-model"
enabled = true
priority = 100

[profiles.models]
haiku = "legacy-fast-model"
sonnet = "legacy-sonnet-model"
opus = "legacy-opus-model"

[profiles.custom_headers]
X-Headroom-Base-Url = "http://127.0.0.1:13457"
X-Orichum-Session-ID = "unbound"

[router]
enabled = false

[context.compression]
enabled = false

[context.sharing]
enabled = false

[context.rag]
enabled = false
TOML
  ln -s generation.legacy "$endpoint_data/model-config/current"
  snapshot_path \
    "$endpoint_data/service-ports.json" "$endpoint_snapshot" service-ports
  cp -pPR "$endpoint_generation" "$endpoint_generation_snapshot"
  if ! normalize_headroom_free_endpoint_snapshot \
      "$endpoint_snapshot/service-ports.data" \
      "$endpoint_generation_snapshot" 8317 13456 13457; then
    printf '%s %s rollback snapshot could not be normalized\n' \
      "$generation_case" "$endpoint_case" >&2
    endpoint_normalization_failed=true
    continue
  fi

  inject_failure_after_endpoint_cleanup() {
    rm -rf -- "$endpoint_data/headroom" "$endpoint_generation"
    write_service_ports "$endpoint_data" 18317 18318 18319
    return 74
  }
  if inject_failure_after_endpoint_cleanup; then
    printf '%s %s endpoint failure injection unexpectedly succeeded\n' \
      "$generation_case" "$endpoint_case" >&2
    exit 1
  fi
  restore_snapshot \
    "$endpoint_data/service-ports.json" "$endpoint_snapshot" service-ports
  restore_model_config_generation \
    "$endpoint_data" generation.legacy "$endpoint_generation_snapshot"
  jq -e '
    keys == ["claudexProxyPort", "cliproxyPort", "routeProxyPort"] and
    .claudexProxyPort == 13456 and
    .cliproxyPort == 8317 and
    .routeProxyPort == 13457
  ' "$endpoint_data/service-ports.json" >/dev/null
  restored_claudex="$endpoint_data/model-config/current/claudex.toml"
  rg -Fxq 'base_url = "http://127.0.0.1:13457"' "$restored_claudex"
  if rg -qi 'headroom|X-Headroom-Base-Url' "$restored_claudex"; then
    printf '%s %s rollback restored a Headroom endpoint\n' \
      "$generation_case" "$endpoint_case" >&2
    exit 1
  fi
  [[ "$(claudex_config_default_model "$restored_claudex")" == \
     "$expected_default_model" ]]
  rg -Fxq "fast = \"$expected_fast_alias\"" "$restored_claudex"
  rg -Fxq "balanced = \"$expected_balanced_alias\"" "$restored_claudex"
  rg -Fxq "powerful = \"$expected_powerful_alias\"" "$restored_claudex"
  [[ ! -e "$endpoint_data/headroom" && ! -L "$endpoint_data/headroom" ]]
done
[[ "$endpoint_normalization_failed" == false ]]

external_graph_root="$fixture/external-graph-root"
unsafe_graph_data="$fixture/unsafe-graph-data"
install -d -m 0700 "$external_graph_root" "$unsafe_graph_data"
printf 'external-unchanged\n' >"$external_graph_root/owner-marker"
ln -s "$external_graph_root" "$unsafe_graph_data/graphs"
if ensure_private_graph_root "$unsafe_graph_data" \
    >"$fixture/unsafe-graph.stdout" \
    2>"$fixture/unsafe-graph.stderr"; then
  printf 'symlinked central graph root was accepted\n' >&2
  exit 1
fi
rg -Fq 'central graph root is unsafe' "$fixture/unsafe-graph.stderr"
[[ "$(<"$external_graph_root/owner-marker")" == external-unchanged ]]

unsafe_snapshot_data="$fixture/unsafe-snapshot-data"
unsafe_snapshot_external="$fixture/unsafe-snapshot-external"
unsafe_snapshot_before="$fixture/unsafe-snapshot-before"
install -d -m 0700 \
  "$unsafe_snapshot_data" \
  "$unsafe_snapshot_external/uv/mempalace" \
  "$unsafe_snapshot_external/uv/graphifyy" \
  "$unsafe_snapshot_data/tools/bin"
printf 'external-mempalace\n' \
  >"$unsafe_snapshot_external/uv/mempalace/version"
printf 'external-graphify\n' \
  >"$unsafe_snapshot_external/uv/graphifyy/version"
printf 'external-bin\n' \
  >"$unsafe_snapshot_data/tools/bin/graphify-mcp"
cp -pPR "$unsafe_snapshot_external/uv" "$unsafe_snapshot_before"
ln -s "$unsafe_snapshot_external/uv" "$unsafe_snapshot_data/tools/uv"
set +e
snapshot_private_tool_state \
  "$unsafe_snapshot_data" \
  "$unsafe_snapshot_data/tools/uv" \
  "$unsafe_snapshot_data/tools/bin" \
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
    "$unsafe_snapshot_before" "$unsafe_snapshot_external/uv" \
    >/dev/null; then
  printf 'private tool snapshot changed an external target\n' >&2
  unsafe_layout_rejected=false
fi

unsafe_restore_data="$fixture/unsafe-restore-data"
unsafe_restore_tools="$unsafe_restore_data/tools/uv"
unsafe_restore_bin="$unsafe_restore_data/tools/bin"
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
mv "$unsafe_restore_data/tools/uv" "$unsafe_restore_data/tools/uv-owned"
install -d -m 0700 \
  "$unsafe_restore_external/uv/mempalace" \
  "$unsafe_restore_external/uv/graphifyy"
printf 'external-mempalace\n' \
  >"$unsafe_restore_external/uv/mempalace/version"
printf 'external-graphify\n' \
  >"$unsafe_restore_external/uv/graphifyy/version"
cp -pPR "$unsafe_restore_external/uv" "$unsafe_restore_before"
ln -s "$unsafe_restore_external/uv" "$unsafe_restore_data/tools/uv"
set +e
restore_private_tool_state \
  "$unsafe_restore_data" \
  "$unsafe_restore_data/tools/uv" \
  "$unsafe_restore_data/tools/bin" \
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
    "$unsafe_restore_before" "$unsafe_restore_external/uv" \
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

loopback_port_is_listening "$occupied"
kill "$listener_pid"
wait "$listener_pid" 2>/dev/null || true
listener_pid=
if loopback_port_is_listening "$occupied"; then
  printf 'stopped listener was confused with residual socket state\n' >&2
  exit 1
fi

rg -Fq 'snapshot_path "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'orichum_launcher_mutated=true' "$ROOT/install.sh"
rg -Fq 'restore_snapshot "$USER_BIN_DIR/orichum"' "$ROOT/install.sh"
rg -Fq 'snapshot_private_tool_state' "$ROOT/install.sh"
rg -Fq 'restore_private_tool_state' "$ROOT/install.sh"
rg -Fq 'remove_orichum_python_generation' "$ROOT/install.sh"
rg -Fq 'from integrations.common.install_control_plane import activate' \
  "$ROOT/install.sh"
rg -Fq 'from integrations.common.install_control_plane import rollback' \
  "$ROOT/install.sh"
rg -Fq 'rollback_installed_control_plane' "$ROOT/install.sh"
rg -Fq 'managed_listener_is_owned' "$ROOT/install.sh"
rg -Fq 'managed_target_matches_definition_or_absent' "$ROOT/install.sh"
settings_line="$(rg -n -F 'install -m 0600 "$WORKFLOW_ROOT/controller/settings.json"' \
  "$ROOT/install.sh" | cut -d: -f1)"
transaction_end_line="$(rg -n -F 'WORKFLOW_TRANSACTION_ACTIVE=false' \
  "$ROOT/install.sh" | tail -1 | cut -d: -f1)"
[[ "$settings_line" -lt "$transaction_end_line" ]]

python3 - "$ROOT/install.sh" <<'PY'
import sys

source = open(sys.argv[1], encoding="utf-8").read()
workflow = open(
    str(__import__("pathlib").Path(sys.argv[1]).parent / "lib/workflow.sh"),
    encoding="utf-8",
).read()
acquire_start = workflow.index("acquire_workflow_lock()")
acquire_end = workflow.index("release_workflow_lock()", acquire_start)
acquire = workflow[acquire_start:acquire_end]
if (
    'hold_workflow_lock_descriptor "$lock_dir"' not in acquire
    or 'exec 9<"$lock_dir"' not in workflow
):
    raise SystemExit("workflow lock acquisition does not retain lock FD 9")
for helper in (
    "recover_installed_control_plane()",
    "activate_installed_control_plane()",
    "rollback_installed_control_plane()",
    "finalize_installed_control_plane()",
):
    start = source.index(helper)
    end = source.index("\n}", start)
    if "install_lock_fd" not in source[start:end]:
        raise SystemExit(f"{helper} does not pass the held installer lock FD")
if source.count('"$WORKFLOW_LOCK_FD"') < 4:
    raise SystemExit("journal helper call sites omit the held installer lock FD")
start = source.index("rollback_install_transaction()")
end = source.index("WORKFLOW_ROLLBACK_HANDLER=", start)
rollback = source[start:end]

stop_route = rollback.index("claudex_proxy_runtime_mutated")
restore_installed_config = rollback.index(
    "rollback_installed_control_plane"
)
restore_python = rollback.index("rollback_python_activation")
restore_private_tools = rollback.index("restore_private_tool_state")
restore_cliproxy = rollback.index(
    'restore_snapshot "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
restore_endpoint = rollback.index("restore_model_config_generation")
restore_route = rollback.index("restore_claudex_proxy_service")
restore_install_state = rollback.index(
    'restore_snapshot "$install_state_path"'
)
if not (
    stop_route
    < restore_installed_config
    < restore_python
    < restore_private_tools
    < restore_cliproxy
    < restore_endpoint
    < restore_route
    < restore_install_state
):
    raise SystemExit("combined service rollback dependency order is unsafe")

fast_attempt = source.index("if attempt_verified_fast_install")
source_validation = source.index("source Orichum control plane is invalid")
first_runtime_snapshot = source.index(
    'snapshot_path "$WORKFLOW_DATA_ROOT/bin/cli-proxy-api"'
)
private_tool_snapshot = source.index("snapshot_private_tool_state")
if not (
    source_validation
    < fast_attempt
    < first_runtime_snapshot
    < private_tool_snapshot
):
    raise SystemExit(
        "verified fast path validation or snapshot ordering is unsafe"
    )
fast_start = source.index("attempt_verified_fast_install()")
fast_end = source.index("\n)\n\nif attempt_verified_fast_install", fast_start)
fast_body = source[fast_start:fast_end]
if (
    "trap cleanup_fast_verifiers EXIT" not in fast_body
    or "wait \"$config_verify_pid\"" not in fast_body
    or "wait \"$runtime_verify_pid\"" not in fast_body
):
    raise SystemExit("verified fast path does not reap background verifiers")

restore_start = source.index("restore_claudex_proxy_service()")
restore_end = source.index("\n}\n\nrollback_install_transaction()", restore_start)
restore_service = source[restore_start:restore_end]
platform_branch = restore_service.index('if [[ "$platform" == darwin ]]')
bootstrap = restore_service.index("launchctl bootstrap", platform_branch)
runtime_branch = restore_service.index(
    'if [[ "${claudex_proxy_runtime_mutated:-false}" == true ]]'
)
if "claudex_proxy_loaded_target_is_expected" in restore_service[
    runtime_branch:platform_branch
] or "claudex_proxy_loaded_target_is_expected" in restore_service[
    platform_branch:bootstrap
]:
    raise SystemExit(
        "darwin rollback requires a loaded target after bootout"
    )
if "claudex_proxy_service_is_owned" not in restore_service:
    raise SystemExit(
        "route-proxy rollback does not validate the restored service file"
    )

stage_config = source.index(
    "stage_installed_control_plane",
    source.index("candidate_config_root="),
)
acquire_install_lock = source.index(
    'acquire_workflow_lock "$WORKFLOW_DATA_ROOT/state/install.lock"'
)
stable_journal = source.index(
    'control_plane_journal="$WORKFLOW_DATA_ROOT/state/install-control-plane"'
)
recover_config = source.index(
    "recover_installed_control_plane", stable_journal
)
validate_candidate = source.index(
    '"$WORKFLOW_ROOT/bin/orichum" config validate',
    stage_config,
)
activate_config = source.index(
    "activate_installed_control_plane", validate_candidate
)
config_active = source.rindex(
    "config_transaction_active=true", validate_candidate, activate_config
)
transaction_end = source.index(
    "WORKFLOW_TRANSACTION_ACTIVE=false", activate_config
)
if not (
    acquire_install_lock
    < stable_journal
    < recover_config
    < stage_config
    < validate_candidate
    < config_active
    < activate_config
    < transaction_end
):
    raise SystemExit(
        "installed control plane activation is not rollback-active before "
        "its first mutation"
    )
if '"$candidate_config_root" "$INSTALLED_CONFIG_ROOT" \\\n  "$control_plane_journal"' not in source:
    raise SystemExit("activation does not use the stable control-plane journal")
finalize_config = source.index(
    "finalize_installed_control_plane", activate_config
)
doctor = source.index('"$USER_BIN_DIR/orichum" doctor', activate_config)
runtime_ready = source.index(
    '"$WORKFLOW_ROOT/bin/orichum-runtime-ready"',
    activate_config,
)
committed_routing_fingerprint = source.index(
    "committed routing input fingerprint failed",
    activate_config,
)
pending_route_service = source.index(
    'committed_route_service_file="$claudex_proxy_desired_service_file"',
    activate_config,
)
if not (
    activate_config
    < pending_route_service
    < committed_routing_fingerprint
):
    raise SystemExit(
        "provider-free install does not fingerprint its staged route service"
    )
publish_install_state = source.index(
    'write "$install_state_path" "$install_state_platform"',
    doctor,
)
install_state_active = source.index(
    "install_state_transaction_active=true",
    doctor,
)
config_inactive = source.index(
    "config_transaction_active=false", finalize_config
)
if not (
    activate_config
    < committed_routing_fingerprint
    < runtime_ready
    < doctor
    < install_state_active
    < publish_install_state
    < finalize_config
    < config_inactive
    < transaction_end
):
    raise SystemExit(
        "stable control-plane journal is not finalized before disarming "
        "rollback"
    )

snapshot_private_tools = source.index("snapshot_private_tool_state")
migrate_legacy_tools = source.index("migrate_legacy_private_tools")
private_tool_exports = source.index("export UV_TOOL_DIR UV_TOOL_BIN_DIR")
python_transaction = source.index("python_transaction_active=true")
provision_python = source.index("install_or_reuse_orichum_python")
snapshot_install_state = source.index(
    'snapshot_path "$install_state_path"'
)
upgrade_mempalace = source.index("uv tool install --upgrade mempalace")
upgrade_graphify = source.index("uv tool install --upgrade 'graphifyy[mcp,terraform]'")
probe_graphify = source.index("reconcile_graphify_storage", upgrade_graphify)
normalize_endpoint = source.index("normalize_headroom_free_endpoint_snapshot")
remove_headroom = source.index("remove_owned_headroom_installation")
if not (
    snapshot_install_state
    < python_transaction
    < provision_python
    < migrate_legacy_tools
    < snapshot_private_tools
    and
    private_tool_exports
    < migrate_legacy_tools
    and
    snapshot_private_tools
    < upgrade_mempalace
    < upgrade_graphify
    < probe_graphify
    < normalize_endpoint
    < remove_headroom
):
    raise SystemExit(
        "legacy migration, private tool snapshot, endpoint normalization, "
        "and Headroom cleanup order is unsafe"
    )
if "headroom" in rollback.lower():
    raise SystemExit("rollback still reinstalls or re-enables Headroom")
if "graphify install" in source:
    raise SystemExit("installer must not invoke graphify install")
for global_skill_root in (
    '$HOME/.agents/skills',
    '$HOME/.codex/skills',
    '$HOME/.claude/skills',
):
    if global_skill_root in source:
        raise SystemExit("installer must not mutate global Graphify skills")
PY

shared_suite_workflow="$ROOT/.github/workflows/amd64-acceptance.yml"
rg -Fq 'if ! bash "$test_script"; then' "$shared_suite_workflow"
rg -Fq 'bash -x "$test_script"' "$shared_suite_workflow"

for acceptance_workflow in \
    "$ROOT/.github/workflows/amd64-acceptance.yml" \
    "$ROOT/.github/workflows/macos-arm64-acceptance.yml"; do
  rg -Fq 'report_acceptance_failure()' "$acceptance_workflow"
  rg -Fq 'trap report_acceptance_failure ERR' "$acceptance_workflow"
  rg -Fq "printf '%s\\n' \"\$doctor_output\"" "$acceptance_workflow"
  python3 - "$acceptance_workflow" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
disable = source.index("trap - ERR", source.index("doctor_output=") - 300)
capture = source.index("doctor_output=", disable)
enable = source.index("trap report_acceptance_failure ERR", capture)
if not disable < capture < enable:
    raise SystemExit("expected doctor failure is not isolated from the ERR trap")
PY
  rg -Fq 'Native acceptance failure' "$acceptance_workflow"
done

rg -Fq 'report_test_failure()' "$ROOT/tests/test_installer.sh"
rg -Fq 'trap report_test_failure ERR' "$ROOT/tests/test_installer.sh"
rg -Fq 'ERROR: test_installer.sh:%s exited %s: %s' \
  "$ROOT/tests/test_installer.sh"
python3 - "$ROOT/install.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
start = source.index("claudex_proxy_runtime_is_owned()")
end = source.index("\n}\n", start) + 3
runtime_check = source[start:end]
if "managed_service_main_pid" not in runtime_check:
    raise SystemExit("route proxy readiness does not verify an active service")
if "claudex_proxy_health_is_ready_at" not in runtime_check:
    raise SystemExit("route proxy readiness does not verify health identity")
if "pid_owns_loopback_listener" in runtime_check:
    raise SystemExit("route proxy readiness still depends on socket metadata")

restart_start = source.index(
    'if [[ "$claudex_proxy_restart_required" == true ]]'
)
restart_end = source.index(
    "claudex_proxy_transaction_active=false", restart_start
)
restart = source[restart_start:restart_end]
bootstrap = restart.index('launchctl bootstrap')
if 'port_is_available "$ROUTE_PROXY_LISTEN_PORT"' in restart[:bootstrap]:
    raise SystemExit(
        "route proxy restart rejects a bindable socket in TIME_WAIT"
    )
if 'loopback_port_is_listening "$ROUTE_PROXY_LISTEN_PORT"' not in restart[:bootstrap]:
    raise SystemExit(
        "route proxy restart does not reject a competing listener"
    )
if restart.index("wait_for_claudex_proxy", bootstrap) <= bootstrap:
    raise SystemExit("route proxy restart omits post-start ownership checks")
PY

printf 'PASS: Orichum installer rollback and port selection\n'
