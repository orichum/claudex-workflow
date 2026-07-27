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

[[ "$(parse_install_mode)" == fast ]]
[[ "$(parse_install_mode --upgrade)" == upgrade ]]
[[ "$(parse_install_mode --uninstall)" == uninstall ]]
[[ "$(parse_install_mode --uninstall --purge)" == purge ]]
if parse_install_mode --purge >/dev/null 2>&1; then
  printf 'standalone --purge was accepted\n' >&2
  exit 1
fi

matching_digest="$(printf 'a%.0s' {1..64})"
changed_digest="$(printf 'b%.0s' {1..64})"
matching_manifest="$fixture/matching-manifest.json"
jq -n \
  --arg digest "$matching_digest" \
  '{
    schemaVersion: 1,
    platform: "darwin:aarch64",
    components: {
      cliproxy: {
        version: "7.2.97",
        sourceIdentity: "github:router-for-me/CLIProxyAPI@v7.2.97",
        artifactSha256: $digest,
        inputSha256: $digest,
        probeSha256: $digest
      }
    }
  }' >"$matching_manifest"
component_state_matches \
  "$matching_manifest" cliproxy 7.2.97 \
  github:router-for-me/CLIProxyAPI@v7.2.97 \
  "$matching_digest" "$matching_digest" "$matching_digest"
if component_state_matches \
    "$matching_manifest" cliproxy 7.2.97 \
    github:router-for-me/CLIProxyAPI@v7.2.97 \
    "$changed_digest" "$matching_digest" "$matching_digest"; then
  printf 'changed component artifact matched installer state\n' >&2
  exit 1
fi
INSTALL_MODE=fast
[[ "$(decide_install_component \
  "$matching_manifest" cliproxy 7.2.97 \
  github:router-for-me/CLIProxyAPI@v7.2.97 \
  "$matching_digest" "$matching_digest" "$matching_digest")" == reused ]]
[[ "$(decide_install_component \
  "$matching_manifest" cliproxy 7.2.97 \
  github:router-for-me/CLIProxyAPI@v7.2.97 \
  "$changed_digest" "$matching_digest" "$matching_digest")" == repaired ]]
[[ "$(decide_install_component \
  "$matching_manifest" cliproxy 7.2.97 \
  github:router-for-me/CLIProxyAPI@v7.2.97 \
  "$matching_digest" "$matching_digest" "$changed_digest")" == repaired ]]
INSTALL_MODE=upgrade
[[ "$(decide_install_component \
  "$matching_manifest" cliproxy 7.2.97 \
  github:router-for-me/CLIProxyAPI@v7.2.97 \
  "$matching_digest" "$matching_digest" "$matching_digest")" == upgraded ]]
INSTALL_MODE=fast

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

[[ "$(leanctx_release_suffix darwin aarch64)" == \
   '-aarch64-apple-darwin.tar.gz' ]]
[[ "$(leanctx_release_suffix darwin x86_64)" == \
   '-x86_64-apple-darwin.tar.gz' ]]
[[ "$(leanctx_release_suffix systemd aarch64)" == \
   '-aarch64-unknown-linux-gnu.tar.gz' ]]
[[ "$(leanctx_release_suffix systemd x86_64)" == \
   '-x86_64-unknown-linux-gnu.tar.gz' ]]
if leanctx_release_suffix systemd unsupported \
    >"$fixture/leanctx-arch.stdout" 2>"$fixture/leanctx-arch.stderr"; then
  printf 'unsupported LeanCTX architecture was accepted\n' >&2
  exit 1
fi

managed_bin="$fixture/managed-bin"
install -d -m 0700 "$managed_bin"
printf '#!/bin/sh\nexit 0\n' >"$managed_bin/tool"
chmod 0755 "$managed_bin/tool"
managed_executable_is_safe "$managed_bin/tool"
chmod 0777 "$managed_bin/tool"
if managed_executable_is_safe "$managed_bin/tool"; then
  printf 'unsafe managed executable permissions were accepted\n' >&2
  exit 1
fi
chmod 0755 "$managed_bin/tool"
ln -s "$managed_bin/tool" "$managed_bin/tool-link"
if managed_executable_is_safe "$managed_bin/tool-link"; then
  printf 'managed executable symlink was accepted\n' >&2
  exit 1
fi

leanctx_probe="$fixture/lean-ctx"
cat >"$leanctx_probe" <<'PY'
#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

required = {
    "LEAN_CTX_HEADLESS": "1",
    "LEAN_CTX_AUTONOMY": "false",
    "LEAN_CTX_FULL_TOOLS": "0",
}
if any(os.environ.get(key) != value for key, value in required.items()):
    raise SystemExit(3)
root = Path(os.environ["LEAN_CTX_PROJECT_ROOT"])
data = Path(os.environ["LEAN_CTX_DATA_DIR"])
if not root.is_dir() or not (data / "config.toml").is_file():
    raise SystemExit(4)
tools = [
    "ctx_read",
    "ctx_search",
    "ctx_tree",
    "ctx_expand",
    "ctx_patch",
    "ctx_shell",
]
extra = os.environ.get("FAKE_LEANCTX_EXTRA")
if extra:
    tools.append(extra)
omitted = os.environ.get("FAKE_LEANCTX_OMIT")
if omitted:
    tools.remove(omitted)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-leanctx", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in tools
            ]
        }
    else:
        continue
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ),
        flush=True,
    )
PY
chmod 0755 "$leanctx_probe"
probe_leanctx_capabilities \
  "$leanctx_probe" "$python_bin/python3.14" "$ROOT" "$fixture"
if FAKE_LEANCTX_EXTRA=ctx_call probe_leanctx_capabilities \
    "$leanctx_probe" "$python_bin/python3.14" "$ROOT" "$fixture" \
    >"$fixture/leanctx-extra.stdout" 2>"$fixture/leanctx-extra.stderr"; then
  printf 'LeanCTX capability probe accepted ctx_call\n' >&2
  exit 1
