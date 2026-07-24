#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-installer-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

printf '6.8.0-generic\n' >"$fixture/linux-osrelease"
printf '4.4.0-Microsoft\n' >"$fixture/wsl1-osrelease"
printf '5.15.153.1-microsoft-standard-WSL2\n' >"$fixture/wsl2-osrelease"
[[ "$(linux_environment_kind "$fixture/linux-osrelease")" == linux ]]
[[ "$(linux_environment_kind "$fixture/wsl1-osrelease")" == wsl1 ]]
[[ "$(linux_environment_kind "$fixture/wsl2-osrelease")" == wsl2 ]]

for script in \
    install.sh lib/workflow.sh bin/orichum bin/orichum-context \
    bin/orichum-doctor bin/orichum-headroom bin/orichum-login \
    bin/orichum-plugin bin/orichum-route-proxy \
    bin/orichum-runtime-ready bin/orichum-verify-cliproxy; do
  bash -n "$ROOT/$script"
done

rg -Fq 'export PATH="$UV_TOOL_BIN_DIR:$HOME/.local/bin:$PATH"' \
  "$ROOT/install.sh"

ports_root="$fixture/ports"
write_service_ports "$ports_root" 18317 18787 13457
[[ "$(read_service_ports "$ports_root")" == $'18317\t18787\t13457' ]]
[[ "$(jq -r 'keys | @tsv' "$(service_ports_file "$ports_root")")" == \
   $'cliproxyPort\theadroomPort\trouteProxyPort' ]]
[[ "$(path_mode "$(service_ports_file "$ports_root")")" == 600 ]]
printf '{"claudexProxyPort":13458,"cliproxyPort":18318,"headroomPort":18788}\n' \
  >"$(service_ports_file "$ports_root")"
[[ "$(read_service_ports "$ports_root")" == $'18318\t18788\t13458' ]]
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
  "$effective" "$fixture/claudex.toml" 18317 18787 13457
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
  "$fixture/route.plist" "$data_root" 13457 18317
render_headroom_launch_agent \
  "$fixture/headroom.plist" "$data_root" "$headroom" \
  "$fixture/ca.pem" 18787 13457
cliproxy_service_is_owned "$fixture/cliproxy.plist" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.plist" "$data_root"
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
rg -Fq '<string>--data-home</string>' "$fixture/route.plist"
rg -Fq '<string>io.orichum.headroom</string>' "$fixture/headroom.plist"
rg -Fq '<string>--anthropic-api-url</string>' "$fixture/headroom.plist"
rg -Fq '<string>http://127.0.0.1:13457</string>' \
  "$fixture/headroom.plist"

render_systemd_user_unit "$fixture/cliproxy.service" "$data_root"
render_claudex_proxy_systemd_user_unit \
  "$fixture/route.service" "$data_root" 13457 18317
render_headroom_systemd_user_unit \
  "$fixture/headroom.service" "$data_root" "$headroom" \
  "$fixture/ca.pem" 18787 13457
cliproxy_service_is_owned "$fixture/cliproxy.service" "$data_root"
claudex_proxy_service_is_owned "$fixture/route.service" "$data_root"
headroom_service_is_owned "$fixture/headroom.service" "$data_root" new
sed \
  's/Description=Orichum Headroom proxy/Description=Claudex Headroom proxy/' \
  "$fixture/headroom.service" >"$fixture/previous-headroom.service"
headroom_service_is_owned \
  "$fixture/previous-headroom.service" "$data_root" legacy
rg -Fq 'Description=Orichum same-family recovery proxy' \
  "$fixture/route.service"
rg -Fq 'Wants=orichum-cliproxy.service' "$fixture/route.service"
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
rg -Fq 'io.orichum.route-proxy' "$ROOT/lib/workflow.sh"
if rg -Fq 'home=Path.home()' "$ROOT/install.sh"; then
  printf 'installer uses obsolete load_control_plane home argument\n' >&2
  exit 1
fi

printf 'installer contract tests passed\n'
