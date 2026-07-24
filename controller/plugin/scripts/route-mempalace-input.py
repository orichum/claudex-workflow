#!/usr/bin/env python3
"""Bind MemPalace wing-aware calls to the verified launch context."""

import copy
import json
import os
import sys
from pathlib import Path

WORKFLOW_SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKFLOW_SOURCE))

from integrations.common.session_config import SessionError, verify_context_binding


WING_TOOLS = {
    "mempalace_list_rooms",
    "mempalace_list_tunnels",
    "mempalace_list_hallways",
    "mempalace_follow_tunnels",
    "mempalace_search",
    "mempalace_add_drawer",
    "mempalace_mine",
    "mempalace_sync",
    "mempalace_list_drawers",
    "mempalace_update_drawer",
    "mempalace_diary_write",
    "mempalace_diary_read",
}

GLOBAL_TOOLS = {
    "mempalace_get_taxonomy",
}


def emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def deny() -> int:
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "MemPalace call is not bound to a verified Orichum project context",
    }})
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return deny()
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
            return deny()

        workflow_root = Path(os.environ["CLAUDEX_WORKFLOW_ROOT"])
        run_dir = Path(os.environ["CLAUDEX_RUN_DIR"])
        run_id = os.environ["CLAUDEX_RUN_ID"]
        context_file = Path(os.environ["CLAUDEX_CONTEXT_FILE"])
        digest = os.environ["CLAUDEX_CONTEXT_SHA256"]
        data_value = os.environ.get("CLAUDEX_DATA_DIR")
        binding = verify_context_binding(
            workflow_root, run_dir, context_file, digest, run_id,
            Path(data_value) if data_value else None,
        )
        route = binding.context.get("route")
        if not isinstance(route, dict) or route.get("memoryAvailable") is not True:
            return deny()
        wing = route.get("memoryWing")
        if not isinstance(wing, str) or not wing:
            return deny()
    except (KeyError, OSError, ValueError, json.JSONDecodeError, SessionError):
        return deny()

    prefix = "mcp__mempalace__"
    if not tool_name.startswith(prefix):
        return 0
    short_name = tool_name[len(prefix):]
    updated = copy.deepcopy(tool_input)
    changed = False
    if short_name in WING_TOOLS:
        updated["wing"] = wing
        changed = True
    elif short_name == "mempalace_checkpoint":
        items = updated.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item["wing"] = wing
                    changed = True
        diary = updated.get("diary")
        if isinstance(diary, dict):
            diary["wing"] = wing
            changed = True
    elif short_name in GLOBAL_TOOLS:
        return 0
    else:
        return deny()

    if changed:
        emit({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        }})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