fi
rg -Fq 'unexpected MCP tool is available: ctx_call' \
  "$fixture/leanctx-extra.stderr"
if FAKE_LEANCTX_OMIT=ctx_patch probe_leanctx_capabilities \
    "$leanctx_probe" "$python_bin/python3.14" "$ROOT" "$fixture" \
    >"$fixture/leanctx-missing.stdout" 2>"$fixture/leanctx-missing.stderr"; then
  printf 'LeanCTX capability probe accepted missing ctx_patch\n' >&2
  exit 1
fi
rg -Fq 'required MCP tool is unavailable: ctx_patch' \
  "$fixture/leanctx-missing.stderr"

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

: >"$fake_uv_log"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
  PATH="$fake_uv_bin:$PATH" \
  FAKE_UV_LOG="$fake_uv_log" \
  FAKE_UV_VERSION=3.14.6 \
    install_or_reuse_orichum_python \
      "$provisioned_data" false 3.14.6
)
[[ "$python_action" == reused ]]
[[ "$python_version" == 3.14.6 ]]
[[ "$python_candidate" == \
   "$(workflow_physical_path \
     "$provisioned_data/python/cpython-3.14.6/bin/python3.14")" ]]
[[ -z "$python_generation" ]]
[[ ! -s "$fake_uv_log" ]]

python_runtime="$provisioned_data/python/cpython-3.14.6/bin/python3.14"
python_runtime_backup="$fixture/python3.14.saved"
cp -p "$python_runtime" "$python_runtime_backup"
python_recorded_sha="$(sha256_file "$python_runtime")"
printf '# drift\n' >>"$python_runtime"
: >"$fake_uv_log"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
  PATH="$fake_uv_bin:$PATH" \
  FAKE_UV_LOG="$fake_uv_log" \
  FAKE_UV_VERSION=3.14.6 \
    install_or_reuse_orichum_python \
      "$provisioned_data" false 3.14.6 "$python_recorded_sha"
)
[[ "$python_action" == repaired ]]
[[ "$python_version" == 3.14.6 ]]
[[ -n "$python_generation" ]]
[[ "$(sha256_file "$python_candidate")" == "$python_recorded_sha" ]]
if rg -Fq 'python list ' "$fake_uv_log"; then
  printf 'recorded Python repair resolved latest upstream version\n' >&2
  exit 1
fi
rg -Fq 'python install ' "$fake_uv_log"
remove_orichum_python_generation "$provisioned_data" "$python_generation"
cp -p "$python_runtime_backup" "$python_runtime"

newer_recorded_runtime="$provisioned_data/python/cpython-3.14.7/bin/python3.14"
install -d -m 0700 "$(dirname "$newer_recorded_runtime")"
sed 's/3\.14\.6/3.14.7/' "$python_runtime_backup" \
  >"$newer_recorded_runtime"
chmod 0755 "$newer_recorded_runtime"
ln -sfn "$newer_recorded_runtime" \
  "$provisioned_data/bin/orichum-python"
: >"$fake_uv_log"
IFS=$'\t' read -r \
  python_action python_version python_candidate python_generation < <(
  PATH="$fake_uv_bin:$PATH" \
  FAKE_UV_LOG="$fake_uv_log" \
  FAKE_UV_VERSION=3.14.6 \
    install_or_reuse_orichum_python \
      "$provisioned_data" false 3.14.6 "$python_recorded_sha"
)
[[ "$python_action" == repaired ]]
[[ "$python_version" == 3.14.6 ]]
[[ "$(sha256_file "$python_candidate")" == "$python_recorded_sha" ]]
remove_orichum_python_generation "$provisioned_data" "$python_generation"
ln -sfn "$python_runtime" "$provisioned_data/bin/orichum-python"
rm -rf -- "$(dirname "$(dirname "$newer_recorded_runtime")")"

printf '# drift again\n' >>"$python_runtime"
if PATH="$fake_uv_bin:$PATH" \
   FAKE_UV_LOG="$fake_uv_log" \
   FAKE_UV_VERSION=3.14.6 \
   FAKE_UV_INSTALL_FAIL=true \
    install_or_reuse_orichum_python \
      "$provisioned_data" false 3.14.6 "$python_recorded_sha" \
      >"$fixture/python-repair.stdout" \
      2>"$fixture/python-repair.stderr"; then
  printf 'failed exact Python repair reused a drifted runtime\n' >&2
  exit 1
fi
rg -Fq 'could not install private CPython 3.14.6' \
  "$fixture/python-repair.stderr"
cp -p "$python_runtime_backup" "$python_runtime"

if INSTALL_MODE=upgrade \
   PATH="$fake_uv_bin:$PATH" \
   FAKE_UV_LOG="$fake_uv_log" \
   FAKE_UV_VERSION=3.14.7 \
   FAKE_UV_INSTALL_FAIL=true \
    install_or_reuse_orichum_python "$provisioned_data" true \
      >"$fixture/python-upgrade.stdout" \
      2>"$fixture/python-upgrade.stderr"; then
  printf 'failed explicit Python upgrade reused the prior runtime\n' >&2
  exit 1
fi
rg -Fq 'could not install private CPython 3.14.7' \
  "$fixture/python-upgrade.stderr"

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

