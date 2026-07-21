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
expanded_review_path="$fixture_workflow_root/controller/plugin/workflows/review.js"
controller_session_id='11111111-1111-4111-8111-111111111111'

printf 'warning: offline fixture diagnostic\n' >&2
if [[ " $* " != *" --output-format stream-json "* ]]; then
  if [[ " $* " == *" --model opus "* ]]; then
    provider_reply=CLAUDEX_CLAUDE_OK
    provider_model=claude-opus-4-8
  else
    provider_reply=CLAUDEX_GPT_OK
    provider_model=gpt-5.6-sol
  fi
  provider_tokens=7
  [[ "${SMOKE_PROVIDER_ZERO:-0}" == 1 ]] && provider_tokens=0
  jq -cn \
    --arg reply "$provider_reply" \
    --arg model "$provider_model" \
    --argjson output_tokens "$provider_tokens" \
    '{type:"result",subtype:"success",is_error:false,result:$reply,modelUsage:{"claude-haiku-4-5-20251001":{outputTokens:3},($model):{outputTokens:$output_tokens}}}'
  exit 0
fi

case "${SMOKE_SCENARIO:-expanded}" in
  expanded|degraded|duplicate-record)
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
  workflow_missing='[{"label":"critique","agentType":"claudex-controller:sonnet-critic","reason":"missing-structured-result"}]'
fi
workflow_record_dir="$fixture_workflow_root/runtime/claude-config/projects/-fixture/$controller_session_id/workflows"
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
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"gpt-5.6-sol":{outputTokens:12},"gpt-5.6-terra":{outputTokens:0},"claude-sonnet-5":{outputTokens:8}}}'
    ;;
  empty-usage)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"gpt-5.6-sol":{},"gpt-5.6-terra":{},"claude-sonnet-5":{}}}'
    ;;
  *)
    jq -cn --arg session_id "$controller_session_id" '{type:"result",subtype:"success",is_error:false,session_id:$session_id,modelUsage:{"gpt-5.6-sol":{outputTokens:12},"gpt-5.6-terra":{outputTokens:10},"claude-sonnet-5":{outputTokens:8}}}'
    ;;
esac
EOF
chmod 0755 "$fixture_root/bin/claudex-gpt"

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
assert_fails degraded
assert_fails duplicate-workflow
assert_fails duplicate-record

gpt_output="$(bash "$fixture_root/smoke-test.sh" gpt)"
[[ "$gpt_output" == 'PASS: Claude Code completed through Claudex on gpt-5.6-sol' ]]
claude_output="$(bash "$fixture_root/smoke-test.sh" claude)"
[[ "$claude_output" == 'PASS: Claude Code completed through Claudex on claude-opus-4-8' ]]
if SMOKE_PROVIDER_ZERO=1 bash "$fixture_root/smoke-test.sh" gpt >/dev/null 2>&1; then
  printf 'provider smoke accepted zero output usage for the expected model\n' >&2
  exit 1
fi

printf 'PASS: offline controller smoke fixtures\n'
