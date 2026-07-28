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
lifecycle_lock_path="$fixture/install.lock"

path_home="$fixture/path-home"
install -d -m 0700 "$path_home"
(
  export HOME="$path_home"
  unset ORICHUM_HOME ORICHUM_DATA_HOME ORICHUM_CONFIG_HOME ORICHUM_CACHE_HOME
  [[ "$(orichum_home_dir)" == "$path_home/.orichum" ]]
  [[ "$(workflow_data_dir)" == "$path_home/.orichum" ]]
  [[ "$(workflow_config_dir)" == "$path_home/.orichum/config" ]]
  [[ "$(workflow_cache_dir)" == "$path_home/.orichum/cache" ]]
)
(
  export HOME="$path_home"
  export ORICHUM_HOME="$fixture/custom-home"
  unset ORICHUM_DATA_HOME ORICHUM_CONFIG_HOME ORICHUM_CACHE_HOME
  [[ "$(workflow_data_dir)" == "$fixture/custom-home" ]]
  [[ "$(workflow_config_dir)" == "$fixture/custom-home/config" ]]
  [[ "$(workflow_cache_dir)" == "$fixture/custom-home/cache" ]]
)

[[ "$(parse_install_mode)" == fast ]]
[[ "$(parse_install_mode --upgrade)" == upgrade ]]
[[ "$(parse_install_mode --uninstall)" == uninstall ]]
[[ "$(parse_install_mode --uninstall --purge)" == purge ]]
if parse_install_mode --purge >/dev/null 2>&1; then
  printf 'standalone --purge was accepted\n' >&2
  exit 1
fi

workflow_cleanup_init
mode_lock="$fixture/mode-lock"
acquire_workflow_lock "$mode_lock"
[[ "$(path_mode "$mode_lock")" == 700 ]]
release_workflow_lock "$mode_lock"
exec 9<"$fixture/install.lock"
WORKFLOW_LOCK_FD=9

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
component_table="$(
  print_component_status_table \
    reused repaired upgraded reused repaired repaired
)"
rg -Fq 'CLIProxyAPI           repaired' <<<"$component_table"
rg -Fq 'Controller plugin     repaired' <<<"$component_table"
if print_component_status_table \
    invalid reused reused reused reused reused >/dev/null; then
  printf 'invalid component status was accepted\n' >&2
  exit 1
fi

routing_fixture="$fixture/routing-fingerprint"
routing_data="$routing_fixture/data"
routing_config="$routing_fixture/config"
routing_generation="$routing_data/model-config/generation.test"
routing_descriptor="$routing_fixture/runtime.descriptor"
install -d -m 0700 \
  "$routing_data/claude-config" "$routing_generation" "$routing_config"
printf '%s\n' cliproxy.yaml >"$routing_data/cliproxy.yaml"
for routing_file in cliproxy.service route-proxy.service; do
  printf '%s\n' "$routing_file" >"$routing_fixture/$routing_file"
done
for routing_file in \
    claudex.toml models.json effective-models.json; do
  printf '%s\n' "$routing_file" >"$routing_generation/$routing_file"
done
for routing_file in \
    accounts.json model-stacks.json plugins.json projects.json \
    providers.json runtime.json controller-policy.md; do
  printf '%s\n' "$routing_file" >"$routing_config/$routing_file"
done
printf '{}\n' >"$routing_data/claude-config/settings.json"
ln -s generation.test "$routing_data/model-config/current"
routing_artifact="$(
  verified_routing_runtime_artifact \
    "$routing_data" "$routing_config" \
    "$routing_fixture/cliproxy.service" \
    "$routing_fixture/route-proxy.service" \
    "$routing_descriptor"
)"
[[ "$routing_artifact" =~ ^[a-f0-9]{64}$ ]]
printf 'changed\n' >>"$routing_config/projects.json"
changed_routing_artifact="$(
  verified_routing_runtime_artifact \
    "$routing_data" "$routing_config" \
    "$routing_fixture/cliproxy.service" \
    "$routing_fixture/route-proxy.service" \
    "$routing_descriptor"
)"
[[ "$changed_routing_artifact" != "$routing_artifact" ]]
routing_source="$routing_fixture/source-runtime.json"
printf '{"source":"v1"}\n' >"$routing_source"
routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 \
    "$routing_config/projects.json" "$routing_source" \
    "$routing_data/cliproxy.yaml"
)"
same_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-copy.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 \
    "$routing_config/projects.json" "$routing_source" \
    "$routing_data/cliproxy.yaml"
)"
[[ "$routing_input" == "$same_routing_input" ]]
changed_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-changed.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf changed-route | sha256_text)" \
    8317 13457 13456 \
    "$routing_config/projects.json" "$routing_source" \
    "$routing_data/cliproxy.yaml"
)"
[[ "$changed_routing_input" != "$routing_input" ]]
printf '{"source":"v2"}\n' >"$routing_source"
source_changed_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-source-changed.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 \
    "$routing_config/projects.json" "$routing_source" \
    "$routing_data/cliproxy.yaml"
)"
[[ "$source_changed_routing_input" != "$routing_input" ]]