recorded_binary_root="$fixture/recorded-binary"
recorded_binary="$recorded_binary_root/tool"
recorded_release_log="$fixture/recorded-release.log"
install -d -m 0700 "$recorded_binary_root"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "tool 1.2.3\n"' >"$recorded_binary"
chmod 0755 "$recorded_binary"
fetch_latest_github_release() {
  printf 'unexpected release lookup\n' >>"$recorded_release_log"
  return 97
}
fetch_github_release_tag() {
  printf 'unexpected tagged release lookup\n' >>"$recorded_release_log"
  return 97
}
curl() {
  printf 'unexpected artifact download\n' >>"$recorded_release_log"
  return 97
}
recorded_state="$(
  stage_github_binary \
    example/tool tool- .tar.gz tool \
    "$recorded_binary" "$fixture/recorded-stage" \
    false 1.2.3 github:example/tool@v1.2.3 \
    "$(sha256_file "$recorded_binary")"
)"
[[ "$(jq -r '.version' <<<"$recorded_state")" == 1.2.3 ]]
[[ "$(jq -r '.changed' <<<"$recorded_state")" == false ]]
[[ "$(jq -r '.staged_path' <<<"$recorded_state")" == null ]]
[[ ! -e "$recorded_release_log" ]]
unset -f fetch_latest_github_release fetch_github_release_tag curl

repair_archive_root="$fixture/repair-archive"
repair_archive="$fixture/tool-1.2.3.tar.gz"
repair_fetch_log="$fixture/repair-fetch.log"
install -d -m 0700 "$repair_archive_root"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "tool 1.2.3\n"' >"$repair_archive_root/tool"
chmod 0755 "$repair_archive_root/tool"
tar -czf "$repair_archive" -C "$repair_archive_root" tool
repair_digest="$(sha256_file "$repair_archive")"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "tool 1.2.2\n"' >"$recorded_binary"
chmod 0755 "$recorded_binary"
fetch_github_release_tag() {
  local repository="$1"
  local tag="$2"
  local output_file="$3"
  printf '%s|%s\n' "$repository" "$tag" >>"$repair_fetch_log"
  jq -n \
    --arg tag "$tag" \
    --arg digest "sha256:$repair_digest" \
    '{
      tag_name: $tag,
      assets: [{
        name: "tool-1.2.3.tar.gz",
        browser_download_url: "fixture://tool-1.2.3.tar.gz",
        digest: $digest
      }]
    }' >"$output_file"
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
  cp "$repair_archive" "$output_file"
}
repaired_state="$(
  stage_github_binary \
    example/tool tool- .tar.gz tool \
    "$recorded_binary" "$fixture/repair-stage" \
    false 1.2.3 github:example/tool@v1.2.3 \
    "$(sha256_file "$repair_archive_root/tool")"
)"
[[ "$(jq -r '.changed' <<<"$repaired_state")" == true ]]
[[ "$(jq -r '.version' <<<"$repaired_state")" == 1.2.3 ]]
[[ "$(cat "$repair_fetch_log")" == 'example/tool|v1.2.3' ]]
binary_reports_semver "$(jq -r '.staged_path' <<<"$repaired_state")" 1.2.3

fetch_github_release_tag() {
  local output_file="$3"
  jq -n \
    --arg digest "sha256:$repair_digest" \
    '{
      tag_name: "v9.9.9",
      assets: [{
        name: "tool-1.2.3.tar.gz",
        browser_download_url: "fixture://tool-1.2.3.tar.gz",
        digest: $digest
      }]
    }' >"$output_file"
}
if stage_github_binary \
    example/tool tool- .tar.gz tool \
    "$recorded_binary" "$fixture/mismatch-stage" \
    false 1.2.3 github:example/tool@v1.2.3 \
    "$(sha256_file "$repair_archive_root/tool")" \
    >"$fixture/mismatch.stdout" 2>"$fixture/mismatch.stderr"; then
  printf 'mismatched tagged release metadata was accepted\n' >&2
  exit 1
fi
rg -Fq 'recorded GitHub release identity did not match' \
  "$fixture/mismatch.stderr"

fetch_github_release_tag() {
  local output_file="$3"
  jq -n \
    '{
      tag_name: "v1.2.3",
      assets: [{
        name: "tool-1.2.3.tar.gz",
        browser_download_url: "fixture://tool-1.2.3.tar.gz",
        digest: "sha256:0000000000000000000000000000000000000000000000000000000000000000"
      }]
    }' >"$output_file"
}
if stage_github_binary \
    example/tool tool- .tar.gz tool \
    "$recorded_binary" "$fixture/checksum-stage" \
    false 1.2.3 github:example/tool@v1.2.3 \
    "$(sha256_file "$repair_archive_root/tool")" \
    >"$fixture/checksum.stdout" 2>"$fixture/checksum.stderr"; then
  printf 'wrong recorded GitHub checksum was accepted\n' >&2
  exit 1
fi
rg -Fq 'checksum mismatch for tool-1.2.3.tar.gz' \
  "$fixture/checksum.stderr"
[[ ! -e "$fixture/checksum-stage/tool" ]]

fetch_github_release_tag() {
  local output_file="$3"
  jq -n \
    --arg digest "sha256:$repair_digest" \
    '{
      tag_name: "v1.2.3",
      assets: [{
        name: "tool-1.2.3.tar.gz",
        browser_download_url: "fixture://tool-1.2.3.tar.gz",
        digest: $digest
      }]
    }' >"$output_file"
}
if stage_github_binary \
    example/tool tool- .tar.gz tool \
    "$recorded_binary" "$fixture/artifact-stage" \
    false 1.2.3 github:example/tool@v1.2.3 \
    0000000000000000000000000000000000000000000000000000000000000000 \
    >"$fixture/artifact.stdout" 2>"$fixture/artifact.stderr"; then
  printf 'wrong installed GitHub artifact hash was accepted\n' >&2
  exit 1
fi
rg -Fq 'recorded GitHub binary artifact did not match' \
  "$fixture/artifact.stderr"
unset -f fetch_github_release_tag curl

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
    bin/orichum-doctor bin/orichum-login \
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

python3 - "$ROOT/install.sh" <<'PY'
import re
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(
    r'integrations/common/mcp_probe\.py"(?P<arguments>.*?)'
    r'-- "\$mempalace_mcp"',
    source,
    flags=re.DOTALL,
)
if match is None or "--timeout 30" not in match.group("arguments"):
    raise SystemExit("Mempalace MCP probe lacks its cold-start timeout")
