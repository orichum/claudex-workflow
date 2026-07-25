#!/usr/bin/env bash
set -euo pipefail

report_test_failure() {
  local status="$?"
  printf 'ERROR: test_installer.sh:%s exited %s: %s\n' \
    "${BASH_LINENO[0]:-$LINENO}" "$status" "$BASH_COMMAND" >&2
  exit "$status"
}
trap report_test_failure ERR

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
export ORICHUM_INSTALL_BOOTSTRAP=true
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-installer-test.XXXXXX")"
fixture="$(cd -P "$fixture" && pwd)"
trap 'rm -rf -- "$fixture"' EXIT
install -d -m 0700 "$fixture/install.lock"
exec 9<"$fixture/install.lock"
WORKFLOW_LOCK_FD=9

python_data="$fixture/python-data"
python_root="$python_data/python"
python_bin="$python_root/cpython-3.14.6/bin"
install -d -m 0700 "$python_data/bin" "$python_bin"
cat >"$python_bin/python3.14" <<'PYTHON'
#!/usr/bin/env bash
if [[ "$*" == *platform.python_implementation* ]]; then
  printf 'CPython\t3.14.6\n'
  exit 0
fi
exec python3 "$@"
PYTHON
chmod 0755 "$python_bin/python3.14"
ln -s "$python_bin/python3.14" "$python_data/bin/orichum-python"
[[ "$(orichum_python_root "$python_data")" == "$python_root" ]]
[[ "$(orichum_python_entrypoint "$python_data")" == \
   "$python_data/bin/orichum-python" ]]
IFS=$'\t' read -r managed_version managed_realpath < <(
  validate_orichum_python "$python_data" "$python_data/bin/orichum-python"
)
[[ "$managed_version" == 3.14.6 ]]
[[ "$managed_realpath" == \
   "$(workflow_physical_path "$python_bin/python3.14")" ]]
[[ "$(resolve_orichum_python "$python_data")" == \
   "$python_data/bin/orichum-python" ]]
preflight_orichum_python_runtime \
  "$python_bin/python3.14" "$ROOT" "$python_data"
preflight_source="$(
  sed -n \
    '/^preflight_orichum_python_runtime() (/,/^service_ports_file()/p' \
    "$ROOT/lib/workflow.sh"
)"
rg -Fq 'RouteProxyServer' <<<"$preflight_source"
rg -Fq 'server.server_close()' <<<"$preflight_source"
if rg -Fq 'socket.create_connection' <<<"$preflight_source"; then
  printf 'Python runtime preflight still launches an interpreter per poll\n' >&2
  exit 1
fi
if rg -Fq 'curl ' <<<"$preflight_source"; then
  printf 'Python runtime preflight still depends on asynchronous polling\n' >&2
  exit 1
fi
chmod 0770 "$python_bin"
if validate_orichum_python "$python_data" "$python_bin/python3.14" \
    >"$fixture/writable-python.stdout" \
    2>"$fixture/writable-python.stderr"; then
  printf 'group-writable managed Python directory was accepted\n' >&2
  exit 1
fi
rg -Fq 'writable by group or others' "$fixture/writable-python.stderr"
chmod 0700 "$python_bin"

wrong_python="$python_root/cpython-3.13.9/bin/python3.13"
install -d -m 0700 "$(dirname "$wrong_python")"
sed 's/3\.14\.6/3.13.9/' "$python_bin/python3.14" >"$wrong_python"
chmod 0755 "$wrong_python"
if validate_orichum_python "$python_data" "$wrong_python" \
    >"$fixture/wrong-python.stdout" 2>"$fixture/wrong-python.stderr"; then
  printf 'wrong managed Python version was accepted\n' >&2
  exit 1
fi
rg -Fq 'requires CPython 3.14.x' "$fixture/wrong-python.stderr"

external_python="$fixture/external-python"
cp "$python_bin/python3.14" "$external_python"
chmod 0755 "$external_python"
ln -sfn "$external_python" "$python_data/bin/orichum-python"
if resolve_orichum_python "$python_data" \
    >"$fixture/escaped-python.stdout" \
    2>"$fixture/escaped-python.stderr"; then
  printf 'managed Python symlink escape was accepted\n' >&2
  exit 1
fi
rg -Fq 'outside private Python root' "$fixture/escaped-python.stderr"
ln -sfn "$python_bin/python3.14" "$python_data/bin/orichum-python"

graph_data="$fixture/graph-data"
install -d -m 0700 "$graph_data"
graph_root="$(ensure_private_graph_root "$graph_data")"
[[ "$graph_root" == "$graph_data/graphs" ]]
[[ -d "$graph_root" && ! -L "$graph_root" ]]
[[ "$(path_mode "$graph_root")" == 700 ]]
printf 'preserve\n' >"$graph_root/prior-state"

