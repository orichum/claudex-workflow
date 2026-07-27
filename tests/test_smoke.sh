#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
[[ "$(<"$ROOT/VERSION")" == 0.1.0-rc.2 ]]
rg -Fq '## 0.1.0-rc.2 - 2026-07-28' "$ROOT/CHANGELOG.md"
rg -Fq 'orichum --version' "$ROOT/docs/cli-reference.md"
rg -Fq '[Changelog](CHANGELOG.md)' "$ROOT/README.md"
rg -Fq 'orichum sessions cleanup' \
  "$ROOT/docs/cli-reference.md" "$ROOT/docs/sessions.md"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-smoke.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

ports_root="$fixture/ports"
install -d -m 0700 "$ports_root"
write_service_ports "$ports_root" 8317 13456 13457
IFS=$'\t' read -r cliproxy_port claudex_proxy_port route_proxy_port \
  < <(read_service_ports "$ports_root")
[[ "$cliproxy_port" == 8317 ]]
[[ "$claudex_proxy_port" == 13456 ]]
[[ "$route_proxy_port" == 13457 ]]

render_claudex_config \
  "$fixture/claudex.toml" \
  gpt-5.6-sol gpt-5.6-terra claude-sonnet-5 gpt-5.6-sol \
  gpt-5.6-terra claude-sonnet-5 claude-opus-4-8 \
  /usr/bin/true 8317 13456 13457
rg -Fxq 'base_url = "http://127.0.0.1:13457"' "$fixture/claudex.toml"

for script in "$ROOT"/bin/orichum* "$ROOT/install.sh" "$ROOT/doctor.sh"; do
  [[ -x "$script" ]]
  bash -n "$script"
done
rg -Fq \
  'Usage: ./install.sh [--upgrade | --uninstall [--purge]]' \
  "$ROOT/install.sh"

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
observed_cwd="$(
  cd "$caller_dir"
  OBSERVE_CWD=1 PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum" config
)"
[[ "$observed_cwd" == "$caller_dir" ]]

install -d \
  "$fixture/post-install-system-bin" \
  "$fixture/post-install-user-bin" \
  "$fixture/post-install-data/bin" \
  "$fixture/post-install-data/python/cpython-3.14.6/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  '  exit 0' \
  'fi' \
  'exit 0' \
  >"$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/post-install-data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/post-install-data/bin/orichum-python"
ln -s "$ROOT/bin/orichum" "$fixture/post-install-user-bin/orichum"
post_install_tools="$(
  ORICHUM_DATA_HOME="$fixture/post-install-data" \
  PATH="$fixture/post-install-user-bin:$fixture/post-install-system-bin:/usr/bin:/bin" \
    "$fixture/post-install-user-bin/orichum" config
)"
[[ -z "$post_install_tools" ]]

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
rg -Fq 'leanctx' <<<"$help"
rg -Fq 'sessions' <<<"$help"

leanctx_help="$("$ROOT/bin/orichum" leanctx --help)"
rg -Fq 'dashboard' <<<"$leanctx_help"
rg -Fq 'list' <<<"$leanctx_help"
rg -Fq 'stats' <<<"$leanctx_help"
rg -Fq 'watch' <<<"$leanctx_help"

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
rg -Fq 'orichum provider configure' \
  "$ROOT/README.md" \
  "$ROOT/docs/providers-and-accounts.md" \
  "$ROOT/docs/cli-reference.md"
rg -Fq 'TARGET_STACK' "$ROOT/docs/sessions.md"
if rg -Fq 'claude-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md || \
   rg -Fq 'google-heavy' "$ROOT/README.md" "$ROOT/docs"/*.md; then
  printf 'documentation references model stacks that are not configured\n' >&2
  exit 1
fi
rg -Fq 'https://github.com/orichum/claudex-workflow.git' \
  "$ROOT/README.md" "$ROOT/docs/installation.md"
if rg -Fq 'https://github.com/arvind9981/claudex-workflow.git' \
    "$ROOT/README.md" "$ROOT/docs/installation.md"; then
  printf 'documentation still uses the pre-organization repository URL\n' >&2
  exit 1
fi
rg -Fq 'macOS on Apple Silicon (native acceptance)' \
  "$ROOT/docs/installation.md"