PY

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 13456 13457
[[ "$(read_service_ports "$ports_root")" == \
   $'18317\t13456\t13457' ]]
[[ "$(jq -r 'keys | @tsv' "$(service_ports_file "$ports_root")")" == \
   $'claudexProxyPort\tcliproxyPort\trouteProxyPort' ]]
[[ "$(path_mode "$(service_ports_file "$ports_root")")" == 600 ]]
printf '{"cliproxyPort":18318,"headroomPort":18788,"routeProxyPort":13458}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18318\t13456\t13458' ]]
printf '{"claudexProxyPort":13459,"cliproxyPort":18319,"headroomPort":18789,"routeProxyPort":13458}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18319\t13459\t13458' ]]
printf '{"cliproxyPort":18320,"headroomPort":18790}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18320\t13456\t13457' ]]
printf '{"claudexProxyPort":13460,"cliproxyPort":18321,"headroomPort":18791}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == \
   $'18321\t13456\t13460' ]]
if write_service_ports "$ports_root" 18317 18317 13457; then
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
  "$effective" "$fixture/claudex.toml" 18317 13456 13457
rg -Fq 'proxy_port = 13456' "$fixture/claudex.toml"
rg -Fq 'base_url = "http://127.0.0.1:13457"' "$fixture/claudex.toml"
if rg -qi 'Headroom|X-Headroom-Base-Url' "$fixture/claudex.toml"; then
  printf 'rendered Claudex config still routes through Headroom\n' >&2
  exit 1
fi
rg -Fq 'X-Orichum-Session-ID = "unbound"' "$fixture/claudex.toml"

data_root="$fixture/data"
install -d -m 0700 \
  "$data_root/bin" "$data_root/state" "$data_root/logs"
touch "$data_root/bin/cli-proxy-api" "$data_root/bin/orichum-route-proxy"
chmod 0755 "$data_root/bin/cli-proxy-api" \
  "$data_root/bin/orichum-route-proxy"
render_launch_agent "$fixture/cliproxy.plist" "$data_root"
render_claudex_proxy_launch_agent \
  "$fixture/route.plist" "$data_root" "$ROOT" 13457 18317 \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cliproxy_service_is_owned "$fixture/cliproxy.plist" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.plist" "$data_root" "$ROOT"
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
render_systemd_user_unit "$fixture/cliproxy.service" "$data_root"
render_claudex_proxy_systemd_user_unit \
  "$fixture/route.service" "$data_root" "$ROOT" 13457 18317 \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cliproxy_service_is_owned "$fixture/cliproxy.service" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.service" "$data_root" "$ROOT"
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

write_legacy_headroom_launchd_fixture() {
  local output_file="$1"
  local fixture_data_root="$2"
  local label="$3"
  python3 - "$output_file" "$fixture_data_root" "$label" <<'PY'
import plistlib
import sys
from pathlib import Path

output = Path(sys.argv[1])
data_root = sys.argv[2]
label = sys.argv[3]
output.write_bytes(
    plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [
                f"{data_root}/headroom/bin/headroom",
                "proxy",
                "--host",
                "127.0.0.1",
                "--port",
                "18787",
                "--anthropic-api-url",
                "http://127.0.0.1:13457",
                "--mode",
                "token",
                "--no-cache",
                "--intercept-tool-results",
                "--lossless",
                "--code-aware",
                "--disable-kompress",
            ],
            "EnvironmentVariables": {
                "HEADROOM_CONFIG_DIR": f"{data_root}/headroom/config",
                "HEADROOM_WORKSPACE_DIR": f"{data_root}/headroom/state",
            },
        }
    )
)
PY
}

write_legacy_headroom_systemd_fixture() {
  local output_file="$1"
  local fixture_data_root="$2"
  local description="$3"
  printf '%s\n' \
    '[Unit]' \
    "Description=$description" \
    '[Service]' \
    "ExecStart=\"$fixture_data_root/headroom/bin/headroom\" proxy --host 127.0.0.1 --port 18787 --anthropic-api-url http://127.0.0.1:13457 --mode token --no-cache --intercept-tool-results --lossless --code-aware --disable-kompress" \
    "Environment=\"HEADROOM_CONFIG_DIR=$fixture_data_root/headroom/config\"" \
    "Environment=\"HEADROOM_WORKSPACE_DIR=$fixture_data_root/headroom/state\"" \
    >"$output_file"
}

cleanup_command_log="$fixture/headroom-cleanup.commands"
: >"$cleanup_command_log"
launchctl() {
  printf 'launchctl\t%s\n' "$*" >>"$cleanup_command_log"
  if [[ "${1:-}" == print ]]; then
    local label="${2##*/}"
    local managed_path="$HOME/Library/LaunchAgents/$label.plist"
    local service_path="$managed_path"
    local identity_path="${FAKE_LAUNCHD_IDENTITY_FILE:-$managed_path}"
    if [[ "${FAKE_LAUNCHD_LABEL:-}" == "$label" ]]; then
      service_path="$FAKE_LAUNCHD_LOADED_PATH"
    elif [[ ! -f "$managed_path" ]]; then
      return 113
    fi
    printf 'path = %s\n' "$service_path"
    python3 - "$identity_path" <<'PY'
import plistlib
import sys
from pathlib import Path

document = plistlib.loads(Path(sys.argv[1]).read_bytes())
arguments = document["ProgramArguments"]
print(f"program = {arguments[0]}")
print("arguments = {")
for argument in arguments:
    print(f"\t{argument}")
print("}")
print("environment = {")
for key, value in document["EnvironmentVariables"].items():
    print(f"\t{key} => {value}")
print("}")
PY
  fi
}
systemctl() {
  printf 'systemctl\t%s\n' "$*" >>"$cleanup_command_log"
  if [[ "${1:-}" == --user && "${2:-}" == show ]]; then
    local property="${4:-}"
    local unit="${6:-}"
    local managed_path="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
    local service_path="$managed_path"
    local identity_path="${FAKE_SYSTEMD_IDENTITY_FILE:-$managed_path}"
    if [[ "${FAKE_SYSTEMD_UNIT:-}" == "$unit" ]]; then
      service_path="$FAKE_SYSTEMD_LOADED_PATH"
    elif [[ ! -f "$managed_path" ]]; then
      if [[ "$property" == LoadState ]]; then
        printf 'not-found\n'
        return 0
      fi
      return 1
    fi
    case "$property" in
      LoadState) printf 'loaded\n' ;;
      FragmentPath) printf '%s\n' "$service_path" ;;
      ExecStart)
        printf '{ path=fake ; argv[]=%s ; ignore_errors=no ; start_time=[] ; stop_time=[] ; pid=0 ; code=(null) ; status=0 }\n' \
          "$(sed -n 's/^ExecStart=//p' "$identity_path")"
        ;;
      Environment)
        sed -n 's/^Environment=//p' "$identity_path" | tr '\n' ' '
        printf '\n'
        ;;
      *) return 1 ;;
    esac
  fi
}

