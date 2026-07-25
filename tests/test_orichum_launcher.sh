#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-launcher.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

install -d \
  "$fixture/data/bin" \
  "$fixture/data/python/cpython-3.14.6/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$*" == *platform.python_implementation* ]]; then' \
  "  printf 'CPython\\t3.14.6\\n'" \
  'else' \
  '  printf "%s\n" "$@"' \
  'fi' >"$fixture/data/python/cpython-3.14.6/bin/python3.14"
chmod 0755 "$fixture/data/python/cpython-3.14.6/bin/python3.14"
ln -s "$fixture/data/python/cpython-3.14.6/bin/python3.14" \
  "$fixture/data/bin/orichum-python"

forwarded="$(
  ORICHUM_DATA_HOME="$fixture/data" \
    "$ROOT/bin/orichum" stack list
)"

[[ "$(tail -n 2 <<<"$forwarded")" == $'stack\nlist' ]]
! rg -Fxq -- 'run' <<<"$forwarded"