graph_probe_bin="$fixture/graph-probe-bin"
graph_probe_log="$fixture/graph-probe.log"
install -d -m 0700 "$graph_probe_bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "%s|%s\n" "${GRAPHIFY_OUT:-}" "$*" >>"$GRAPH_PROBE_LOG"' \
  '[[ "${GRAPHIFY_OUT:-}" == /* ]]' \
  'command_name="$1"' \
  'repository="$2"' \
  '[[ "$repository" == /* && -d "$repository/.git" ]]' \
  '[[ "$GRAPHIFY_OUT" != "$repository/graphify-out" ]]' \
  'case "$command_name" in' \
  '  extract)' \
  '    [[ "$3" == --code-only ]]' \
  '    install -d -m 0700 "$GRAPHIFY_OUT"' \
  '    printf "%s\n" "{\"directed\":false,\"multigraph\":false,\"graph\":{},\"nodes\":[{\"id\":\"graphify_probe\",\"label\":\"graphify_probe\"}],\"links\":[]}" >"$GRAPHIFY_OUT/graph.json"' \
  '    ;;' \
  '  update)' \
  '    [[ "$#" -eq 2 && -f "$GRAPHIFY_OUT/graph.json" ]]' \
  '    [[ "${GRAPHIFY_FAIL_UPDATE:-false}" != true ]] || exit 73' \
  '    git -C "$repository" diff --quiet HEAD --' \
  '    git -C "$repository" show HEAD:probe.py | grep -Fq graphify_probe_updated' \
  '    [[ "${GRAPHIFY_STALE_UPDATE:-false}" != true ]] || exit 0' \
  '    printf "%s\n" "{\"directed\":false,\"multigraph\":false,\"graph\":{},\"nodes\":[{\"id\":\"graphify_probe_updated\",\"label\":\"graphify_probe_updated\"}],\"links\":[]}" >"$GRAPHIFY_OUT/graph.json"' \
  '    if [[ "${GRAPHIFY_LOCAL_UPDATE:-false}" == true ]]; then' \
  '      install -d -m 0700 "$repository/graphify-out"' \
  '      cp "$GRAPHIFY_OUT/graph.json" "$repository/graphify-out/graph.json"' \
  '    fi' \
  '    ;;' \
  '  *) exit 64 ;;' \
  'esac' >"$graph_probe_bin/graphify"
chmod 0755 "$graph_probe_bin/graphify"
printf '%s\n' \
  '#!/usr/bin/env python3' \
  'import json, os, sys' \
  'assert sys.argv[1:2] == ["--graph"]' \
  'graph = os.path.realpath(sys.argv[2])' \
  'assert os.path.isabs(graph) and os.path.isfile(graph)' \
  'with open(os.environ["GRAPH_PROBE_LOG"], "a", encoding="utf-8") as log:' \
  '    log.write(f"mcp|--graph {graph}\n")' \
  'for line in sys.stdin:' \
  '    request = json.loads(line)' \
  '    if "id" not in request:' \
  '        continue' \
  '    if request["method"] == "initialize":' \
  '        result = {"serverInfo": {"name": "fake-graphify", "version": "1"}}' \
  '    elif request["method"] == "tools/list":' \
  '        result = {"tools": [{"name": "query_graph"}, {"name": "graph_stats"}]}' \
  '    else:' \
  '        result = {}' \
  '    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)' \
  >"$graph_probe_bin/graphify-mcp"
chmod 0755 "$graph_probe_bin/graphify-mcp"

skill_home="$fixture/skill-home"
install -d -m 0700 \
  "$skill_home/.agents/skills/graphify" \
  "$skill_home/.codex/skills/graphify" \
  "$skill_home/.claude/skills/graphify"
for skill_root in .agents .codex .claude; do
  printf 'unchanged\n' \
    >"$skill_home/$skill_root/skills/graphify/owner-marker"
done
cp -pPR "$skill_home" "$fixture/skill-home-before"

GRAPH_PROBE_LOG="$graph_probe_log" HOME="$skill_home" \
  reconcile_graphify_storage \
    "$graph_data" "$graph_probe_bin/graphify" \
    "$graph_probe_bin/graphify-mcp" "$(command -v python3)" \
    "$ROOT" "$fixture"
rg -Fq '|extract ' "$graph_probe_log"
rg -Fq ' --code-only' "$graph_probe_log"
rg -Fq '|update ' "$graph_probe_log"
rg -Fq 'mcp|--graph ' "$graph_probe_log"
if rg -q '(^|[|[:space:]])install([[:space:]]|$)' "$graph_probe_log"; then
  printf 'Graphify capability probe invoked graphify install\n' >&2
  exit 1
fi
diff -qr -- "$fixture/skill-home-before" "$skill_home" >/dev/null

if GRAPH_PROBE_LOG="$graph_probe_log" GRAPHIFY_FAIL_UPDATE=true \
    HOME="$skill_home" reconcile_graphify_storage \
      "$graph_data" "$graph_probe_bin/graphify" \
      "$graph_probe_bin/graphify-mcp" "$(command -v python3)" \
      "$ROOT" "$fixture" \
      >"$fixture/failed-graph-upgrade.stdout" \
      2>"$fixture/failed-graph-upgrade.stderr"; then
  printf 'failed Graphify upgrade probe unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$(<"$graph_root/prior-state")" == preserve ]]
[[ "$(path_mode "$graph_root")" == 700 ]]
diff -qr -- "$fixture/skill-home-before" "$skill_home" >/dev/null

for unsafe_update_mode in GRAPHIFY_STALE_UPDATE GRAPHIFY_LOCAL_UPDATE; do
  if (
    export "$unsafe_update_mode=true"
    GRAPH_PROBE_LOG="$graph_probe_log" HOME="$skill_home" \
      reconcile_graphify_storage \
        "$graph_data" "$graph_probe_bin/graphify" \
        "$graph_probe_bin/graphify-mcp" "$(command -v python3)" \
        "$ROOT" "$fixture" \
        >"$fixture/$unsafe_update_mode.stdout" \
        2>"$fixture/$unsafe_update_mode.stderr"
  ); then
    printf '%s capability probe unexpectedly succeeded\n' \
      "$unsafe_update_mode" >&2
    exit 1
  fi
  [[ "$(<"$graph_root/prior-state")" == preserve ]]
  [[ "$(path_mode "$graph_root")" == 700 ]]
done

