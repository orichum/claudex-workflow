#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-smoke.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

ports_root="$fixture/ports"
for legacy_ports in \
  '{"cliproxyPort":8317,"headroomPort":8787,"routeProxyPort":13457}' \
  '{"claudexProxyPort":13456,"cliproxyPort":8317,"headroomPort":8787,"routeProxyPort":13457}'; do
  install -d -m 0700 "$ports_root"
  printf '%s\n' "$legacy_ports" >"$ports_root/service-ports.json"
  IFS=$'\t' read -r cliproxy_port claudex_proxy_port route_proxy_port \
    < <(read_service_ports "$ports_root")
  write_service_ports \
    "$ports_root" "$cliproxy_port" "$claudex_proxy_port" "$route_proxy_port"
  jq -e '
    keys == ["claudexProxyPort", "cliproxyPort", "routeProxyPort"] and
    ([.[]] | unique | length) == 3
  ' "$ports_root/service-ports.json" >/dev/null
done

render_claudex_config \
  "$fixture/claudex.toml" \
  gpt-5.6-sol gpt-5.6-terra claude-sonnet-5 gpt-5.6-sol \
  gpt-5.6-terra claude-sonnet-5 claude-opus-4-8 \
  /usr/bin/true 8317 13456 13457
rg -Fxq 'base_url = "http://127.0.0.1:13457"' "$fixture/claudex.toml"
if rg -qi 'Headroom|X-Headroom-Base-Url' "$fixture/claudex.toml"; then
  printf 'Claudex config still routes through Headroom\n' >&2
  exit 1
fi

for script in "$ROOT"/bin/orichum* "$ROOT/install.sh" "$ROOT/doctor.sh"; do
  [[ -x "$script" ]]
  bash -n "$script"
done

install -d \
  "$fixture/fake-bin" \
  "$fixture/caller" \
  "$fixture/shadowed/integrations/common" \
  "$fixture/data/bin" \
  "$fixture/data/python/cpython-3.14.6/bin"
system_python="$(command -v python3)"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  'elif [[ -n "${CAPTURE_ARGS:-}" ]]; then' \
  '  printf "%s\n" "$@"' \
  'elif [[ -n "${OBSERVE_CWD:-}" ]]; then' \
  '  pwd' \
  'else' \
  "  exec \"$system_python\" \"\$@\"" \
  'fi' >"$fixture/data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/data/bin/orichum-python"
printf '#!/usr/bin/env bash\nexit 99\n' >"$fixture/fake-bin/python3"
chmod 0755 "$fixture/fake-bin/python3"
export ORICHUM_DATA_HOME="$fixture/data"
caller_dir="$(cd "$fixture/caller" && pwd -P)"
graph_from_caller="$(
  cd "$caller_dir"
  PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum-graph" .
)"
[[ "$graph_from_caller" == "[discover] found 0 repositories" ]]
observed_cwd="$(
  cd "$caller_dir"
  OBSERVE_CWD=1 PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum" config
)"
[[ "$observed_cwd" == "$caller_dir" ]]

set +e
ORICHUM_CONFIG_HOME="$ROOT/config" \
PATH="$fixture/fake-bin:$PATH" \
  "$ROOT/bin/orichum" headroom status \
  >"$fixture/headroom-command.stdout" \
  2>"$fixture/headroom-command.stderr"
headroom_command_status=$?
set -e
[[ "$headroom_command_status" -eq 2 ]]
[[ ! -s "$fixture/headroom-command.stdout" ]]
rg -Fq "invalid choice: 'headroom'" "$fixture/headroom-command.stderr"

install -d \
  "$fixture/post-install-system-bin" \
  "$fixture/post-install-user-bin" \
  "$fixture/post-install-data/tools/bin" \
  "$fixture/post-install-data/bin" \
  "$fixture/post-install-data/python/cpython-3.14.6/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  '  exit 0' \
  'fi' \
  'command -v mempalace-mcp' \
  'command -v graphify-mcp' \
  >"$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/post-install-data/bin/orichum-python"
