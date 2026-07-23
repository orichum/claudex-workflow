#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_root="$(mktemp -d /tmp/claudex-smoke-test.XXXXXX)"
trap 'rm -rf "$fixture_root"' EXIT

mkdir -p "$fixture_root/bin"
cp "$WORKFLOW_ROOT/smoke-test.sh" "$fixture_root/smoke-test.sh"

cat >"$fixture_root/bin/claudex-gpt" <<'EOF'
#!/usr/bin/env bash
fixture_workflow_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_data_root="${CLAUDEX_DATA_DIR:-$fixture_workflow_root/runtime}"
expanded_review_path="$fixture_workflow_root/controller/plugin/workflows/review.js"
controller_session_id='11111111-1111-4111-8111-111111111111'
mkdir -p "$fixture_data_root/state/sessions"
session_dir="$(mktemp -d "$fixture_data_root/state/sessions/run.XXXXXX")"
chmod 0700 "$session_dir"
jq -n '{
  schemaVersion: 1,
  stack: "fixture",
  controller: "provider/controller-model",
  agents: {
    "repository-explorer": "provider/exploration-model",
    "repository-verifier": "provider/shared-review-model",
    "correctness-critic": "provider/shared-review-model",
    "architecture-advisor": "provider/architecture-model",
    "implementation-worker": "provider/worker-model"
  }
}' >"$session_dir/effective-models.json"
chmod 0600 "$session_dir/effective-models.json"

printf 'warning: offline fixture diagnostic\n' >&2
if [[ " $* " != *" --output-format stream-json "* ]]; then
  provider_reply=CLAUDEX_ROUTED_PROVIDER_OK
  provider_model=provider/controller-model
  [[ "${SMOKE_PROVIDER_SUBSTITUTE:-0}" == 1 ]] && \
    provider_model=provider/unrelated-model
  provider_tokens=7
  [[ "${SMOKE_PROVIDER_ZERO:-0}" == 1 ]] && provider_tokens=0
  jq -cn \
    --arg reply "$provider_reply" \
    --arg model "$provider_model" \
    --argjson output_tokens "$provider_tokens" \
    '{type:"result",subtype:"success",is_error:false,result:$reply,modelUsage:{"provider/auxiliary-model":{outputTokens:3},($model):{outputTokens:$output_tokens}}}'
  exit 0
fi

case "${SMOKE_SCENARIO:-expanded}" in
  expanded|degraded|duplicate-record|unrelated-positive-substitute)
    jq -cn --arg path "$expanded_review_path" '{type:"assistant",message:{content:[{type:"tool_use",name:"Workflow",input:{scriptPath:$path,args:{subject:"controller",scope:"eight surfaces",highRisk:false}}}]}}'
    ;;
  duplicate-workflow)
    jq -cn --arg path "$expanded_review_path" '{type:"assistant",message:{content:[{type:"tool_use",name:"Workflow",input:{scriptPath:$path,args:{subject:"controller",scope:"eight surfaces",highRisk:false}}}]}}'
    jq -cn --arg path "$expanded_review_path" '{type:"assistant",message:{content:[{type:"tool_use",name:"Workflow",input:{scriptPath:$path,args:{subject:"controller",scope:"eight surfaces",highRisk:false}}}]}}'
    ;;
  literal)
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Workflow","input":{"scriptPath":"${CLAUDE_PLUGIN_ROOT}/workflows/review.js","args":{"subject":"controller","scope":"eight surfaces","highRisk":false}}}]}}'
    ;;
  name-only)
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Workflow","input":{"name":"review"}}]}}'
    ;;
  external)
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Workflow","input":{"scriptPath":"/tmp/review.js","args":{"subject":"controller","scope":"eight surfaces","highRisk":false}}}]}}'
    ;;
  inline)
    printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Workflow","input":{"scriptPath":"inline: return 1","args":{"subject":"controller","scope":"eight surfaces","highRisk":false}}}]}}'
    ;;
  high-risk)
    jq -cn --arg path "$expanded_review_path" '{type:"assistant",message:{content:[{type:"tool_use",name:"Workflow",input:{scriptPath:$path,args:{subject:"controller",scope:"eight surfaces",highRisk:true}}}]}}'
    ;;
  zero-usage|empty-usage)
    jq -cn --arg path "$expanded_review_path" '{type:"assistant",message:{content:[{type:"tool_use",name:"Workflow",input:{scriptPath:$path,args:{subject:"controller",scope:"eight surfaces",highRisk:false}}}]}}'
    ;;
  *)
    exit 92
    ;;