doctor_project="$fixture/doctor-project"
doctor_config="$fixture/doctor-config"
doctor_bin="$fixture/doctor-bin"
install -d -m 0700 "$doctor_project" "$doctor_config" "$doctor_bin"
git -C "$doctor_project" init -q
install -d -m 0700 "$doctor_project/graphify-out"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "graphifyy 2.0.0\n"' >"$doctor_bin/graphify"
chmod 0755 "$doctor_bin/graphify"
printf '1.9.0\n' \
  >"$skill_home/.agents/skills/graphify/.graphify_version"
python3 - "$doctor_config/projects.json" "$doctor_project" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(
        {
            "schemaVersion": 1,
            "contexts": [{"root": sys.argv[2]}],
        },
        stream,
    )
PY
doctor_graph_output="$(
  HOME="$skill_home" graphify_doctor_diagnostics \
    "$graph_data" "$doctor_config" "$ROOT" "$(command -v python3)" \
    "$doctor_bin/graphify"
)"
rg -Fq 'Graphify package/skill drift: 2.0.0 != 1.9.0' \
  <<<"$doctor_graph_output"
rg -Fq 'repository-local legacy Graphify outputs: 1' \
  <<<"$doctor_graph_output"
rg -Fq 'repository graph hooks need reconciliation: 1' \
  <<<"$doctor_graph_output"
[[ -d "$doctor_project/graphify-out" ]]

fake_uv_bin="$fixture/fake-uv-bin"
fake_uv_log="$fixture/fake-uv.log"
install -d -m 0700 "$fake_uv_bin"
cat >"$fake_uv_bin/uv" <<'UV'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$FAKE_UV_LOG"
uv_command="$1 $2"
install_root="${UV_PYTHON_INSTALL_DIR:-}"
if [[ "$uv_command" == "python install" ]]; then
  shift 2
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --install-dir)
        install_root="$2"
        shift 2
        ;;
      *) shift ;;
    esac
  done
fi
runtime="$install_root/cpython-$FAKE_UV_VERSION/bin/python3.14"
case "$uv_command" in
  "python list")
    printf \
      '[{"version":"%s","version_parts":{"major":3,"minor":14,"patch":6}}]\n' \
      "$FAKE_UV_VERSION"
    ;;
  "python install")
    [[ "${FAKE_UV_INSTALL_FAIL:-false}" != true ]] || exit 71
    install -d -m 0700 "$(dirname "$runtime")"
    cat >"$runtime" <<PYTHON
#!/usr/bin/env bash
if [[ "\$*" == *platform.python_implementation* ]]; then
  printf 'CPython\\t$FAKE_UV_VERSION\\n'
  exit 0
fi
exec python3 "\$@"
PYTHON
    chmod 0755 "$runtime"
    ;;
  "python find")
    printf '%s\n' "$runtime"
    ;;
  *) exit 64 ;;
esac
UV
chmod 0755 "$fake_uv_bin/uv"

provisioned_data="$fixture/provisioned-data"
install -d -m 0700 "$provisioned_data/bin"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
  PATH="$fake_uv_bin:$PATH" \
  FAKE_UV_LOG="$fake_uv_log" \
  FAKE_UV_VERSION=3.14.6 \
    install_or_reuse_orichum_python "$provisioned_data"
)
[[ "$python_action" == installed ]]
[[ "$python_version" == 3.14.6 ]]
[[ "$python_candidate" == \
   "$(workflow_physical_path \
     "$provisioned_data/python/cpython-3.14.6/bin/python3.14")" ]]
[[ "$python_generation" == \
   "$provisioned_data/python/cpython-3.14.6" ]]
rg -Fxq \
  'python list --only-downloads --output-format json --no-config 3.14' \
  "$fake_uv_log"
rg -Fq 'python install --install-dir ' "$fake_uv_log"
rg -Fq ' --no-bin --no-config 3.14.6' "$fake_uv_log"
rg -Fxq \
  'python find --managed-python --no-project --no-python-downloads --resolve-links --no-config 3.14.6' \
  "$fake_uv_log"
activate_orichum_python "$provisioned_data" "$python_candidate"
[[ "$(resolve_orichum_python "$provisioned_data")" == \
   "$provisioned_data/bin/orichum-python" ]]

: >"$fake_uv_log"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
  PATH="$fake_uv_bin:$PATH" \
  FAKE_UV_LOG="$fake_uv_log" \
  FAKE_UV_VERSION=3.14.6 \
  FAKE_UV_INSTALL_FAIL=true \
    install_or_reuse_orichum_python "$provisioned_data"
)
[[ "$python_action" == reused ]]
[[ "$python_version" == 3.14.6 ]]
[[ "$python_candidate" == \
   "$(workflow_physical_path \
     "$provisioned_data/python/cpython-3.14.6/bin/python3.14")" ]]
[[ -z "$python_generation" ]]

rollback_data="$fixture/rollback-data"
rollback_snapshot="$fixture/rollback-snapshot"
old_runtime="$rollback_data/python/cpython-3.14.5/bin/python3.14"
install -d -m 0700 \
  "$rollback_data/bin" "$(dirname "$old_runtime")" "$rollback_snapshot"
sed 's/3\.14\.6/3.14.5/' "$python_bin/python3.14" >"$old_runtime"
chmod 0755 "$old_runtime"
ln -s "$old_runtime" "$rollback_data/bin/orichum-python"
corrupt_latest="$rollback_data/python/cpython-3.14.6/bin/python3.14"
install -d -m 0700 "$(dirname "$corrupt_latest")"
printf '#!/usr/bin/env bash\nexit 91\n' >"$corrupt_latest"
chmod 0755 "$corrupt_latest"
snapshot_path \
  "$rollback_data/bin/orichum-python" "$rollback_snapshot" python-entrypoint
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
    PATH="$fake_uv_bin:$PATH" \
    FAKE_UV_LOG="$fake_uv_log" \
    FAKE_UV_VERSION=3.14.6 \
      install_or_reuse_orichum_python "$rollback_data"
  )
