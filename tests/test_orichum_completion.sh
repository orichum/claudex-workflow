#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture="$(mktemp -d)"
trap 'rm -rf -- "$fixture"' EXIT

home="$fixture/home"
config="$fixture/config"
data="$fixture/data"
state="$fixture/state"
mkdir -p \
  "$home" "$config" "$data" \
  "$state/logical-sessions/oc-s-0123456789abcdef" \
  "$state/sessions/run.abc123"

cat >"$config/model-stacks.json" <<'JSON'
{"schemaVersion":2,"stacks":{"balanced":{},"claude focus":{}}}
JSON
cat >"$config/providers.json" <<'JSON'
{
  "schemaVersion":1,
  "providers":{"anthropic":{"authType":"claude"},"openai":{"authType":"codex"}},
  "accountPools":{"shared":{},"realtime":{}}
}
JSON
cat >"$config/accounts.json" <<'JSON'
{
  "schemaVersion":2,
  "accounts":[
    {
      "id":"oc-a-0123456789abcdef",
      "name":"Personal GPT",
      "credentialRef":"do-not-complete-secret.json"
    },
    {
      "id":"oc-a-fedcba9876543210",
      "name":"Team:Blue",
      "credentialRef":"also-secret.json"
    }
  ]
}
JSON
cat >"$config/plugins.json" <<'JSON'
{
  "schemaVersion":1,
  "plugins":["github@official"],
  "marketplaces":[{"name":"official","source":"secret-source"}]
}
JSON
cat >"$config/projects.json" <<'JSON'
{
  "schemaVersion":1,
  "contexts":[
    {"root":"/work/acme project"},
    {"root":"bad\ncontext"}
  ]
}
JSON
printf '{}\n' >"$state/logical-sessions/oc-s-0123456789abcdef/binding.json"
printf '{}\n' >"$state/sessions/run.abc123/.complete"

complete_values() {
  ORICHUM_HOME="$home" \
  ORICHUM_CONFIG_HOME="$config" \
  ORICHUM_DATA_HOME="$data" \
  ORICHUM_STATE_HOME="$state" \
    "$ROOT/bin/orichum-complete" "$@"
}

[[ "$(complete_values stack '')" == $'balanced\tstack\nclaude focus\tstack' ]]
[[ "$(complete_values provider o)" == $'openai\tprovider' ]]
[[ "$(complete_values pool '')" == $'realtime\tpool\nshared\tpool' ]]
[[ "$(complete_values auth-type '')" == $'claude\tprovider login type\ncodex\tprovider login type' ]]
[[ "$(complete_values account Personal)" == $'Personal GPT\taccount' ]]
[[ "$(complete_values account oc-a-0)" == $'oc-a-0123456789abcdef\taccount' ]]
[[ "$(complete_values plugin '')" == $'github@official\tplugin' ]]
[[ "$(complete_values marketplace '')" == $'official\tmarketplace' ]]
[[ "$(complete_values plugin-add 'sample@o')" == $'sample@official\tplugin' ]]
[[ "$(complete_values context /work)" == $'/work/acme project\tcontext' ]]
[[ "$(complete_values logical-session '')" == $'oc-s-0123456789abcdef\tlogical session' ]]
[[ "$(complete_values run '')" == $'run.abc123\tphysical run' ]]

all_values="$(
  for kind in stack provider pool auth-type account plugin plugin-add marketplace context logical-session run; do
    complete_values "$kind" ''
  done
)"
[[ "$all_values" != *secret* ]]
[[ "$all_values" != *credentialRef* ]]
[[ "$all_values" != *$'\ncontext'* ]]

PYTHONPATH="$ROOT" python3 - <<'PY' >"$fixture/orichum.bash"
from integrations.common.orichum_cli import build_parser
from integrations.common.orichum_completion import render_completion
print(render_completion(build_parser(), "bash"), end="")
PY
mkdir -p "$fixture/bin"
ln -s "$ROOT/bin/orichum" "$fixture/bin/orichum"
export PATH="$fixture/bin:$PATH"
export ORICHUM_HOME="$home"
export ORICHUM_CONFIG_HOME="$config"
export ORICHUM_DATA_HOME="$data"
export ORICHUM_STATE_HOME="$state"
# shellcheck source=/dev/null
source "$fixture/orichum.bash"

contains_reply() {
  local expected="$1" reply
  for reply in "${COMPREPLY[@]}"; do
    [[ "$reply" == "$expected" ]] && return 0
  done
  return 1
}

COMP_WORDS=(orichum s)
COMP_CWORD=1
_orichum_complete
contains_reply stack
contains_reply status
contains_reply setup

