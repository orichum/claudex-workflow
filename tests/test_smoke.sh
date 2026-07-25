#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-smoke.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

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
observed_cwd="$(
  cd "$caller_dir"
  OBSERVE_CWD=1 PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum" config
)"
[[ "$observed_cwd" == "$caller_dir" ]]

install -d \
  "$fixture/post-install-system-bin" \
  "$fixture/post-install-user-bin" \
  "$fixture/post-install-data/headroom/bin" \
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
    >"$fixture/post-install-data/headroom/bin/$private_tool"
  chmod 0755 "$fixture/post-install-data/headroom/bin/$private_tool"
done
ln -s "$ROOT/bin/orichum" "$fixture/post-install-user-bin/orichum"
post_install_tools="$(
  ORICHUM_DATA_HOME="$fixture/post-install-data" \
  PATH="$fixture/post-install-user-bin:$fixture/post-install-system-bin:/usr/bin:/bin" \
    "$fixture/post-install-user-bin/orichum" config
)"
[[ "$post_install_tools" == "$(
  printf '%s\n' \
    "$fixture/post-install-data/headroom/bin/mempalace-mcp" \
    "$fixture/post-install-data/headroom/bin/graphify-mcp"
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
rg -Fq 'orichum fork' "$ROOT/README.md"
rg -Fq 'orichum models stacks' "$ROOT/README.md"
rg -Fq 'orichum stack available' "$ROOT/README.md"
rg -Fq 'orichum stack configure' "$ROOT/README.md"
rg -Fq 'orichum stack list' "$ROOT/README.md"
rg -Fq 'orichum stack show heavy' "$ROOT/README.md"
rg -Fq 'TARGET_STACK' "$ROOT/README.md"
if rg -Fq 'claude-heavy' "$ROOT/README.md" || \
   rg -Fq 'google-heavy' "$ROOT/README.md"; then
  printf 'README references model stacks that are not configured\n' >&2
  exit 1
fi
[[ "$(rg -c -- '--max-time 4' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh")" == 4 ]]
rg -Fq \
  'Claudex template separates per-session and recovery proxy ports' \
  "$ROOT/doctor.sh"
rg -Fq 'Claudex template is pending provider login' "$ROOT/doctor.sh"
rg -Fq 'provider_login_pending=false' "$ROOT/doctor.sh"
rg -Fq 'Private CPython 3.14' "$ROOT/doctor.sh"
rg -Fq 'validate_stack_bindings' "$ROOT/doctor.sh"
rg -Fq 'load_accounts(config_root / "accounts.json")' "$ROOT/doctor.sh"
rg -Fq \
  'Account display names are shown only by explicit account-management and stack' \
  "$ROOT/README.md"
rg -Fq 'validate_orichum_python' \
  "$ROOT/bin/orichum-runtime-ready"
for parallel_health_contract in \
    'clip_verify_pid=$!' \
    'head_verify_pid=$!' \
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
    'pull_request:' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: ubuntu-24.04' \
    'timeout-minutes: 30' \
    'uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0' \
    'sudo apt-get install --yes ripgrep' \
    'PATH="$poison_bin:$USER_BIN_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' \
    'systemctl --user show orichum-headroom.service --property=ExecStart --value' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'systemd-container:' \
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
    'pull_request:' \
    'workflow_dispatch:' \
    'permissions:' \
    'contents: read' \
    'runs-on: macos-15' \
    'GH_TOKEN: ${{ github.token }}' \
    'test "$(uname -m)" = arm64' \
    'brew install ripgrep' \
    'launchctl print "gui/$(id -u)/io.orichum.cliproxy"' \
    'launchctl print "gui/$(id -u)/io.orichum.headroom"' \
    'launchctl print "gui/$(id -u)/io.orichum.route-proxy"' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'tests/test_live_stack_routes.sh' \
    'Verify idempotent upgrade' \
    'Clean up launch agents'; do
  rg -Fq "$required_contract" "$macos_workflow"
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
