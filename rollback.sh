#!/usr/bin/env bash
set -euo pipefail

WORKFLOW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/workflow.sh
source "$WORKFLOW_ROOT/lib/workflow.sh"
USER_BIN_DIR="${USER_BIN_DIR:-$HOME/.local/bin}"

remove_managed_symlink \
  "$USER_BIN_DIR/claudex-gpt" \
  "$WORKFLOW_ROOT/bin/claudex-gpt"

printf 'Workflow-owned integration launch is disabled. Shared CLIProxyAPI, launch agent, other launchers, session state, and persistent integration effects were retained.\n'