[[ "$python_action" == upgraded && -n "$python_generation" ]]
activate_orichum_python "$rollback_data" "$python_candidate"
restore_snapshot \
  "$rollback_data/bin/orichum-python" "$rollback_snapshot" python-entrypoint
remove_orichum_python_generation "$rollback_data" "$python_generation"
[[ -x "$old_runtime" && ! -e "$python_generation" ]]
IFS=$'\t' read -r rollback_version _ < <(
  validate_orichum_python "$rollback_data" \
    "$rollback_data/bin/orichum-python"
)
[[ "$rollback_version" == 3.14.5 ]]

downgrade_data="$fixture/downgrade-data"
newer_runtime="$downgrade_data/python/cpython-3.14.7/bin/python3.14"
install -d -m 0700 \
  "$downgrade_data/bin" "$(dirname "$newer_runtime")"
sed 's/3\.14\.6/3.14.7/' "$python_bin/python3.14" >"$newer_runtime"
chmod 0755 "$newer_runtime"
ln -s "$newer_runtime" "$downgrade_data/bin/orichum-python"
: >"$fake_uv_log"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
    PATH="$fake_uv_bin:$PATH" \
    FAKE_UV_LOG="$fake_uv_log" \
    FAKE_UV_VERSION=3.14.6 \
      install_or_reuse_orichum_python "$downgrade_data"
  )
[[ "$python_action" == reused && "$python_version" == 3.14.7 ]]
[[ -z "$python_generation" ]]
[[ "$(wc -l <"$fake_uv_log" | tr -d ' ')" == 1 ]]

authenticated_release="$fixture/authenticated-release.json"
gh() {
  [[ "$1" == api && "$2" == repos/example/tool/releases/latest ]]
  printf '{"tag_name":"v1.2.3"}\n'
}
curl() {
  printf 'authenticated release lookup unexpectedly used curl\n' >&2
  return 99
}
GH_TOKEN=ephemeral-test-token \
  fetch_latest_github_release example/tool "$authenticated_release"
[[ "$(jq -r .tag_name "$authenticated_release")" == v1.2.3 ]]
unset -f gh curl

