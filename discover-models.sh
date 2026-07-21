#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"

models_file="$WORKFLOW_ROOT/runtime/models.json.new"
config_file="$WORKFLOW_ROOT/runtime/claudex.toml.new"
trap 'rm -f "$models_file" "$config_file"' EXIT

curl --fail --silent --show-error http://127.0.0.1:8317/v1/models >"$models_file"
chmod 0600 "$models_file"
render_discovered_claudex_config "$models_file" "$config_file"
chmod 0600 "$config_file"
"$WORKFLOW_ROOT/bin/claudex" --config "$config_file" config validate >/dev/null

backup_timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="$WORKFLOW_ROOT/backups/$backup_timestamp"
install -d -m 0700 "$backup_dir"
if [[ -f "$WORKFLOW_ROOT/runtime/claudex.toml" ]]; then
  backup_path "$WORKFLOW_ROOT/runtime/claudex.toml" "$backup_dir" "claudex.toml"
fi
printf '%s\n' "$backup_dir" >"$WORKFLOW_ROOT/runtime/last-claudex-backup"
chmod 0600 "$WORKFLOW_ROOT/runtime/last-claudex-backup"

mv "$models_file" "$WORKFLOW_ROOT/runtime/models.json"
mv "$config_file" "$WORKFLOW_ROOT/runtime/claudex.toml"
trap - EXIT

printf 'Configured one dual-provider profile (backup: %s):\n' "$backup_dir"
rg '^(default_model|haiku|sonnet|opus) = ' "$WORKFLOW_ROOT/runtime/claudex.toml"