for launchd_case in \
    'io.orichum.headroom:new' \
    'com.user.claudex-headroom:legacy' \
    'com.user.headroom-proxy:legacy'; do
  IFS=: read -r service_label ownership_mode <<<"$launchd_case"
  cleanup_root="$fixture/cleanup-launchd-${service_label//./-}"
  cleanup_data="$cleanup_root/data"
  cleanup_home="$cleanup_root/home"
  cleanup_service="$cleanup_home/Library/LaunchAgents/$service_label.plist"
  unrelated_service="$cleanup_root/standalone.plist"
  install -d -m 0700 \
    "$cleanup_data/headroom/bin" "$(dirname "$cleanup_service")"
  write_legacy_headroom_launchd_fixture \
    "$cleanup_service" "$cleanup_data" "$service_label"
  cp "$cleanup_service" "$unrelated_service"
  HOME="$cleanup_home" remove_owned_headroom_installation \
    darwin "$cleanup_data" "$cleanup_service" "$service_label" - \
    "$ownership_mode"
  [[ ! -e "$cleanup_service" && ! -e "$cleanup_data/headroom" ]]
  [[ -f "$unrelated_service" ]]
done
[[ "$(rg -c '^launchctl[[:space:]]+bootout[[:space:]]' \
  "$cleanup_command_log")" == 3 ]]

for systemd_case in \
    'orichum-headroom.service|Orichum Headroom proxy|new' \
    'claudex-headroom.service|Claudex Headroom proxy|legacy' \
    'headroom-proxy.service|Headroom proxy for Claudex|legacy'; do
  IFS='|' read -r service_unit description ownership_mode <<<"$systemd_case"
  cleanup_root="$fixture/cleanup-systemd-${service_unit//./-}"
  cleanup_data="$cleanup_root/data"
  cleanup_home="$cleanup_root/home"
  cleanup_config="$cleanup_root/config"
  cleanup_service="$cleanup_config/systemd/user/$service_unit"
  unrelated_service="$cleanup_root/standalone-headroom.service"
  install -d -m 0700 \
    "$cleanup_data/headroom/bin" "$(dirname "$cleanup_service")"
  write_legacy_headroom_systemd_fixture \
    "$cleanup_service" "$cleanup_data" "$description"
  cp "$cleanup_service" "$unrelated_service"
  HOME="$cleanup_home" XDG_CONFIG_HOME="$cleanup_config" \
    remove_owned_headroom_installation \
    systemd "$cleanup_data" "$cleanup_service" - "$service_unit" \
    "$ownership_mode"
  [[ ! -e "$cleanup_service" && ! -e "$cleanup_data/headroom" ]]
  [[ -f "$unrelated_service" ]]
done
for service_unit in \
    orichum-headroom.service \
    claudex-headroom.service \
    headroom-proxy.service; do
  rg -Fq "systemctl	--user stop $service_unit" "$cleanup_command_log"
  rg -Fq "systemctl	--user disable $service_unit" "$cleanup_command_log"
done

foreign_launchd_root="$fixture/cleanup-foreign-launchd"
foreign_launchd_data="$foreign_launchd_root/data"
foreign_launchd_home="$foreign_launchd_root/home"
foreign_launchd_service="$foreign_launchd_home/Library/LaunchAgents/io.orichum.headroom.plist"
install -d -m 0700 \
  "$foreign_launchd_data/headroom/bin" \
  "$(dirname "$foreign_launchd_service")"
write_legacy_headroom_launchd_fixture \
  "$foreign_launchd_service" "$foreign_launchd_data" io.orichum.headroom
python3 - "$foreign_launchd_service" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
document = plistlib.loads(path.read_bytes())
document["ProgramArguments"][3] = "127.0.0.2"
path.write_bytes(plistlib.dumps(document))
PY
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$foreign_launchd_home" remove_owned_headroom_installation \
    darwin "$foreign_launchd_data" "$foreign_launchd_service" \
    io.orichum.headroom - new \
    2>"$foreign_launchd_root/removal.stderr"; then
  printf 'foreign launchd Headroom service at managed path was removed\n' >&2
  exit 1