anonymous_release="$fixture/anonymous-release.json"
gh() {
  printf 'anonymous release lookup unexpectedly used gh\n' >&2
  return 99
}
curl() {
  local output_file=
  while (($# > 0)); do
    if [[ "$1" == --output ]]; then
      output_file="$2"
      shift 2
    else
      shift
    fi
  done
  [[ -n "$output_file" ]]
  printf '{"tag_name":"v4.5.6"}\n' >"$output_file"
}
GH_TOKEN= fetch_latest_github_release example/tool "$anonymous_release"
[[ "$(jq -r .tag_name "$anonymous_release")" == v4.5.6 ]]
unset -f gh curl

printf '6.8.0-generic\n' >"$fixture/linux-osrelease"
printf '4.4.0-Microsoft\n' >"$fixture/wsl1-osrelease"
printf '5.15.153.1-microsoft-standard-WSL2\n' >"$fixture/wsl2-osrelease"
[[ "$(linux_environment_kind "$fixture/linux-osrelease")" == linux ]]
[[ "$(linux_environment_kind "$fixture/wsl1-osrelease")" == wsl1 ]]
[[ "$(linux_environment_kind "$fixture/wsl2-osrelease")" == wsl2 ]]

migration_library="$fixture/installed-control-plane.sh"
python3 - "$ROOT/install.sh" "$migration_library" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
start_marker = "# BEGIN installed control-plane transaction\n"
end_marker = "# END installed control-plane transaction\n"
try:
    start = source.index(start_marker) + len(start_marker)
    end = source.index(end_marker, start)
except ValueError as error:
    raise SystemExit("installed control-plane transaction library is missing") from error
Path(sys.argv[2]).write_text(source[start:end], encoding="utf-8")
PY
# shellcheck source=/dev/null
source "$migration_library"
rollback_library="$fixture/installed-control-plane-rollback.sh"
python3 - "$ROOT/install.sh" "$rollback_library" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
start = source.index("rollback_install_transaction()")
end = source.index("WORKFLOW_ROLLBACK_HANDLER=", start)
Path(sys.argv[2]).write_text(source[start:end], encoding="utf-8")
PY
# shellcheck source=/dev/null
source "$rollback_library"

v1_config="$fixture/v1-config"
v1_candidate="$fixture/v1-candidate"
install -d -m 0700 "$v1_config"
for control_file in \
    model-stacks.json projects.json providers.json plugins.json runtime.json \
    controller-policy.md; do
  install -m 0600 "$ROOT/config/$control_file" "$v1_config/$control_file"
done
printf '%s\n' \
  '{"schemaVersion":2,"accounts":[{' \
  '"id":"oc-a-1111111111111111","name":"Primary OpenAI",' \
  '"provider":"openai","credentialRef":"openai.json","pool":"shared",' \
  '"routingPrefix":"oc-r-1111111111111111","priority":100,' \
  '"state":"active","originalPrefix":null,"originalPriority":null}]}' \
  >"$v1_config/accounts.json"
chmod 0600 "$v1_config/accounts.json"
jq '
  {
    schemaVersion: 1,
    defaultStack,
    models: (
      .models | with_entries(
        .value = {
          provider: (.value.routes | keys[0]),
          family: .value.family,
          upstream: (.value.routes | to_entries[0].value)
        }
      )
    ),
    stacks: (
      .stacks | with_entries(
        .value = {
          controller: .value.controller[0].model,
          agents: (
            .value.agents | with_entries(
              .value = [.value[].model]
            )
          )
        }
      )
    )
  }
' "$ROOT/config/model-stacks.json" >"$v1_config/model-stacks.json"
printf '%s\n' \
  '{"schemaVersion":1,"candidateAccounts":{' \
  '"oc-c-c64159d152c2cf90":"oc-a-1111111111111111"}}' \
  >"$v1_config/stack-bindings.json"
chmod 0600 "$v1_config/model-stacks.json" "$v1_config/stack-bindings.json"
rm "$v1_config/plugins.json"
cp "$v1_config/model-stacks.json" "$fixture/v1-model-stacks.saved"
cp "$v1_config/stack-bindings.json" "$fixture/v1-bindings.saved"
install -d -m 0700 "$fixture/v1-snapshot"
snapshot_path "$v1_config/model-stacks.json" \
  "$fixture/v1-snapshot" model-stacks
snapshot_path "$v1_config/stack-bindings.json" \
  "$fixture/v1-snapshot" stack-bindings

stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v1_config" "$v1_candidate"
cmp "$fixture/v1-model-stacks.saved" "$v1_config/model-stacks.json"
cmp "$fixture/v1-bindings.saved" "$v1_config/stack-bindings.json"
jq -e '.schemaVersion == 2 and .stacks.balanced' \
  "$v1_candidate/model-stacks.json" >/dev/null
cmp "$fixture/v1-bindings.saved" "$v1_candidate/stack-bindings.json"

activation_snapshot="$fixture/activation-snapshot"
install -d -m 0700 "$activation_snapshot"
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v1_candidate" "$v1_config" \
  "$activation_snapshot" "$WORKFLOW_LOCK_FD"
jq -e '.schemaVersion == 2 and .stacks.balanced' \
  "$v1_config/model-stacks.json" >/dev/null

trap - ERR
set +e
(
  workflow_cleanup_init
  WORKFLOW_LOCK_FD=9
  snapshot_dir="$activation_snapshot"
  control_plane_journal="$activation_snapshot"
  INSTALLED_CONFIG_ROOT="$v1_config"
  WORKFLOW_ROOT="$ROOT"
  ORICHUM_PYTHON="$python_bin/python3.14"
  config_transaction_active=true
  python_transaction_active=false
  private_tools_transaction_active=false
  cliproxy_transaction_active=false
  endpoint_transaction_active=false
  claudex_proxy_transaction_active=false
  headroom_transaction_active=false
  claudex_proxy_runtime_mutated=false
  orichum_launcher_mutated=false
  endpoint_lock_owned=false
  WORKFLOW_ROLLBACK_HANDLER=rollback_install_transaction
  WORKFLOW_TRANSACTION_ACTIVE=true
  verify_committed_control_plane() { return 73; }
  verify_committed_control_plane
  workflow_cleanup "$?"
)
activation_failure_rc=$?
set -e
trap report_test_failure ERR
[[ "$activation_failure_rc" -eq 73 ]]
cmp "$fixture/v1-model-stacks.saved" "$v1_config/model-stacks.json"
cmp "$fixture/v1-bindings.saved" "$v1_config/stack-bindings.json"
[[ ! -e "$v1_config/plugins.json" && ! -L "$v1_config/plugins.json" ]]
[[ -z "$(find "$v1_config" -maxdepth 1 -name '.model-stacks.transaction*' \
  -print -quit)" ]]

activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v1_candidate" "$v1_config" \
  "$activation_snapshot" "$WORKFLOW_LOCK_FD"
jq -e '.schemaVersion == 2 and .stacks.balanced' \
  "$v1_config/model-stacks.json" >/dev/null
jq -e \
  '.candidateAccounts["oc-c-c64159d152c2cf90"] == "oc-a-1111111111111111"' \
  "$v1_config/stack-bindings.json" >/dev/null
[[ "$(path_mode "$v1_config/model-stacks.json")" == 600 ]]
[[ "$(path_mode "$v1_config/stack-bindings.json")" == 600 ]]

v2_config="$fixture/v2-config"
v2_candidate="$fixture/v2-candidate"
install -d -m 0700 "$v2_config"
cp -p "$v1_config/"* "$v2_config/"
jq '
  .defaultStack = "heavy" |
  .stacks = {heavy: .stacks.balanced}
' "$v1_config/model-stacks.json" >"$v2_config/model-stacks.json"
chmod 0600 "$v2_config/model-stacks.json"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_config" "$v2_candidate"
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_candidate" "$v2_config" \
  "$fixture/v2-activation-snapshot" "$WORKFLOW_LOCK_FD"
jq -e '.schemaVersion == 2 and .stacks.heavy' \
  "$v2_config/model-stacks.json" >/dev/null
cp "$v2_config/model-stacks.json" "$fixture/v2-first-run.saved"
cp "$v2_config/stack-bindings.json" "$fixture/v2-bindings.saved"
finalize_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" \
  "$fixture/v2-activation-snapshot" "$WORKFLOW_LOCK_FD"
rm -rf -- "$v2_candidate"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_config" "$v2_candidate"
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_candidate" "$v2_config" \
  "$fixture/v2-activation-snapshot" "$WORKFLOW_LOCK_FD"
cmp "$fixture/v2-first-run.saved" "$v2_config/model-stacks.json"
cmp "$fixture/v2-bindings.saved" "$v2_config/stack-bindings.json"

concurrent_config="$fixture/concurrent-config"
concurrent_candidate="$fixture/concurrent-candidate"
concurrent_snapshot="$fixture/concurrent-snapshot"
concurrent_ready="$fixture/concurrent.ready"
concurrent_release="$fixture/concurrent.release"
install -d -m 0700 "$concurrent_config"
cp -p "$v2_config/"* "$concurrent_config/"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" \
  "$concurrent_config" "$concurrent_candidate"
mkfifo "$concurrent_ready" "$concurrent_release"
python3 - \
  "$ROOT" "$concurrent_config" "$concurrent_ready" \
  "$concurrent_release" <<'PY' &
from dataclasses import replace
from pathlib import Path
import sys
from types import MappingProxyType

root = Path(sys.argv[1])
config = Path(sys.argv[2]).resolve()
ready = Path(sys.argv[3])
release = Path(sys.argv[4])
sys.path.insert(0, str(root))

from integrations.common.project_context import control_plane_transaction
from integrations.common.stack_store import load_stack_snapshot, save_stack

model = config / "model-stacks.json"
bindings = config / "stack-bindings.json"
with control_plane_transaction(config):
    with ready.open("wb") as signal:
        signal.write(b"x")
    with release.open("rb") as gate:
        gate.read(1)
    snapshot = load_stack_snapshot(model, bindings)
    updated = replace(
        snapshot.stacks,
        models=MappingProxyType(
            {
                **snapshot.stacks.models,
                "concurrent-model": next(
                    iter(snapshot.stacks.models.values())
                ),
            }
        ),
    )
    save_stack(snapshot, updated, snapshot.bindings)
PY
writer_pid=$!
IFS= read -r -n 1 <"$concurrent_ready"
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" \
  "$concurrent_candidate" "$concurrent_config" "$concurrent_snapshot" \
  "$WORKFLOW_LOCK_FD" &
activation_pid=$!
printf x >"$concurrent_release"
wait "$writer_pid"
wait "$activation_pid"
jq -e '.models["concurrent-model"] and .defaultStack == "heavy"' \
  "$concurrent_config/model-stacks.json" >/dev/null

unlocked_config="$fixture/unlocked-config"
unlocked_candidate="$fixture/unlocked-candidate"
install -d -m 0700 "$unlocked_config"
for control_file in \
    model-stacks.json projects.json providers.json plugins.json runtime.json \
    controller-policy.md; do
  install -m 0600 "$ROOT/config/$control_file" \
    "$unlocked_config/$control_file"
done
printf '{"schemaVersion":2,"accounts":[]}\n' >"$unlocked_config/accounts.json"
chmod 0600 "$unlocked_config/accounts.json"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$unlocked_config" "$unlocked_candidate"
[[ ! -e "$unlocked_candidate/stack-bindings.json" ]]
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$unlocked_candidate" "$unlocked_config" \
  "$fixture/unlocked-activation-snapshot" "$WORKFLOW_LOCK_FD"
[[ ! -e "$unlocked_config/stack-bindings.json" ]]

unsafe_config="$fixture/unsafe-config"
unsafe_candidate="$fixture/unsafe-candidate"
install -d -m 0700 "$unsafe_config"
cp -p "$v1_config/"* "$unsafe_config/"
mv "$unsafe_config/model-stacks.json" "$unsafe_config/model-stacks.real"
ln -s model-stacks.real "$unsafe_config/model-stacks.json"
if stage_installed_control_plane \
    "$python_bin/python3.14" "$ROOT" "$unsafe_config" "$unsafe_candidate" \
    >"$fixture/unsafe-symlink.stdout" \
    2>"$fixture/unsafe-symlink.stderr"; then
  printf 'symlinked installed model stacks were accepted\n' >&2
  exit 1
fi
rg -Fq 'model stacks is unsafe' "$fixture/unsafe-symlink.stderr"
rm "$unsafe_config/model-stacks.json"
mv "$unsafe_config/model-stacks.real" "$unsafe_config/model-stacks.json"
chmod 0644 "$unsafe_config/model-stacks.json"
if stage_installed_control_plane \
    "$python_bin/python3.14" "$ROOT" "$unsafe_config" "$unsafe_candidate" \
    >"$fixture/unsafe-mode.stdout" 2>"$fixture/unsafe-mode.stderr"; then
  printf 'unsafe installed model-stack mode was accepted\n' >&2
  exit 1
fi
rg -Fq 'model stacks is unsafe' "$fixture/unsafe-mode.stderr"
chmod 0600 "$unsafe_config/model-stacks.json"
mv "$unsafe_config/stack-bindings.json" \
  "$unsafe_config/stack-bindings.real"
ln -s stack-bindings.real "$unsafe_config/stack-bindings.json"
if stage_installed_control_plane \
    "$python_bin/python3.14" "$ROOT" "$unsafe_config" "$unsafe_candidate" \
    >"$fixture/unsafe-binding-symlink.stdout" \
    2>"$fixture/unsafe-binding-symlink.stderr"; then
  printf 'symlinked installed stack bindings were accepted\n' >&2
  exit 1
fi
rg -Fq 'stack bindings are unsafe' \
  "$fixture/unsafe-binding-symlink.stderr"
rm "$unsafe_config/stack-bindings.json"
mv "$unsafe_config/stack-bindings.real" \
  "$unsafe_config/stack-bindings.json"
chmod 0644 "$unsafe_config/stack-bindings.json"
if stage_installed_control_plane \
    "$python_bin/python3.14" "$ROOT" "$unsafe_config" "$unsafe_candidate" \
    >"$fixture/unsafe-binding-mode.stdout" \
    2>"$fixture/unsafe-binding-mode.stderr"; then
  printf 'unsafe installed stack-binding mode was accepted\n' >&2
  exit 1
fi
rg -Fq 'stack bindings are unsafe' "$fixture/unsafe-binding-mode.stderr"

python3 - "$ROOT" "$unsafe_config/model-stacks.json" <<'PY'
import os
from pathlib import Path
import sys
from unittest import mock

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from integrations.common import install_control_plane

with mock.patch.object(
    install_control_plane.os, "getuid", return_value=os.getuid() + 1
):
    try:
        install_control_plane._private_bytes(
            Path(sys.argv[2]), "model stacks", 1024 * 1024
        )
    except install_control_plane.InstallControlPlaneError as error:
        if "unsafe" not in str(error):
            raise
    else:
        raise SystemExit("foreign-owner model stacks were accepted")
PY

for script in \
    install.sh lib/workflow.sh bin/orichum bin/orichum-context \
    bin/orichum-doctor bin/orichum-headroom bin/orichum-login \
    bin/orichum-plugin bin/orichum-route-proxy \
    bin/orichum-runtime-ready bin/orichum-verify-cliproxy; do
  bash -n "$ROOT/$script"
done
if rg -Fq 'anthropic_proxy.py' "$ROOT/install.sh"; then
  printf 'route runtime fingerprint references a nonexistent legacy module\n' >&2
  exit 1
fi
rg -Fq 'root.glob("*.py")' "$ROOT/install.sh"

rg -Fq 'export PATH="$UV_TOOL_BIN_DIR:$HOME/.local/bin:$PATH"' \
  "$ROOT/install.sh"

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 18787 13456 13457
[[ "$(read_service_ports "$ports_root")" == \
   $'18317\t18787\t13456\t13457' ]]
[[ "$(jq -r 'keys | @tsv' "$(service_ports_file "$ports_root")")" == \
   $'claudexProxyPort\tcliproxyPort\theadroomPort\trouteProxyPort' ]]
[[ "$(path_mode "$(service_ports_file "$ports_root")")" == 600 ]]
printf '{"cliproxyPort":18318,"headroomPort":18788,"routeProxyPort":13458}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18318\t18788\t13456\t13458' ]]
printf '{"claudexProxyPort":13459,"cliproxyPort":18319,"headroomPort":18789}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18319\t18789\t13456\t13459' ]]
if write_service_ports "$ports_root" 18317 18317 13456 13457; then
  printf 'duplicate ports were accepted\n' >&2
  exit 1