candidate_models="$routing_fixture/candidate-models.json"
committed_models="$routing_fixture/committed-models.json"
printf '{"models":{"demo":{"priority":1}}}\n' >"$candidate_models"
printf '{\n  "models": {\n    "demo": {\n      "priority": 1\n    }\n  }\n}\n' >"$committed_models"
precommit_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-precommit.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 "$candidate_models"
)"
committed_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-committed.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 "$committed_models"
)"
fast_routing_input="$(
  verified_routing_input_fingerprint \
    "$routing_fixture/input-fast.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 "$committed_models"
)"
[[ "$precommit_routing_input" != "$committed_routing_input" ]]
[[ "$committed_routing_input" == "$fast_routing_input" ]]
missing_routing_error="$routing_fixture/missing-routing.stderr"
if verified_routing_input_fingerprint \
    "$routing_fixture/input-missing.descriptor" \
    "$(printf clip | sha256_text)" "$(printf claudex | sha256_text)" \
    "$(printf route | sha256_text)" \
    8317 13457 13456 "$routing_fixture/absent.service" \
    2>"$missing_routing_error"; then
  printf 'missing routing input unexpectedly fingerprinted\n' >&2
  exit 1
fi
[[ ! -s "$missing_routing_error" ]]

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

plugin_fixture="$fixture/plugin-fingerprint"
install -d -m 0700 \
  "$plugin_fixture/controller/plugin/hooks" "$plugin_fixture/config"
printf '{"hooks":{}}\n' \
  >"$plugin_fixture/controller/plugin/hooks/hooks.json"
printf '{"plugins":[]}\n' >"$plugin_fixture/config/plugins.json"
plugin_fingerprint="$(
  controller_plugin_fingerprint "$ROOT" "$plugin_fixture" python3
)"
[[ "$plugin_fingerprint" =~ ^[a-f0-9]{64}$ ]]
printf 'untracked runtime content\n' \
  >"$plugin_fixture/controller/plugin/untracked.txt"
plugin_fingerprint_with_untracked="$(
  controller_plugin_fingerprint "$ROOT" "$plugin_fixture" python3
)"
[[ "$plugin_fingerprint_with_untracked" != "$plugin_fingerprint" ]]
ln -s hooks/hooks.json \
  "$plugin_fixture/controller/plugin/unsafe-link"
if controller_plugin_fingerprint \
    "$ROOT" "$plugin_fixture" python3 >/dev/null 2>&1; then
  printf 'symlinked controller plugin content was accepted\n' >&2
  exit 1
fi

rg -Fq \
  'Use `ctx_shell` for observational commands whose output may be noisy' \
  "$ROOT/config/controller-policy.md"
rg -Fq \
  'Use `ctx_shell(raw=true)` when exact diagnostic output is required' \
  "$ROOT/config/controller-policy.md"
rg -Fq \
  'Do not run the same command through both shell paths' \
  "$ROOT/config/controller-policy.md"
rg -Fq \
  'Git commit/push/branch' \
  "$ROOT/config/controller-policy.md"
rg -Fq \
  'status/diff/log' \
  "$ROOT/controller/plugin/agents/implementation-worker.md"

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
config = Path(os.environ["LEAN_CTX_CONFIG_DIR"])
state = Path(os.environ["LEAN_CTX_STATE_DIR"])
cache = Path(os.environ["LEAN_CTX_CACHE_DIR"])
xdg = Path(os.environ["XDG_DATA_HOME"])
if (
    not root.is_dir()
    or not (config / "config.toml").is_file()
    or not state.is_dir()
    or not cache.is_dir()
    or data != xdg / "lean-ctx"
):
    raise SystemExit(4)