rg -Fq 'Linux on x86-64 with systemd (native acceptance)' \
  "$ROOT/docs/installation.md"
rg -Fq 'WSL2 on x86-64 with systemd (contract acceptance)' \
  "$ROOT/docs/installation.md"
if rg -Fq 'macOS on Apple Silicon or x86-64' \
    "$ROOT/docs/installation.md"; then
  printf 'installation guide overstates native platform acceptance\n' >&2
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
  claudex-context claudex-doctor claudex-gpt \
  claudex-login claudex-models claudex-plugin \
  claudex-provider; do
  [[ ! -e "$ROOT/bin/$obsolete" ]]
done

amd64_workflow="$ROOT/.github/workflows/amd64-acceptance.yml"
[[ -f "$amd64_workflow" ]]
for required_contract in \
    'name: Native AMD64 acceptance' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: blacksmith-4vcpu-ubuntu-2404' \
    'timeout-minutes: 30' \
    'uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0' \
    'sudo apt-get install --yes ripgrep' \
    'PATH="$poison_bin:$USER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' \
    'Run repository test suites' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'tests/test_orichum_launcher.sh' \
    'Verify LeanCTX code-intelligence contract' \
    'probe_leanctx_capabilities' \
    'name: Linux AMD64 acceptance' \
    'ubuntu:24.04' \
    '--privileged' \
    'loginctl enable-linger orichum' \
    'Verify fast repeat and explicit upgrade' \
    'repeat_started="$(python3 -c' \
    'test "$repeat_ms" -lt 15000' \
    'orichum-fast.log' \
    'Fast readiness checks passed.' \
    '^Controller plugin[[:space:]]+reused' \
    './install.sh --upgrade' \
    'orichum-upgrade.log' \
    '^Controller plugin[[:space:]]+upgraded' \
    '.components.routing | not' \
    'Running Orichum doctor'; do
  rg -Fq -- "$required_contract" "$amd64_workflow"
done
if rg -q '^  push:' "$amd64_workflow"; then
  printf 'AMD64 acceptance must not repeat verified PR work after merge\n' >&2
  exit 1
fi
if rg -q '^  pull_request:' "$amd64_workflow"; then
  printf 'AMD64 acceptance must run only when explicitly dispatched\n' >&2
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
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: blacksmith-6vcpu-macos-15' \
    'GH_TOKEN: ${{ github.token }}' \
    'test "$(uname -m)" = arm64' \
    'brew install ripgrep' \
    'launchctl print "gui/$(id -u)/io.orichum.cliproxy"' \
    'launchctl print "gui/$(id -u)/io.orichum.route-proxy"' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'Verify LeanCTX code-intelligence contract' \
    'probe_leanctx_capabilities' \
    'Verify fast repeat and explicit upgrade' \
    'repeat_started="$(python3 -c' \
    'test "$repeat_ms" -lt 15000' \
    'orichum-fast.log' \
    'Fast readiness checks passed.' \
    '^Controller plugin[[:space:]]+reused' \
    './install.sh --upgrade' \
    'orichum-upgrade.log' \
    '^Controller plugin[[:space:]]+upgraded' \
    '.components.routing | not' \
    'Running Orichum doctor' \
    'Clean up launch agents'; do
  rg -Fq -- "$required_contract" "$macos_workflow"
done
if rg -q '^  push:' "$macos_workflow"; then
  printf 'macOS acceptance must not repeat verified PR work after merge\n' >&2
  exit 1
fi
if rg -q '^  pull_request:' "$macos_workflow"; then
  printf 'macOS acceptance must run only when explicitly dispatched\n' >&2
  exit 1
fi
if rg -Fq 'Run repository test suites' "$macos_workflow"; then
  printf 'macOS acceptance must not repeat platform-neutral repository tests\n' >&2
  exit 1
fi
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

for installer_document in \
    "$ROOT/README.md" \
    "$ROOT/docs/installation.md" \
    "$ROOT/docs/cli-reference.md"; do
  rg -Fq './install.sh --upgrade' "$installer_document"
done
for installation_contract in \
    'Fast reconciliation' \
    'about 10 seconds' \
    'state/install-state.json' \
    'identities and digests, not secrets'; do
  rg -Fq "$installation_contract" "$ROOT/docs/installation.md"
done

printf 'PASS: Orichum command and control-plane smoke\n'