fi

management_key='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-'
render_cliproxy_config \
  "$fixture/cliproxy.yaml" "$fixture/auth" 18317 "$management_key"
rg -Fq 'host: "127.0.0.1"' "$fixture/cliproxy.yaml"
rg -Fq 'port: 18317' "$fixture/cliproxy.yaml"
rg -Fq "secret-key: \"$management_key\"" "$fixture/cliproxy.yaml"
rg -Fq 'max-retry-credentials: 0' "$fixture/cliproxy.yaml"

effective="$fixture/effective.json"
jq -n '{
  stack: "balanced",
  controller: "oc-r-0000000000000001/gpt-5.6-sol",
  agents: {
    "repository-explorer": "oc-r-0000000000000001/gpt-5.6-terra",
    "repository-verifier": "oc-r-0000000000000001/gpt-5.6-terra",
    "correctness-critic": "oc-r-0000000000000002/claude-sonnet-5",
    "architecture-advisor": "oc-r-0000000000000002/claude-opus-4-8",
    "implementation-worker": "oc-r-0000000000000001/gpt-5.6-sol"
  }
}' >"$effective"
render_discovered_claudex_config \
  "$effective" "$fixture/claudex.toml" 18317 18787 13456 13457
rg -Fq 'proxy_port = 13456' "$fixture/claudex.toml"
rg -Fq 'base_url = "http://127.0.0.1:18787"' "$fixture/claudex.toml"
rg -Fq \
  'X-Headroom-Base-Url = "http://127.0.0.1:13457"' \
  "$fixture/claudex.toml"