tools = [
    "ctx_read",
    "ctx_search",
    "ctx_tree",
    "ctx_expand",
    "ctx_graph",
    "ctx_impact",
    "ctx_callgraph",
    "ctx_knowledge",
    "ctx_overview",
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
    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        call_log = os.environ.get("FAKE_LEANCTX_CALL_LOG")
        if call_log:
            with Path(call_log).open("a", encoding="utf-8") as stream:
                stream.write(f"{name}\n")
        if name == "ctx_graph" and arguments.get("action") == "build":
            text = "Project Graph: 1 files"
        elif name == "ctx_graph" and arguments.get("action") == "symbol":
            text = "probe.py::orichum_probe_target"
        elif name == "ctx_impact":
            text = "No files depend on probe.py. [ctx_impact: 8 tok]"
        elif name == "ctx_shell":
            text = "orichum-shell-ready"
        else:
            text = f"{name} completed"
        result = {
            "content": [{"type": "text", "text": text}],
            "isError": False,
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
FAKE_LEANCTX_CALL_LOG="$fixture/leanctx-calls" probe_leanctx_capabilities \
  "$leanctx_probe" "$python_bin/python3.14" "$ROOT" "$fixture"
rg -Fxq 'ctx_shell' "$fixture/leanctx-calls"
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
rollback_consolidated_runtime_and_home() { return 0; }

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
  "$activation_snapshot" "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
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
  lifecycle_lock_path="$fixture/install.lock"
  ORICHUM_PYTHON="$python_bin/python3.14"
  config_transaction_active=true
  python_transaction_active=false
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
  "$activation_snapshot" "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
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
  "$fixture/v2-activation-snapshot" \
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
jq -e '.schemaVersion == 2 and .stacks.heavy' \
  "$v2_config/model-stacks.json" >/dev/null
cp "$v2_config/model-stacks.json" "$fixture/v2-first-run.saved"
cp "$v2_config/stack-bindings.json" "$fixture/v2-bindings.saved"
finalize_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" \
  "$fixture/v2-activation-snapshot" \
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
rm -rf -- "$v2_candidate"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_config" "$v2_candidate"
activate_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_candidate" "$v2_config" \
  "$fixture/v2-activation-snapshot" \
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
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
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD" &
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
  "$fixture/unlocked-activation-snapshot" \
  "$lifecycle_lock_path" "$WORKFLOW_LOCK_FD"
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
rg -Fq '(root / "integrations" / "common").glob("*.py")' \
  "$ROOT/install.sh"

runtime_root="$fixture/runtime-root"
install -d -m 0700 \
  "$runtime_root/bin" "$runtime_root/lib" \
  "$runtime_root/integrations/common" \
  "$runtime_root/controller" "$runtime_root/config"
cp "$ROOT/VERSION" "$runtime_root/VERSION"
cp "$ROOT/bin/orichum" "$ROOT/bin/orichum-route-proxy" \
  "$ROOT/bin/orichum-statusline" \
  "$runtime_root/bin/"
cp "$ROOT/lib/workflow.sh" "$runtime_root/lib/workflow.sh"
cp "$ROOT/integrations/common/"*.py "$runtime_root/integrations/common/"
cp -R "$ROOT/controller/plugin" "$runtime_root/controller/plugin"
cp "$ROOT/controller/settings.json" "$runtime_root/controller/settings.json"
cp "$ROOT/config/plugins.json" "$runtime_root/config/plugins.json"
runtime_python="$python_bin/python3.14"
runtime_python_version=3.14.6
first_runtime_digest="$(
  verified_route_runtime_digest \
    "$runtime_root" "$runtime_python" "$runtime_python_version" \
    "$fixture/runtime-first"
)"
printf '\n' >>"$runtime_root/controller/plugin/plugin.json"
second_runtime_digest="$(
  verified_route_runtime_digest \
    "$runtime_root" "$runtime_python" "$runtime_python_version" \
    "$fixture/runtime-second"
)"
[[ "$first_runtime_digest" != "$second_runtime_digest" ]] || {
  printf 'controller plugin changes do not change runtime identity\n' >&2
  exit 1
}
printf '\n' >>"$runtime_root/bin/orichum"
third_runtime_digest="$(
  verified_route_runtime_digest \
    "$runtime_root" "$runtime_python" "$runtime_python_version" \
    "$fixture/runtime-third"
)"
[[ "$second_runtime_digest" != "$third_runtime_digest" ]] || {
  printf 'launcher changes do not change runtime identity\n' >&2
  exit 1
}
printf '\n' >>"$runtime_root/bin/orichum-statusline"
fourth_runtime_digest="$(
  verified_route_runtime_digest \
    "$runtime_root" "$runtime_python" "$runtime_python_version" \
    "$fixture/runtime-fourth"
)"
[[ "$third_runtime_digest" != "$fourth_runtime_digest" ]] || {
  printf 'status-line changes do not change runtime identity\n' >&2
  exit 1
}

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 13456 13457
[[ "$(read_service_ports "$ports_root")" == \
   $'18317\t13456\t13457' ]]
