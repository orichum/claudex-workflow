#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for script in \
  install.sh doctor.sh rollback.sh smoke-test.sh discover-models.sh \
  bin/claudex-gpt bin/claudex-login bin/claudex-doctor bin/claude-headroom \
  controller/plugin/scripts/check-local-services.sh \
  controller/plugin/scripts/guard-orchestration.sh
do
  bash -n "$ROOT/$script"
done

if rg -q 'CLIPROXY_VERSION|CLAUDEX_VERSION|/opt/homebrew|/Users/arvind' \
  "$ROOT/install.sh" "$ROOT/bin" "$ROOT/lib"; then
  printf 'portable scripts contain a tool pin or personal absolute path\n' >&2
  exit 1
fi

if ! rg -q 'releases/latest' "$ROOT/lib/workflow.sh"; then
  printf 'installer is not using rolling GitHub releases\n' >&2
  exit 1
fi

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-installer-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT
# shellcheck source=../lib/workflow.sh
source "$ROOT/lib/workflow.sh"
render_systemd_user_unit "$fixture/claudex.service" "$fixture/root with % and \$"
rg -q '^Type=exec$' "$fixture/claudex.service"
rg -q '^Restart=on-failure$' "$fixture/claudex.service"
rg -q '^StandardOutput="append:' "$fixture/claudex.service"
rg -q '%%' "$fixture/claudex.service"
rg -q '\$\$' "$fixture/claudex.service"

printf 'PASS: portable installer surface\n'
