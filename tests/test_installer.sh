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
trap 'rm -rf -- "$fixture"' EXIT

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

v1_config="$fixture/v1-config"
v1_candidate="$fixture/v1-candidate"
install -d -m 0700 "$v1_config"
for control_file in \
    model-stacks.json projects.json providers.json plugins.json runtime.json \
    controller-policy.md; do
  install -m 0600 "$ROOT/config/$control_file" "$v1_config/$control_file"
done
printf '{"schemaVersion":2,"accounts":[]}\n' >"$v1_config/accounts.json"
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
cp "$v1_config/model-stacks.json" "$fixture/v1-model-stacks.saved"
cp "$v1_config/stack-bindings.json" "$fixture/v1-bindings.saved"
install -d -m 0700 "$fixture/v1-snapshot"
snapshot_path "$v1_config/model-stacks.json" \
  "$fixture/v1-snapshot" model-stacks
snapshot_path "$v1_config/stack-bindings.json" \
  "$fixture/v1-snapshot" stack-bindings

stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v1_config" "$v1_candidate"
if (exit 73); then
  printf 'forced post-staging failure unexpectedly succeeded\n' >&2
  exit 1
fi
cmp "$fixture/v1-model-stacks.saved" "$v1_config/model-stacks.json"
cmp "$fixture/v1-bindings.saved" "$v1_config/stack-bindings.json"
jq -e '.schemaVersion == 2 and .stacks.balanced' \
  "$v1_candidate/model-stacks.json" >/dev/null
cmp "$fixture/v1-bindings.saved" "$v1_candidate/stack-bindings.json"
activate_installed_control_plane "$v1_candidate" "$v1_config"
restore_snapshot "$v1_config/model-stacks.json" \
  "$fixture/v1-snapshot" model-stacks
restore_snapshot "$v1_config/stack-bindings.json" \
  "$fixture/v1-snapshot" stack-bindings
cmp "$fixture/v1-model-stacks.saved" "$v1_config/model-stacks.json"
cmp "$fixture/v1-bindings.saved" "$v1_config/stack-bindings.json"
activate_installed_control_plane "$v1_candidate" "$v1_config"
jq -e '.schemaVersion == 2 and .stacks.balanced' \
  "$v1_config/model-stacks.json" >/dev/null
cmp "$fixture/v1-bindings.saved" "$v1_config/stack-bindings.json"
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
activate_installed_control_plane "$v2_candidate" "$v2_config"
jq -e '.schemaVersion == 2 and .stacks.heavy' \
  "$v2_config/model-stacks.json" >/dev/null
cp "$v2_config/model-stacks.json" "$fixture/v2-first-run.saved"
cp "$v2_config/stack-bindings.json" "$fixture/v2-bindings.saved"
rm -rf -- "$v2_candidate"
stage_installed_control_plane \
  "$python_bin/python3.14" "$ROOT" "$v2_config" "$v2_candidate"
activate_installed_control_plane "$v2_candidate" "$v2_config"
cmp "$fixture/v2-first-run.saved" "$v2_config/model-stacks.json"
cmp "$fixture/v2-bindings.saved" "$v2_config/stack-bindings.json"

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
activate_installed_control_plane "$unlocked_candidate" "$unlocked_config"
[[ ! -e "$unlocked_config/stack-bindings.json" ]]

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