[[ "$(jq -r 'keys | @tsv' "$(service_ports_file "$ports_root")")" == \
   $'claudexProxyPort\tcliproxyPort\trouteProxyPort' ]]
[[ "$(path_mode "$(service_ports_file "$ports_root")")" == 600 ]]
printf '{"cliproxyPort":18318,"routeProxyPort":13458}\n' \
  >"$(service_ports_file "$ports_root")"
if read_service_ports "$ports_root" >/dev/null 2>&1; then
  printf 'incomplete service port state was accepted\n' >&2
  exit 1
fi
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
cp "$fixture/route.plist" "$fixture/previous-route.plist"
"$python_bin/python3.14" - "$fixture/previous-route.plist" <<'PY'
import plistlib
from pathlib import Path
import sys

path = Path(sys.argv[1])
document = plistlib.loads(path.read_bytes())
del document["EnvironmentVariables"]["ORICHUM_DATA_HOME"]
path.write_bytes(plistlib.dumps(document))
PY
claudex_proxy_service_is_owned \
  "$fixture/previous-route.plist" "$data_root" "$ROOT"
rg -Fq '<string>io.orichum.cliproxy</string>' "$fixture/cliproxy.plist"
rg -Fq '<string>io.orichum.route-proxy</string>' "$fixture/route.plist"
rg -Fq 'Orichum route runtime SHA-256: aaaaaaaaaa' "$fixture/route.plist"
rg -Fq "<string>$data_root/bin/orichum-python</string>" \
  "$fixture/route.plist"
rg -Fq '<key>ORICHUM_WORKFLOW_ROOT</key>' "$fixture/route.plist"
rg -Fq "<string>$ROOT</string>" "$fixture/route.plist"
rg -Fq '<key>ORICHUM_PYTHON</key>' "$fixture/route.plist"
rg -Fq '<key>ORICHUM_DATA_HOME</key>' "$fixture/route.plist"
rg -Fq "<string>$data_root</string>" "$fixture/route.plist"
rg -Fq '<string>-I</string>' "$fixture/route.plist"
rg -Fq '<string>-B</string>' "$fixture/route.plist"
rg -Fq '<string>--data-home</string>' "$fixture/route.plist"
[[ "$(route_service_runtime_digest "$fixture/route.plist")" == \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]]
render_systemd_user_unit "$fixture/cliproxy.service" "$data_root"
render_claudex_proxy_systemd_user_unit \
  "$fixture/route.service" "$data_root" "$ROOT" 13457 18317 \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cliproxy_service_is_owned "$fixture/cliproxy.service" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.service" "$data_root" "$ROOT"
awk '!/^Environment="ORICHUM_DATA_HOME=/' \
  "$fixture/route.service" >"$fixture/previous-route.service"
claudex_proxy_service_is_owned \
  "$fixture/previous-route.service" "$data_root" "$ROOT"
rg -Fq 'Description=Orichum same-family recovery proxy' \
  "$fixture/route.service"
rg -Fq 'Orichum route runtime SHA-256: aaaaaaaaaa' \
  "$fixture/route.service"
rg -Fq "$data_root/bin/orichum-python" "$fixture/route.service"
rg -Fq "Environment=\"ORICHUM_WORKFLOW_ROOT=$ROOT\"" \
  "$fixture/route.service"
rg -Fq "Environment=\"ORICHUM_PYTHON=$data_root/bin/orichum-python\"" \
  "$fixture/route.service"
rg -Fq "Environment=\"ORICHUM_DATA_HOME=$data_root\"" \
  "$fixture/route.service"
[[ "$(route_service_runtime_digest "$fixture/route.service")" == \
  aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ]]
rg -Fq 'Wants=orichum-cliproxy.service' "$fixture/route.service"
rg -Fq 'resolve_orichum_python' "$ROOT/bin/orichum-route-proxy"

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
