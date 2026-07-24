#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/lib/workflow.sh"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/orichum-plugin-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

checkout="$fixture/checkout"
install -d "$checkout/bin" "$checkout/lib" "$checkout/config" \
  "$fixture/fake-bin" "$fixture/fake-state"
cp "$ROOT/lib/workflow.sh" "$checkout/lib/workflow.sh"
cp "$ROOT/config/plugins.json" "$checkout/config/plugins.json"
cp "$ROOT/bin/orichum-plugin" "$checkout/bin/orichum-plugin"
chmod 0755 "$checkout/bin/orichum-plugin"

printf '[]\n' >"$fixture/fake-state/marketplaces.json"
printf '[]\n' >"$fixture/fake-state/plugins.json"
: >"$fixture/fake-state/calls.log"

cat >"$fixture/fake-bin/claude" <<'FAKE_CLAUDE'
#!/usr/bin/env bash
set -euo pipefail

state_root="${FAKE_CLAUDE_STATE:?}"
printf 'config=%s args=%s\n' "${CLAUDE_CONFIG_DIR:-}" "$*" >>"$state_root/calls.log"

if [[ "${1:-}" != plugin ]]; then
  printf 'unexpected fake Claude command: %s\n' "$*" >&2
  exit 2
fi
shift
case "${1:-}" in
  marketplace)
    shift
    case "${1:-}" in
      list)
        cat "$state_root/marketplaces.json"
        ;;
      add)
        source_value="${@: -1}"
        marketplace_name="${source_value##*/}"
        marketplace_name="${marketplace_name%.git}"
        jq --arg name "$marketplace_name" --arg source "$source_value" \
          '. + [{name: $name, source: $source}]' \
          "$state_root/marketplaces.json" >"$state_root/marketplaces.next"
        mv "$state_root/marketplaces.next" "$state_root/marketplaces.json"
        ;;
      update) ;;
      *) exit 2 ;;
    esac
    ;;
  list)
    cat "$state_root/plugins.json"
    ;;
  install)
    plugin_id="${2:?}"
    jq --arg id "$plugin_id" '. + [{id: $id, enabled: true}]' \
      "$state_root/plugins.json" >"$state_root/plugins.next"
    mv "$state_root/plugins.next" "$state_root/plugins.json"
    ;;
  update|enable)
    ;;
  uninstall)
    plugin_id="${2:?}"
    jq --arg id "$plugin_id" 'map(select(.id != $id))' \
      "$state_root/plugins.json" >"$state_root/plugins.next"
    mv "$state_root/plugins.next" "$state_root/plugins.json"
    ;;
  *)
    printf 'unexpected fake Claude plugin command: %s\n' "$*" >&2
    exit 2
    ;;
esac
FAKE_CLAUDE
chmod 0755 "$fixture/fake-bin/claude"

run_plugin() {
  PATH="$fixture/fake-bin:$PATH" \
    FAKE_CLAUDE_STATE="$fixture/fake-state" \
    ORICHUM_CONFIG_HOME="$checkout/config" \
    ORICHUM_DATA_HOME="$fixture/private data" \
    "$checkout/bin/orichum-plugin" "$@"
}

[[ "$(jq -c . "$checkout/config/plugins.json")" == \
  '{"schemaVersion":1,"marketplaces":[],"plugins":[]}' ]]

empty_list="$(run_plugin list)"
grep -Fq 'PLUGIN' <<<"$empty_list"
grep -Fq '(none)' <<<"$empty_list"

: >"$fixture/fake-state/calls.log"
run_plugin sync
[[ ! -s "$fixture/fake-state/calls.log" ]]

run_plugin add sample@acme --source example/acme
jq -e '.marketplaces == [{"name":"acme","source":"example/acme"}]' \
  "$checkout/config/plugins.json" >/dev/null
jq -e '.plugins == ["sample@acme"]' "$checkout/config/plugins.json" >/dev/null
jq -e 'map(.id) == ["sample@acme"]' "$fixture/fake-state/plugins.json" >/dev/null
grep -Fq "config=$fixture/private data/claude-config args=plugin marketplace add --scope user example/acme" \
  "$fixture/fake-state/calls.log"
grep -Fq "config=$fixture/private data/claude-config args=plugin install sample@acme --scope user" \
  "$fixture/fake-state/calls.log"

: >"$fixture/fake-state/calls.log"
run_plugin sync
grep -Fq 'args=plugin marketplace update acme' "$fixture/fake-state/calls.log"
grep -Fq 'args=plugin update sample@acme --scope user' "$fixture/fake-state/calls.log"
grep -Fq 'args=plugin enable sample@acme' "$fixture/fake-state/calls.log"
if grep -Fq 'args=plugin install sample@acme' "$fixture/fake-state/calls.log"; then
  printf 'idempotent sync reinstalled an existing plugin\n' >&2
  exit 1
fi

run_plugin add formatter@acme
jq -e '.plugins == ["formatter@acme", "sample@acme"]' \
  "$checkout/config/plugins.json" >/dev/null

manifest_digest="$(sha256_file "$checkout/config/plugins.json")"
if run_plugin add invalid >/dev/null 2>&1; then
  printf 'invalid plugin identifier was accepted\n' >&2
  exit 1
fi
if run_plugin add unknown@missing >/dev/null 2>&1; then
  printf 'undeclared marketplace without a source was accepted\n' >&2
  exit 1
fi
[[ "$(sha256_file "$checkout/config/plugins.json")" == \
  "$manifest_digest" ]]

run_plugin remove sample@acme
jq -e '.plugins == ["formatter@acme"]' "$checkout/config/plugins.json" >/dev/null
jq -e 'map(.id) == ["formatter@acme"]' "$fixture/fake-state/plugins.json" >/dev/null

: >"$fixture/fake-state/calls.log"
run_plugin update
grep -Fq 'args=plugin update formatter@acme --scope user' \
  "$fixture/fake-state/calls.log"

listed="$(run_plugin list)"
grep -Fq 'formatter@acme' <<<"$listed"
grep -Fq 'installed' <<<"$listed"

jq '.marketplaces = []' "$checkout/config/plugins.json" \
  >"$checkout/config/plugins.next"
mv "$checkout/config/plugins.next" "$checkout/config/plugins.json"
if run_plugin list >"$fixture/malformed.stdout" 2>"$fixture/malformed.stderr"; then
  printf 'plugin referencing an undeclared marketplace was accepted\n' >&2
  exit 1
fi
grep -Fq 'invalid plugin declaration' "$fixture/malformed.stderr"

printf 'PASS: portable Orichum plugin management\n'