rg -Fq 'X-Orichum-Session-ID = "unbound"' "$fixture/claudex.toml"

data_root="$fixture/data"
install -d -m 0700 \
  "$data_root/bin" "$data_root/state" "$data_root/logs" \
  "$data_root/headroom/bin" "$data_root/headroom/config" \
  "$data_root/headroom/state"
touch "$data_root/bin/cli-proxy-api" "$data_root/bin/orichum-route-proxy"
chmod 0755 "$data_root/bin/cli-proxy-api" \
  "$data_root/bin/orichum-route-proxy"
headroom="$data_root/headroom/bin/headroom"
touch "$headroom"
chmod 0755 "$headroom"

render_launch_agent "$fixture/cliproxy.plist" "$data_root"
render_claudex_proxy_launch_agent \
  "$fixture/route.plist" "$data_root" "$ROOT" 13457 18317 \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
render_headroom_launch_agent \
  "$fixture/headroom.plist" "$data_root" "$headroom" \
  "$fixture/ca.pem" 18787 13457
cliproxy_service_is_owned "$fixture/cliproxy.plist" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.plist" "$data_root" "$ROOT"
headroom_service_is_owned "$fixture/headroom.plist" "$data_root" new
sed 's/io.orichum.headroom/com.user.claudex-headroom/' \
  "$fixture/headroom.plist" >"$fixture/previous-headroom.plist"
headroom_service_is_owned \
  "$fixture/previous-headroom.plist" "$data_root" legacy