for private_tool in mempalace-mcp graphify-mcp; do
  printf '#!/usr/bin/env bash\nexit 0\n' \
    >"$fixture/post-install-data/tools/bin/$private_tool"
  chmod 0755 "$fixture/post-install-data/tools/bin/$private_tool"
done
ln -s "$ROOT/bin/orichum" "$fixture/post-install-user-bin/orichum"
post_install_tools="$(
  ORICHUM_DATA_HOME="$fixture/post-install-data" \
  PATH="$fixture/post-install-user-bin:$fixture/post-install-system-bin:/usr/bin:/bin" \
    "$fixture/post-install-user-bin/orichum" config
)"
[[ "$post_install_tools" == "$(
  printf '%s\n' \
    "$fixture/post-install-data/tools/bin/mempalace-mcp" \
    "$fixture/post-install-data/tools/bin/graphify-mcp"
)" ]]

forwarded="$(
  cd "$caller_dir"
  CAPTURE_ARGS=1 PATH="$fixture/fake-bin:$PATH" \
    "$ROOT/bin/orichum" -p "acceptance prompt"
)"
[[ "$(tail -n 3 <<<"$forwarded")" == $'--\n-p\nacceptance prompt' ]]
rg -Fxq -- '-I' <<<"$forwarded"
rg -Fq 'export ORICHUM_PYTHON_VALIDATED' "$ROOT/bin/orichum"
if rg -n '(^|[[:space:]])python3([[:space:]]|$)' \
    "$ROOT/bin" "$ROOT/controller/plugin/hooks/hooks.json" \
    "$ROOT/discover-models.sh"; then
  printf 'installed Orichum runtime still invokes ambient python3\n' >&2
  exit 1
fi

touch \
  "$fixture/shadowed/integrations/__init__.py" \
  "$fixture/shadowed/integrations/common/__init__.py"
printf 'raise SystemExit(97)\n' >"$fixture/shadowed/runpy.py"
(
  cd "$fixture/shadowed"
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
  ORICHUM_STATE_HOME="$fixture/state" \
  ORICHUM_CACHE_HOME="$fixture/cache" \
    "$ROOT/bin/orichum" config validate
)

help="$("$ROOT/bin/orichum" --help)"
rg -Fq 'usage: orichum ' <<<"$help"
rg -Fq 'context' <<<"$help"
rg -Fq 'graph' <<<"$help"
rg -Fq 'sessions' <<<"$help"

ORICHUM_CONFIG_HOME="$ROOT/config" \
ORICHUM_DATA_HOME="$fixture/data" \
ORICHUM_STATE_HOME="$fixture/state" \
ORICHUM_CACHE_HOME="$fixture/cache" \
  "$ROOT/bin/orichum" config validate

models="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" models list
)"
rg -Fq 'gpt-5.6-sol' <<<"$models"
rg -Fq 'claude-opus-4-8' <<<"$models"

stacks="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" models stacks
)"
rg -Fq 'STACK' <<<"$stacks"
rg -Fq 'balanced' <<<"$stacks"

stack_list="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack list
)"
rg -Fq 'STACK' <<<"$stack_list"
rg -Fq 'balanced' <<<"$stack_list"
stack_show="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack show balanced
)"
rg -Fq 'ACCOUNT POLICY' <<<"$stack_show"
rg -Fq 'Automatic within provider' <<<"$stack_show"
if ORICHUM_CONFIG_HOME="$ROOT/config" \
    ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack configure \
    >"$fixture/noninteractive-stack.stdout" \
    2>"$fixture/noninteractive-stack.stderr"; then
  printf 'non-interactive stack mutation unexpectedly succeeded\n' >&2
  exit 1
fi
rg -Fq 'stack configuration requires an interactive terminal' \
  "$fixture/noninteractive-stack.stderr"

contexts="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" context list
)"
rg -Fq 'ACCOUNT POOLS' <<<"$contexts"
rg -Fq 'MCP_DOCKER' "$ROOT/README.md"
rg -Fq 'orichum fork' "$ROOT/docs/sessions.md"
rg -Fq 'orichum models stacks' "$ROOT/docs/sessions.md"
rg -Fq 'orichum stack available' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack configure' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack list' "$ROOT/docs/model-stacks.md"
rg -Fq 'orichum stack show STACK' "$ROOT/docs/model-stacks.md"
rg -Fq 'TARGET_STACK' "$ROOT/docs/sessions.md"
if rg -Fq 'claude-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md || \
   rg -Fq 'google-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md; then
  printf 'documentation references model stacks that are not configured\n' >&2
  exit 1
