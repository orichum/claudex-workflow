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
  "$fixture/shadowed/integrations/common"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ -n "${CAPTURE_ARGS:-}" ]]; then' \
  '  printf "%s\n" "$@"' \
  'else' \
  '  pwd' \
  'fi' >"$fixture/fake-bin/python3"
chmod 0755 "$fixture/fake-bin/python3"
caller_dir="$(cd "$fixture/caller" && pwd -P)"
observed_cwd="$(
  cd "$caller_dir"
  PATH="$fixture/fake-bin:$PATH" "$ROOT/bin/orichum" config
)"
[[ "$observed_cwd" == "$caller_dir" ]]

forwarded="$(
  cd "$caller_dir"
  CAPTURE_ARGS=1 PATH="$fixture/fake-bin:$PATH" \
    "$ROOT/bin/orichum" -p "acceptance prompt"
)"
[[ "$(tail -n 3 <<<"$forwarded")" == $'--\n-p\nacceptance prompt' ]]
rg -Fxq -- '-I' <<<"$forwarded"

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

contexts="$(
  ORICHUM_CONFIG_HOME="$ROOT/config" \
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" context list
)"
rg -Fq 'ACCOUNT POOLS' <<<"$contexts"
rg -Fq 'MCP_DOCKER' "$ROOT/README.md"
rg -Fq 'orichum fork' "$ROOT/README.md"
[[ "$(rg -c -- '--max-time 4' \
  "$ROOT/controller/plugin/scripts/check-local-services.sh")" == 3 ]]
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
    '$ORICHUM_DATA_HOME/headroom/bin' \
    'systemctl --user show orichum-headroom.service --property=ExecStart --value' \
    'Fresh install without providers' \
    'Activate disposable multi-family routes' \
    'Verify idempotent upgrade'; do
  rg -Fq "$required_contract" "$amd64_workflow"
done
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

printf 'PASS: Orichum command and control-plane smoke\n'