sed 's#http://127.0.0.1:13457#http://127.0.0.2:13457#' \
  "$fixture/headroom.plist" >"$fixture/foreign-headroom.plist"
if headroom_service_is_owned \
    "$fixture/foreign-headroom.plist" "$data_root" new; then
  printf 'foreign Headroom upstream was accepted\n' >&2
  exit 1
fi
rg -Fq '<string>io.orichum.cliproxy</string>' "$fixture/cliproxy.plist"
rg -Fq '<string>io.orichum.route-proxy</string>' "$fixture/route.plist"
rg -Fq 'Orichum route runtime SHA-256: aaaaaaaaaa' "$fixture/route.plist"
rg -Fq "<string>$data_root/bin/orichum-route-proxy</string>" \
  "$fixture/route.plist"
rg -Fq '<key>ORICHUM_DATA_HOME</key>' "$fixture/route.plist"
rg -Fq '<string>--data-home</string>' "$fixture/route.plist"
awk '
  skip { skip = 0; next }
  /<key>ORICHUM_DATA_HOME<\/key>/ { skip = 1; next }
  { print }
' "$fixture/route.plist" >"$fixture/previous-route.plist"
claudex_proxy_service_is_owned \
  "$fixture/previous-route.plist" "$data_root" "$ROOT"
rg -Fq '<string>io.orichum.headroom</string>' "$fixture/headroom.plist"
rg -Fq '<string>--anthropic-api-url</string>' "$fixture/headroom.plist"
rg -Fq '<string>http://127.0.0.1:13457</string>' \
  "$fixture/headroom.plist"

render_systemd_user_unit "$fixture/cliproxy.service" "$data_root"
render_claudex_proxy_systemd_user_unit \
  "$fixture/route.service" "$data_root" "$ROOT" 13457 18317 \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
render_headroom_systemd_user_unit \
  "$fixture/headroom.service" "$data_root" "$headroom" \
  "$fixture/ca.pem" 18787 13457
cliproxy_service_is_owned "$fixture/cliproxy.service" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.service" "$data_root" "$ROOT"
headroom_service_is_owned "$fixture/headroom.service" "$data_root" new
sed \
  's/Description=Orichum Headroom proxy/Description=Claudex Headroom proxy/' \
  "$fixture/headroom.service" >"$fixture/previous-headroom.service"
headroom_service_is_owned \
  "$fixture/previous-headroom.service" "$data_root" legacy
rg -Fq 'Description=Orichum same-family recovery proxy' \
  "$fixture/route.service"
rg -Fq 'Orichum route runtime SHA-256: aaaaaaaaaa' \
  "$fixture/route.service"
rg -Fq "$data_root/bin/orichum-route-proxy" "$fixture/route.service"
rg -Fq "Environment=\"ORICHUM_DATA_HOME=$data_root\"" \
  "$fixture/route.service"
sed '/^Environment="ORICHUM_DATA_HOME=/d' \
  "$fixture/route.service" >"$fixture/previous-route.service"
claudex_proxy_service_is_owned \
  "$fixture/previous-route.service" "$data_root" "$ROOT"
rg -Fq 'Wants=orichum-cliproxy.service' "$fixture/route.service"
rg -Fq 'resolve_orichum_python' "$ROOT/bin/orichum-route-proxy"
rg -Fq -- '--anthropic-api-url http://127.0.0.1:13457' \
  "$fixture/headroom.service"
rg -Fq -- '--disable-kompress' "$fixture/headroom.service"
rg -Fq 'StandardOutput=journal' "$fixture/headroom.service"
rg -Fq 'StandardError=journal' "$fixture/headroom.service"
if rg -Fq 'append:' "$fixture/headroom.service"; then
  printf 'systemd Headroom unit still uses an invalid quoted append target\n' >&2
  exit 1
fi

rg -Fq 'for launcher in orichum' "$ROOT/install.sh"
if rg -q 'for launcher in .*claudex-gpt' "$ROOT/install.sh"; then
  printf 'legacy launchers are still installed\n' >&2
  exit 1
fi
rg -Fq 'ORICHUM_ROUTE_PROXY_PORT' "$ROOT/install.sh"
rg -Fq 'com.user.claudex-headroom.plist' "$ROOT/install.sh"
rg -Fq 'claudex-headroom.service' "$ROOT/install.sh"
rg -Fq "headroom-ai[proxy,code]" "$ROOT/lib/workflow.sh"
if rg -Fq "headroom-ai[all]" "$ROOT/lib/workflow.sh"; then
  printf 'Headroom still installs the unbounded all extra\n' >&2
  exit 1
fi
rg -Fq 'headroom_service_is_ready' "$ROOT/install.sh"
rg -Fq \
  'Headroom did not become fully ready after route proxy activation' \
  "$ROOT/install.sh"
rg -Fq 'preflight_claudex_translation_proxy' "$ROOT/install.sh"
rg -Fq \
  'Claudex translation proxy failed isolated bind and catalogue preflight' \
  "$ROOT/install.sh"
rg -Fq '"$USER_BIN_DIR/orichum" doctor' "$ROOT/install.sh"
if rg -Fq 'Next: orichum doctor' "$ROOT/install.sh"; then
  printf 'installer still delegates final health verification to the user\n' >&2
  exit 1
fi
rg -Fq 'io.orichum.route-proxy' "$ROOT/lib/workflow.sh"
if rg -Fq 'home=Path.home()' "$ROOT/install.sh"; then
  printf 'installer uses obsolete load_control_plane home argument\n' >&2
  exit 1
fi

printf 'installer contract tests passed\n'
