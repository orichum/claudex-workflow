#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
launcher="$ROOT/bin/claudex-models"

bash -n "$launcher"

fixture="$(mktemp -d "${TMPDIR:-/tmp}/claudex-models-test.XXXXXX")"
trap 'rm -rf -- "$fixture"' EXIT

workflow="$fixture/workflow"
data_root="$fixture/data"
tools="$fixture/tools"
temporary="$fixture/tmp"
elsewhere="$fixture/elsewhere"
install -d "$workflow/bin" "$workflow/lib" \
  "$workflow/integrations/common" "$workflow/controller" \
  "$data_root" "$tools" "$temporary" "$elsewhere"
install -m 0755 "$launcher" "$workflow/bin/claudex-models"
install -m 0644 \
  "$ROOT/lib/workflow.sh" "$workflow/lib/workflow.sh"
install -m 0644 \
  "$ROOT/integrations/__init__.py" "$workflow/integrations/__init__.py"
install -m 0644 \
  "$ROOT/integrations/common/__init__.py" \
  "$ROOT/integrations/common/model_routing.py" \
  "$workflow/integrations/common/"

jq -n '{
  schemaVersion: 1,
  defaultStack: "portable",
  stacks: {
    portable: {
      controller: "provider/controller",
      agents: {
        "repository-explorer": [
          "provider/explorer-preferred",
          "provider/explorer-fallback"
        ],
        "repository-verifier": [
          "provider/verifier-preferred",
          "provider/verifier-fallback"
        ],
        "correctness-critic": [
          "provider/critic-preferred",
          "provider/critic-fallback"
        ],
        "architecture-advisor": [
          "provider/architect-preferred",
          "provider/architect-fallback"
        ],
        "implementation-worker": [
          "provider/worker-preferred",
          "provider/worker-fallback"
        ]
      }
    }
  }
}' >"$workflow/controller/model-routing.json"

jq -n '{
  object: "list",
  data: [
    {id: "provider/controller"},
    {id: "provider/explorer-fallback"},
    {id: "provider/verifier-fallback"},
    {id: "provider/critic-fallback"},
    {id: "provider/architect-fallback"},
    {id: "provider/worker-fallback"}
  ]
}' >"$fixture/models.json"
jq -n '{
  object: "list",
  data: [
    {id: "provider/explorer-fallback"},
    {id: "provider/verifier-fallback"},
    {id: "provider/critic-fallback"},
    {id: "provider/architect-fallback"},
    {id: "provider/worker-fallback"}
  ]
}' >"$fixture/models-without-controller.json"
printf 'not-json\n' >"$fixture/models-malformed.json"
printf '%s\n' \
  '{"object":"list","data":[{"id":"safe/model\ninjected"}]}' \
  >"$fixture/models-unsafe.json"
jq -n '{
  claudexProxyPort: 13456,
  cliproxyPort: 18317,
  headroomPort: 18787
}' >"$data_root/service-ports.json"

cat >"$tools/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >"$MODELS_CURL_ARGS"
cat "$MODELS_FIXTURE"
EOF
chmod 0755 "$tools/curl"

list_output="$(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" list
)"

rg -Fq 'STACK' <<<"$list_output"
rg -Fq 'SCOPE' <<<"$list_output"
rg -Fq 'ROLE' <<<"$list_output"
rg -Fq 'CANDIDATES' <<<"$list_output"
rg -Fq 'SELECTED' <<<"$list_output"
rg -Fq 'STATUS' <<<"$list_output"
rg -Fq 'portable' <<<"$list_output"
rg -Fq 'global' <<<"$list_output"
rg -Fq 'controller' <<<"$list_output"
for role_and_model in \
  'repository-explorer explorer' \
  'repository-verifier verifier' \
  'correctness-critic critic' \
  'architecture-advisor architect' \
  'implementation-worker worker'
do
  read -r role model_name <<<"$role_and_model"
  rg -Fq "$role" <<<"$list_output"
  rg -Fq \
    "provider/$model_name-preferred [unavailable] -> provider/$model_name-fallback [available]" \
    <<<"$list_output"
  rg -Fq "provider/$model_name-fallback" <<<"$list_output"
done
rg -Fq 'http://127.0.0.1:18317/v1/models' "$fixture/curl.args"
rg -Fq -- '--connect-timeout 1' "$fixture/curl.args"
rg -Fq -- '--max-time 4' "$fixture/curl.args"
[[ -z "$(find "$temporary" -mindepth 1 -print -quit)" ]]

validate_output="$(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" validate
)"
[[ -z "$validate_output" ]]
[[ -z "$(find "$temporary" -mindepth 1 -print -quit)" ]]

selected_output="$(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" list portable
)"
rg -Fq 'selected' <<<"$selected_output"

unavailable_status=0
(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models-without-controller.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" validate \
      >"$fixture/invalid.stdout" 2>"$fixture/invalid.stderr"
) || unavailable_status=$?
if [[ "$unavailable_status" -eq 0 ]]; then
  printf 'unavailable controller was accepted\n' >&2
  exit 1
fi
[[ "$unavailable_status" -eq 42 ]]
[[ ! -s "$fixture/invalid.stdout" ]]
rg -Fq 'controller provider/controller is unavailable' \
  "$fixture/invalid.stderr"
[[ -z "$(find "$temporary" -mindepth 1 -print -quit)" ]]

malformed_status=0
(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models-malformed.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" validate \
      >"$fixture/malformed.stdout" 2>"$fixture/malformed.stderr"
) || malformed_status=$?
[[ "$malformed_status" -eq 1 ]]
[[ ! -s "$fixture/malformed.stdout" ]]
rg -Fq 'model catalogue could not be parsed' "$fixture/malformed.stderr"
[[ -z "$(find "$temporary" -mindepth 1 -print -quit)" ]]

unsafe_status=0
(
  cd "$elsewhere"
  CLAUDEX_DATA_DIR="$data_root" \
  MODELS_CURL_ARGS="$fixture/curl.args" \
  MODELS_FIXTURE="$fixture/models-unsafe.json" \
  TMPDIR="$temporary" \
  PATH="$tools:$PATH" \
    "$workflow/bin/claudex-models" validate \
      >"$fixture/unsafe.stdout" 2>"$fixture/unsafe.stderr"
) || unsafe_status=$?
[[ "$unsafe_status" -eq 1 ]]
[[ ! -s "$fixture/unsafe.stdout" ]]
rg -Fq 'catalogue has an unsafe model ID' "$fixture/unsafe.stderr"
[[ -z "$(find "$temporary" -mindepth 1 -print -quit)" ]]

printf 'PASS: provider-agnostic model commands\n'