fi
[[ "$(rg -c -- '--max-time 4' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh")" == 3 ]]
rg -Fq \
  'Claudex template separates per-session and recovery proxy ports' \
  "$ROOT/doctor.sh"
rg -Fq 'Claudex template is pending provider login' "$ROOT/doctor.sh"
rg -Fq 'provider_login_pending=false' "$ROOT/doctor.sh"
rg -Fq 'Private CPython 3.14' "$ROOT/doctor.sh"
rg -Fq 'validate_stack_bindings' "$ROOT/doctor.sh"
rg -Fq 'load_accounts(config_root / "accounts.json")' "$ROOT/doctor.sh"
rg -Fq 'repository graph manager and hook contract are available' \
  "$ROOT/doctor.sh"
rg -Fq 'central Graphify storage is private' "$ROOT/doctor.sh"
rg -Fq 'graphify_doctor_diagnostics' "$ROOT/doctor.sh"
rg -Fq 'repository-local legacy Graphify outputs' \
  "$ROOT/lib/workflow.sh"
rg -Fq 'Graphify package/skill drift' "$ROOT/lib/workflow.sh"
[[ ! -e "$ROOT/controller/plugin/scripts/ensure-graphify-hook.py" ]]
"$system_python" -I -B - "$ROOT" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from integrations.common.graph_hooks import (
    graph_hook_status,
    install_graph_hooks,
    remove_upstream_graphify_hooks,
)

assert callable(graph_hook_status)
assert callable(install_graph_hooks)
assert callable(remove_upstream_graphify_hooks)
PY
rg -Fq \
  'Display names appear in explicit account and route inspection output.' \
  "$ROOT/docs/providers-and-accounts.md"
rg -Fq 'validate_orichum_python' \
  "$ROOT/bin/orichum-runtime-ready"
for parallel_health_contract in \
    'clip_verify_pid=$!' \
    'route_verify_pid=$!'; do
  rg -Fq "$parallel_health_contract" "$ROOT/bin/orichum-runtime-ready"
done
for python_summary in \
    'Python request: 3.14.x' \
    'Python version:' \
    'Python runtime:' \
    'Python action:'; do
  rg -Fq "$python_summary" "$ROOT/lib/workflow.sh"
done
[[ "$(jq -r '
  .hooks.SessionStart[0].hooks[0].timeout
' "$ROOT/controller/plugin/hooks/hooks.json")" == 6 ]]

for obsolete in \
  claude-headroom claudex-context claudex-doctor claudex-gpt \
  claudex-headroom claudex-login claudex-models claudex-plugin \
  claudex-provider; do
  [[ ! -e "$ROOT/bin/$obsolete" ]]
done

amd64_workflow="$ROOT/.github/workflows/amd64-acceptance.yml"
[[ -f "$amd64_workflow" ]]
for required_contract in \
    'name: Native AMD64 acceptance' \
    'push:' \
    'pull_request:' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: ubuntu-24.04' \
    'timeout-minutes: 30' \
    'uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0' \
    'sudo apt-get install --yes ripgrep' \
    'PATH="$poison_bin:$USER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'tests/test_orichum_launcher.sh' \
    'Verify central repository graph lifecycle' \
    'orichum graph "$graph_project"' \
    'orichum graph status "$graph_project"' \
    'test ! -e "$graph_project/graphify-out"' \
    'CLAUDEX_MCP_CONFIG' \
    'mcp_config = Path(os.environ["CLAUDEX_MCP_CONFIG"])' \
    'sessions = data_home / "state" / "sessions"' \
    'resolved_data_home / "state" / "sessions" / run_dir.name' \
    'snapshot = run_dir / "graph.json"' \
    'graph_file="${CLAUDEX_MCP_CONFIG%/mcp.json}/graph.json"' \
    'central.relative_to(central_root)' \
    'if not central.is_file():' \
    'chunk = os.read(graph_fd, 1024 * 1024)' \
    'hmac.compare_digest(digest.hexdigest(), expected_digest)' \
    'stat.S_IMODE(before.st_mode) != 0o600' \
    'integrations/common/mcp_probe.py' \
    '--require-tool query_graph' \
    '--require-tool graph_stats' \
    'graphify_command="$(' \
    'run-graph-session-fixture' \
    'name: Linux AMD64 acceptance' \
    'ubuntu:24.04' \
    '--privileged' \
    'loginctl enable-linger orichum' \
    'Verify idempotent upgrade'; do
  rg -Fq -- "$required_contract" "$amd64_workflow"
done
if sed -n '/>>"[$]GITHUB_PATH"/,+3p' "$amd64_workflow" | \
    rg -Fq '$ORICHUM_DATA_HOME/headroom/bin'; then
  printf 'AMD64 acceptance still adds the private tool directory to GITHUB_PATH\n' >&2
  exit 1
fi
set +e
rg -q 'secrets[.]|\$\{\{[[:space:]]*secrets' "$amd64_workflow"
secret_scan_rc=$?
set -e
case "$secret_scan_rc" in
  0)
    printf 'AMD64 acceptance workflow must not consume repository secrets\n' >&2
    exit 1
    ;;
  1) ;;
  *)
    printf 'AMD64 acceptance workflow secret scan failed (rc=%s)\n' \
      "$secret_scan_rc" >&2
    exit 1
    ;;
esac

macos_workflow="$ROOT/.github/workflows/macos-arm64-acceptance.yml"
[[ -f "$macos_workflow" ]]
for required_contract in \
    'name: Native macOS ARM64 acceptance' \
    'push:' \
    'pull_request:' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: macos-15' \
    'GH_TOKEN: ${{ github.token }}' \
    'test "$(uname -m)" = arm64' \
    'brew install ripgrep' \
    'launchctl print "gui/$(id -u)/io.orichum.cliproxy"' \
    'launchctl print "gui/$(id -u)/io.orichum.route-proxy"' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'tests/test_orichum_launcher.sh' \
    'Verify central repository graph lifecycle' \
    'orichum graph "$graph_project"' \
    'orichum graph status "$graph_project"' \
    'test ! -e "$graph_project/graphify-out"' \
    'CLAUDEX_MCP_CONFIG' \
    'mcp_config = Path(os.environ["CLAUDEX_MCP_CONFIG"])' \
    'sessions = data_home / "state" / "sessions"' \
    'resolved_data_home / "state" / "sessions" / run_dir.name' \
    'snapshot = run_dir / "graph.json"' \
    'graph_file="${CLAUDEX_MCP_CONFIG%/mcp.json}/graph.json"' \
    'central.relative_to(central_root)' \
    'if not central.is_file():' \
    'chunk = os.read(graph_fd, 1024 * 1024)' \
    'hmac.compare_digest(digest.hexdigest(), expected_digest)' \
    'stat.S_IMODE(before.st_mode) != 0o600' \
    'integrations/common/mcp_probe.py' \
    '--require-tool query_graph' \
    '--require-tool graph_stats' \
    'graphify_command="$(' \
    'run-graph-session-fixture' \
    'Verify idempotent upgrade' \
    'Clean up launch agents'; do
  rg -Fq -- "$required_contract" "$macos_workflow"
done
if rg -Fq 'macos-15-intel' "$macos_workflow"; then
  printf 'macOS acceptance must run on Apple Silicon only\n' >&2
  exit 1
fi
set +e
rg -q 'secrets[.]|\$\{\{[[:space:]]*secrets' "$macos_workflow"
macos_secret_scan_rc=$?
set -e
case "$macos_secret_scan_rc" in
  0)
    printf 'macOS acceptance workflow must not consume repository secrets\n' >&2
    exit 1
    ;;
  1) ;;
  *)
    printf 'macOS acceptance workflow secret scan failed (rc=%s)\n' \
      "$macos_secret_scan_rc" >&2
    exit 1
    ;;
esac

printf 'PASS: Orichum command and control-plane smoke\n'