fi
[[ -f "$foreign_launchd_service" && \
   -d "$foreign_launchd_data/headroom" ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

foreign_root="$fixture/cleanup-foreign"
foreign_data="$foreign_root/data"
foreign_home="$foreign_root/home"
foreign_config="$foreign_root/config"
foreign_service="$foreign_config/systemd/user/orichum-headroom.service"
install -d -m 0700 \
  "$foreign_data/headroom/bin" "$(dirname "$foreign_service")"
write_legacy_headroom_systemd_fixture \
  "$foreign_service" "$foreign_data" 'Orichum Headroom proxy'
sed 's#http://127.0.0.1:13457#http://127.0.0.2:13457#' \
  "$foreign_service" >"$foreign_service.tmp"
mv "$foreign_service.tmp" "$foreign_service"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$foreign_home" XDG_CONFIG_HOME="$foreign_config" \
  remove_owned_headroom_installation \
    systemd "$foreign_data" "$foreign_service" - \
    orichum-headroom.service new \
    2>"$foreign_root/removal.stderr"; then
  printf 'foreign Headroom service at managed path was removed\n' >&2
  exit 1
fi
[[ -f "$foreign_service" && -d "$foreign_data/headroom" ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]
rg -Fq 'refusing to remove unknown service file' \
  "$foreign_root/removal.stderr"

loaded_foreign_launchd_root="$fixture/cleanup-loaded-foreign-launchd"
loaded_foreign_launchd_data="$loaded_foreign_launchd_root/data"
loaded_foreign_launchd_home="$loaded_foreign_launchd_root/home"
loaded_foreign_launchd_service="$loaded_foreign_launchd_home/Library/LaunchAgents/io.orichum.headroom.plist"
install -d -m 0700 \
  "$loaded_foreign_launchd_data/headroom/bin" \
  "$(dirname "$loaded_foreign_launchd_service")"
write_legacy_headroom_launchd_fixture \
  "$loaded_foreign_launchd_service" "$loaded_foreign_launchd_data" \
  io.orichum.headroom
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$loaded_foreign_launchd_home" \
  FAKE_LAUNCHD_LABEL=io.orichum.headroom \
  FAKE_LAUNCHD_LOADED_PATH="$loaded_foreign_launchd_root/foreign.plist" \
  remove_owned_headroom_installation \
    darwin "$loaded_foreign_launchd_data" "$loaded_foreign_launchd_service" \
    io.orichum.headroom - new \
    2>"$loaded_foreign_launchd_root/removal.stderr"; then
  printf 'owned launchd file removed while label loaded a foreign target\n' >&2
  exit 1
fi
[[ -f "$loaded_foreign_launchd_service" && \
   -d "$loaded_foreign_launchd_data/headroom" ]]
if tail -n "+$((commands_before + 1))" "$cleanup_command_log" | \
    rg -q '^launchctl[[:space:]]+bootout[[:space:]]'; then
  printf 'foreign loaded launchd target was stopped\n' >&2
  exit 1
fi

loaded_foreign_systemd_root="$fixture/cleanup-loaded-foreign-systemd"
loaded_foreign_systemd_data="$loaded_foreign_systemd_root/data"
loaded_foreign_systemd_home="$loaded_foreign_systemd_root/home"
loaded_foreign_systemd_config="$loaded_foreign_systemd_root/config"
loaded_foreign_systemd_service="$loaded_foreign_systemd_config/systemd/user/orichum-headroom.service"
install -d -m 0700 \
  "$loaded_foreign_systemd_data/headroom/bin" \
  "$(dirname "$loaded_foreign_systemd_service")"
write_legacy_headroom_systemd_fixture \
  "$loaded_foreign_systemd_service" "$loaded_foreign_systemd_data" \
  'Orichum Headroom proxy'
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$loaded_foreign_systemd_home" \
  XDG_CONFIG_HOME="$loaded_foreign_systemd_config" \
  FAKE_SYSTEMD_UNIT=orichum-headroom.service \
  FAKE_SYSTEMD_LOADED_PATH="$loaded_foreign_systemd_root/foreign.service" \
  remove_owned_headroom_installation \
    systemd "$loaded_foreign_systemd_data" \
    "$loaded_foreign_systemd_service" - orichum-headroom.service new \
    2>"$loaded_foreign_systemd_root/removal.stderr"; then
  printf 'owned systemd file removed while unit loaded a foreign target\n' >&2
  exit 1
fi
[[ -f "$loaded_foreign_systemd_service" && \
   -d "$loaded_foreign_systemd_data/headroom" ]]
if tail -n "+$((commands_before + 1))" "$cleanup_command_log" | \
    rg -q '^systemctl[[:space:]]+--user[[:space:]]+(stop|disable)[[:space:]]'; then
  printf 'foreign loaded systemd target was stopped or disabled\n' >&2
  exit 1
fi

stale_loaded_launchd_root="$fixture/cleanup-stale-loaded-launchd"
stale_loaded_launchd_data="$stale_loaded_launchd_root/data"
stale_loaded_launchd_home="$stale_loaded_launchd_root/home"
stale_loaded_launchd_service="$stale_loaded_launchd_home/Library/LaunchAgents/io.orichum.headroom.plist"
stale_loaded_launchd_identity="$stale_loaded_launchd_root/loaded.plist"
install -d -m 0700 \
  "$stale_loaded_launchd_data/headroom/bin" \
  "$(dirname "$stale_loaded_launchd_service")"
write_legacy_headroom_launchd_fixture \
  "$stale_loaded_launchd_service" "$stale_loaded_launchd_data" \
  io.orichum.headroom
cp "$stale_loaded_launchd_service" "$stale_loaded_launchd_identity"
python3 - "$stale_loaded_launchd_identity" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
document = plistlib.loads(path.read_bytes())
document["ProgramArguments"][3] = "127.0.0.2"
path.write_bytes(plistlib.dumps(document))
PY
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$stale_loaded_launchd_home" \
  FAKE_LAUNCHD_IDENTITY_FILE="$stale_loaded_launchd_identity" \
  remove_owned_headroom_installation \
    darwin "$stale_loaded_launchd_data" "$stale_loaded_launchd_service" \
    io.orichum.headroom - new \
    2>"$stale_loaded_launchd_root/removal.stderr"; then
  printf 'stale foreign launchd identity was stopped from a replaced file\n' >&2
  exit 1
fi
[[ -f "$stale_loaded_launchd_service" && \
   -d "$stale_loaded_launchd_data/headroom" ]]
if tail -n "+$((commands_before + 1))" "$cleanup_command_log" | \
    rg -q '^launchctl[[:space:]]+bootout[[:space:]]'; then
  printf 'stale foreign launchd identity was stopped\n' >&2
  exit 1
fi

stale_loaded_systemd_root="$fixture/cleanup-stale-loaded-systemd"
stale_loaded_systemd_data="$stale_loaded_systemd_root/data"
stale_loaded_systemd_home="$stale_loaded_systemd_root/home"
stale_loaded_systemd_config="$stale_loaded_systemd_root/config"
stale_loaded_systemd_service="$stale_loaded_systemd_config/systemd/user/orichum-headroom.service"
stale_loaded_systemd_identity="$stale_loaded_systemd_root/loaded.service"
install -d -m 0700 \
  "$stale_loaded_systemd_data/headroom/bin" \
  "$(dirname "$stale_loaded_systemd_service")"
write_legacy_headroom_systemd_fixture \
  "$stale_loaded_systemd_service" "$stale_loaded_systemd_data" \
  'Orichum Headroom proxy'
sed 's#--host 127.0.0.1#--host 127.0.0.2#' \
  "$stale_loaded_systemd_service" >"$stale_loaded_systemd_identity"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$stale_loaded_systemd_home" \
  XDG_CONFIG_HOME="$stale_loaded_systemd_config" \
  FAKE_SYSTEMD_IDENTITY_FILE="$stale_loaded_systemd_identity" \
  remove_owned_headroom_installation \
    systemd "$stale_loaded_systemd_data" \
    "$stale_loaded_systemd_service" - orichum-headroom.service new \
    2>"$stale_loaded_systemd_root/removal.stderr"; then
  printf 'stale foreign systemd identity was stopped from a replaced file\n' >&2
  exit 1
fi
[[ -f "$stale_loaded_systemd_service" && \
   -d "$stale_loaded_systemd_data/headroom" ]]
if tail -n "+$((commands_before + 1))" "$cleanup_command_log" | \
    rg -q '^systemctl[[:space:]]+--user[[:space:]]+(stop|disable)[[:space:]]'; then
  printf 'stale foreign systemd identity was stopped or disabled\n' >&2
  exit 1
fi

mixed_root="$fixture/cleanup-mixed"
mixed_data="$mixed_root/data"
mixed_home="$mixed_root/home"
mixed_config="$mixed_root/config"
mixed_current="$mixed_config/systemd/user/orichum-headroom.service"
mixed_foreign="$mixed_config/systemd/user/claudex-headroom.service"
install -d -m 0700 \
  "$mixed_data/headroom/bin" "$(dirname "$mixed_current")"
write_legacy_headroom_systemd_fixture \
  "$mixed_current" "$mixed_data" 'Orichum Headroom proxy'
write_legacy_headroom_systemd_fixture \
  "$mixed_foreign" "$mixed_data" 'Claudex Headroom proxy'
sed 's#http://127.0.0.1:13457#http://127.0.0.2:13457#' \
  "$mixed_foreign" >"$mixed_foreign.tmp"
mv "$mixed_foreign.tmp" "$mixed_foreign"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$mixed_home" XDG_CONFIG_HOME="$mixed_config" \
  remove_owned_headroom_installation \
    systemd "$mixed_data" "$mixed_current" - \
    orichum-headroom.service new \
    2>"$mixed_root/removal.stderr"; then
  printf 'mixed owned and foreign Headroom definitions were partially removed\n' >&2
  exit 1
fi
[[ -f "$mixed_current" && -f "$mixed_foreign" ]]
[[ -d "$mixed_data/headroom" ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

unsafe_runtime_root="$fixture/cleanup-unsafe-runtime"
unsafe_runtime_data="$unsafe_runtime_root/data"
unsafe_runtime_home="$unsafe_runtime_root/home"
unsafe_runtime_service="$unsafe_runtime_home/Library/LaunchAgents/io.orichum.headroom.plist"
install -d -m 0700 \
  "$unsafe_runtime_data" "$(dirname "$unsafe_runtime_service")"
printf 'unrelated file\n' >"$unsafe_runtime_data/headroom"
write_legacy_headroom_launchd_fixture \
  "$unsafe_runtime_service" "$unsafe_runtime_data" io.orichum.headroom
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$unsafe_runtime_home" remove_owned_headroom_installation \
    darwin "$unsafe_runtime_data" "$unsafe_runtime_service" \
    io.orichum.headroom - new \
    2>"$unsafe_runtime_root/removal.stderr"; then
  printf 'non-directory private Headroom path was removed\n' >&2
  exit 1
fi
[[ -f "$unsafe_runtime_service" && -f "$unsafe_runtime_data/headroom" ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

preflight_foreign_root="$fixture/preflight-foreign"
preflight_foreign_data="$preflight_foreign_root/data"
preflight_foreign_home="$preflight_foreign_root/home"
preflight_foreign_config="$preflight_foreign_root/config"
preflight_foreign_service="$preflight_foreign_config/systemd/user/orichum-headroom.service"
install -d -m 0700 \
  "$preflight_foreign_data/headroom/bin" \
  "$(dirname "$preflight_foreign_service")"
write_legacy_headroom_systemd_fixture \
  "$preflight_foreign_service" "$preflight_foreign_data" \
  'Orichum Headroom proxy'
sed 's#--host 127.0.0.1#--host 127.0.0.2#' \
  "$preflight_foreign_service" >"$preflight_foreign_service.tmp"
mv "$preflight_foreign_service.tmp" "$preflight_foreign_service"
preflight_foreign_hash="$(
  sha256_file "$preflight_foreign_service"
)"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$preflight_foreign_home" \
  XDG_CONFIG_HOME="$preflight_foreign_config" \
  preflight_owned_headroom_installation \
    systemd "$preflight_foreign_data" \
    2>"$preflight_foreign_root/preflight.stderr"; then
  printf 'foreign Headroom state passed the read-only preflight\n' >&2
  exit 1
fi
[[ "$(sha256_file "$preflight_foreign_service")" == \
   "$preflight_foreign_hash" ]]
[[ -d "$preflight_foreign_data/headroom" ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

preflight_unsafe_root="$fixture/preflight-unsafe-runtime"
preflight_unsafe_data="$preflight_unsafe_root/data"
preflight_unsafe_home="$preflight_unsafe_root/home"
install -d -m 0700 "$preflight_unsafe_data"
printf 'foreign runtime\n' >"$preflight_unsafe_data/headroom"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$preflight_unsafe_home" preflight_owned_headroom_installation \
    darwin "$preflight_unsafe_data" \
    2>"$preflight_unsafe_root/preflight.stderr"; then
  printf 'unsafe private runtime passed the read-only preflight\n' >&2
  exit 1
fi
[[ "$(<"$preflight_unsafe_data/headroom")" == 'foreign runtime' ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

preflight_unsafe_tools_root="$fixture/preflight-unsafe-tools"
preflight_unsafe_tools_data="$preflight_unsafe_tools_root/data"
preflight_unsafe_tools_home="$preflight_unsafe_tools_root/home"
preflight_unsafe_tools_external="$preflight_unsafe_tools_root/external"
install -d -m 0700 \
  "$preflight_unsafe_tools_data/headroom/bin" \
  "$preflight_unsafe_tools_external"
printf 'external\n' >"$preflight_unsafe_tools_external/marker"
ln -s "$preflight_unsafe_tools_external" \
  "$preflight_unsafe_tools_data/headroom/tools"
commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
if HOME="$preflight_unsafe_tools_home" \
  preflight_owned_headroom_installation \
    darwin "$preflight_unsafe_tools_data" \
    2>"$preflight_unsafe_tools_root/preflight.stderr"; then
  printf 'unsafe legacy private tool subtree passed early preflight\n' >&2
  exit 1
fi
[[ -L "$preflight_unsafe_tools_data/headroom/tools" ]]
[[ "$(<"$preflight_unsafe_tools_external/marker")" == external ]]
[[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]

orphan_runtime_root="$fixture/cleanup-orphan-runtime"
orphan_runtime_data="$orphan_runtime_root/data"
orphan_runtime_home="$orphan_runtime_root/home"
orphan_runtime_service="$orphan_runtime_home/Library/LaunchAgents/io.orichum.headroom.plist"
install -d -m 0700 "$orphan_runtime_data/headroom/bin"
HOME="$orphan_runtime_home" remove_owned_headroom_installation \
  darwin "$orphan_runtime_data" "$orphan_runtime_service" \
  io.orichum.headroom - new
[[ ! -e "$orphan_runtime_data/headroom" && \
   ! -L "$orphan_runtime_data/headroom" ]]

for unsafe_orphan_kind in regular symlink; do
  unsafe_orphan_root="$fixture/cleanup-unsafe-orphan-$unsafe_orphan_kind"
  unsafe_orphan_data="$unsafe_orphan_root/data"
  unsafe_orphan_home="$unsafe_orphan_root/home"
  unsafe_orphan_service="$unsafe_orphan_home/Library/LaunchAgents/io.orichum.headroom.plist"
  install -d -m 0700 "$unsafe_orphan_data"
  case "$unsafe_orphan_kind" in
    regular)
      printf 'foreign runtime\n' >"$unsafe_orphan_data/headroom"
      ;;
    symlink)
      install -d -m 0700 "$unsafe_orphan_root/external"
      printf 'external\n' >"$unsafe_orphan_root/external/marker"
      ln -s "$unsafe_orphan_root/external" "$unsafe_orphan_data/headroom"
      ;;
  esac
  commands_before="$(wc -l <"$cleanup_command_log" | tr -d ' ')"
  if HOME="$unsafe_orphan_home" remove_owned_headroom_installation \
      darwin "$unsafe_orphan_data" "$unsafe_orphan_service" \
      io.orichum.headroom - new \
      2>"$unsafe_orphan_root/removal.stderr"; then
    printf '%s orphan Headroom runtime was accepted\n' \
      "$unsafe_orphan_kind" >&2
    exit 1
  fi
  [[ -e "$unsafe_orphan_data/headroom" || \
     -L "$unsafe_orphan_data/headroom" ]]
  if [[ "$unsafe_orphan_kind" == symlink ]]; then
    [[ "$(<"$unsafe_orphan_root/external/marker")" == external ]]
  fi
  [[ "$(wc -l <"$cleanup_command_log" | tr -d ' ')" == "$commands_before" ]]
done
unset -f launchctl systemctl

python3 - "$ROOT/install.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
preflight = source.index(
    'preflight_owned_headroom_installation \\\n'
    '  "$platform" "$WORKFLOW_DATA_ROOT"'
)
first_data_write = source.index(
    'install -d -m 0700 \\\n'
    '  "$WORKFLOW_DATA_ROOT" "$WORKFLOW_DATA_ROOT/state"'
)
management_key = source.index(
    'management_key_file="$WORKFLOW_DATA_ROOT/cliproxy-management.key"'
)
route_link = source.index(
    'ln -sfn "$WORKFLOW_ROOT/bin/orichum-route-proxy"'
)
model_migration = source.index(
    'migrate_legacy_model_config "$WORKFLOW_DATA_ROOT"'
)
tool_upgrade = source.index("uv tool install --upgrade mempalace")
if not (
    preflight
    < first_data_write
    < management_key
    < route_link
    < model_migration
    < tool_upgrade
):
    raise SystemExit(
        "Headroom safety preflight does not precede persistent installer writes"
    )
PY

rg -Fq 'for launcher in orichum' "$ROOT/install.sh"
if rg -q 'for launcher in .*claudex-gpt' "$ROOT/install.sh"; then
  printf 'legacy launchers are still installed\n' >&2
  exit 1
fi
rg -Fq 'ORICHUM_ROUTE_PROXY_PORT' "$ROOT/install.sh"
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