esac

workflow_status=complete
workflow_missing='[]'
if [[ "${SMOKE_SCENARIO:-expanded}" == degraded ]]; then
  workflow_status=degraded
  workflow_missing='[{"label":"critique","agentType":"claudex-controller:correctness-critic","reason":"missing-structured-result"}]'
fi
workflow_record_dir="$fixture_data_root/claude-config/projects/-fixture/$controller_session_id/workflows"
mkdir -p "$workflow_record_dir"
jq -cn \
  --arg status "$workflow_status" \
  --argjson missing "$workflow_missing" \
  '{status:"completed",result:{status:$status,missingAgents:$missing}}' \
  >"$workflow_record_dir/wf_fixture.json"
if [[ "${SMOKE_SCENARIO:-expanded}" == duplicate-record ]]; then
  cp "$workflow_record_dir/wf_fixture.json" "$workflow_record_dir/wf_duplicate.json"
fi

case "${SMOKE_SCENARIO:-expanded}" in
  zero-usage)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"provider/controller-model":{outputTokens:12},"provider/shared-review-model":{outputTokens:0}}}'
    ;;
  empty-usage)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"provider/controller-model":{},"provider/shared-review-model":{}}}'
    ;;
  unrelated-positive-substitute)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"provider/controller-model":{outputTokens:12},"provider/unrelated-model-a":{outputTokens:10},"provider/unrelated-model-b":{outputTokens:8}}}'
    ;;
  *)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"provider/controller-model":{outputTokens:12},"provider/shared-review-model":{outputTokens:10}}}'
    ;;
esac
EOF
chmod 0755 "$fixture_root/bin/claudex-gpt"
export CLAUDEX_DATA_DIR="$fixture_root/runtime"

assert_passes() {
  local scenario="$1"
  local actual_output
  actual_output="$(SMOKE_SCENARIO="$scenario" bash "$fixture_root/smoke-test.sh" controller)"
  [[ "$actual_output" == 'PASS: automatic controller selected the audited Workflow' ]]
}

assert_fails() {
  local scenario="$1"
  local actual_output status
  set +e
  actual_output="$(SMOKE_SCENARIO="$scenario" bash "$fixture_root/smoke-test.sh" controller 2>&1)"
  status=$?
  set -e
  [[ "$status" -ne 0 ]]
  [[ "$actual_output" != 'PASS: automatic controller selected the audited Workflow' ]]
}

assert_passes expanded
assert_passes literal
assert_fails name-only
assert_fails external
assert_fails inline
assert_fails high-risk
assert_fails zero-usage
assert_fails empty-usage
assert_fails unrelated-positive-substitute
assert_fails degraded
assert_fails duplicate-workflow
assert_fails duplicate-record

provider_output="$(bash "$fixture_root/smoke-test.sh" provider)"
[[ "$provider_output" == 'PASS: routed controller completed through Claudex with positive output usage' ]]
if bash "$fixture_root/smoke-test.sh" gpt >/dev/null 2>&1; then
  printf 'provider smoke still accepted the misleading gpt mode\n' >&2
  exit 1
fi
if bash "$fixture_root/smoke-test.sh" claude >/dev/null 2>&1; then
  printf 'provider smoke still accepted the misleading claude mode\n' >&2
  exit 1
fi
if SMOKE_PROVIDER_ZERO=1 \
    bash "$fixture_root/smoke-test.sh" provider >/dev/null 2>&1; then
  printf 'provider smoke accepted zero output usage\n' >&2
  exit 1
fi
if SMOKE_PROVIDER_SUBSTITUTE=1 \
    bash "$fixture_root/smoke-test.sh" provider >/dev/null 2>&1; then
  printf 'provider smoke accepted unrelated positive usage\n' >&2
  exit 1
fi

printf 'PASS: offline controller smoke fixtures\n'