COMP_WORDS=(orichum stack sh)
COMP_CWORD=2
_orichum_complete
contains_reply show

COMP_WORDS=(orichum stack show b)
COMP_CWORD=3
_orichum_complete
contains_reply balanced

COMP_WORDS=(orichum leanctx dashboard --open n)
COMP_CWORD=4
_orichum_complete
contains_reply none

COMP_WORDS=(orichum fork oc-s-0123456789abcdef --stack=bal)
COMP_CWORD=3
_orichum_complete
contains_reply --stack=balanced

COMP_WORDS=(orichum plugin add sample@o)
COMP_CWORD=3
_orichum_complete
contains_reply sample@official

touch "$fixture/handoff.md"
COMP_WORDS=(orichum fork oc-s-0123456789abcdef --handoff-file "$fixture/hand")
COMP_CWORD=4
_orichum_complete
contains_reply "$fixture/handoff.md"

COMP_WORDS=(orichum run -- anything '')
COMP_CWORD=4
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

COMP_WORDS=(orichum run anything '')
COMP_CWORD=3
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

COMP_WORDS=(orichum resume oc-s-0123456789abcdef anything '')
COMP_CWORD=4
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

COMP_WORDS=(orichum resume oc-s-0123456789abcdef '')
COMP_CWORD=3
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

COMP_WORDS=(orichum provider login c)
COMP_CWORD=3
_orichum_complete
contains_reply codex
contains_reply claude

COMP_WORDS=(orichum provider login codex '')
COMP_CWORD=4
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

COMP_WORDS=(orichum provider login codex anything '')
COMP_CWORD=5
_orichum_complete
[[ "${#COMPREPLY[@]}" -eq 0 ]]

if command -v zsh >/dev/null 2>&1; then
  PYTHONPATH="$ROOT" python3 - <<'PY' >"$fixture/_orichum"
from integrations.common.orichum_cli import build_parser
from integrations.common.orichum_completion import render_completion
print(render_completion(build_parser(), "zsh"), end="")
PY
  cat >"$fixture/test-zsh-completion.zsh" <<'ZSH'
words=(orichum '')
CURRENT=2
_describe() { :; }
_files() { :; }
_directories() { :; }
source "$1"
typeset -gi describe_calls=0
_describe() { (( describe_calls += 1 )); }

typeset -a described_values
_describe() {
  local values_name="$2"
  described_values=("${(@P)values_name}")
  (( describe_calls += 1 ))
}

_orichum_add_values 'provider account enable:positional:0' 'Team:'
[[ "${described_values[1]}" == 'Team\:Blue:account' ]]

words=(orichum fork oc-s-0123456789abcdef --stack=bal)
CURRENT=4
_orichum
[[ "${described_values[1]}" == '--stack=balanced:stack' ]]
describe_calls=0

words=(orichum run anything '')
CURRENT=4
_orichum
(( describe_calls == 0 ))

words=(orichum resume oc-s-0123456789abcdef anything '')
CURRENT=5
_orichum
(( describe_calls == 0 ))

words=(orichum resume oc-s-0123456789abcdef '')
CURRENT=4
_orichum
(( describe_calls == 0 ))

words=(orichum provider login codex anything '')
CURRENT=6
_orichum
(( describe_calls == 0 ))

words=(orichum provider login codex '')
CURRENT=5
_orichum
(( describe_calls == 0 ))
ZSH
  zsh -f "$fixture/test-zsh-completion.zsh" "$fixture/_orichum"
fi

printf '{broken\n' >"$config/model-stacks.json"
[[ -z "$(complete_values stack '' 2>"$fixture/broken.stderr")" ]]
[[ ! -s "$fixture/broken.stderr" ]]

set +e
complete_values unknown '' >"$fixture/unknown.stdout" 2>"$fixture/unknown.stderr"
unknown_status="$?"
set -e
if [[ "$unknown_status" -eq 0 ]]; then
  printf 'unknown completion kind unexpectedly succeeded\n' >&2
  exit 1
fi
[[ "$unknown_status" -eq 2 ]]
[[ ! -s "$fixture/unknown.stdout" ]]

ORICHUM_HOME="$home" \
ORICHUM_CONFIG_HOME="$config" \
ORICHUM_DATA_HOME="$data" \
ORICHUM_STATE_HOME="$state" \
  "$ROOT/bin/orichum" __complete account Personal \
  >"$fixture/launcher.stdout" 2>"$fixture/launcher.stderr"
[[ "$(cat "$fixture/launcher.stdout")" == $'Personal GPT\taccount' ]]
[[ ! -s "$fixture/launcher.stderr" ]]

printf 'completion helper contract tests passed\n'
