"""Pure Orichum contract for the bounded LeanCTX MCP surface."""

from pathlib import Path


AUTO_APPROVED_TOOLS = (
    "ctx_read",
    "ctx_delta",
    "ctx_search",
    "ctx_glob",
    "ctx_tree",
    "ctx_outline",
    "ctx_explore",
    "ctx_expand",
)
TOOLS = (*AUTO_APPROVED_TOOLS, "ctx_shell")

_CONFIG = """compression_level = "lite"
minimal_overhead = true
tools_enabled = ["ctx_read", "ctx_delta", "ctx_search", "ctx_glob", "ctx_tree", "ctx_outline", "ctx_explore", "ctx_expand", "ctx_shell"]
disabled_tools = ["ctx_call"]
auto_capture = false
buddy_enabled = false
enable_wakeup_ctx = false
journal_enabled = false
max_index_threads = 2
no_degrade = true
prefer_native_editor = true
proxy_enabled = false
rules_injection = "off"
shadow_mode = false
shell_activation = "off"
shell_hook_disabled = true
update_check_disabled = true
"""


def config_bytes() -> bytes:
    """Return the exact private LeanCTX configuration for one session."""
    return _CONFIG.encode("utf-8")


def mcp_server(
    binary: Path,
    project_root: Path,
    session_dir: Path,
) -> dict[str, object]:
    """Build one headless, project-jailed LeanCTX stdio server entry."""
    for path in (binary, project_root, session_dir):
        if not path.is_absolute():
            raise ValueError("LeanCTX paths must be absolute")
    isolated = str(session_dir)
    return {
        "command": str(binary),
        "args": [],
        "env": {
            "LEAN_CTX_ALLOW_REROOT": "false",
            "LEAN_CTX_AUTONOMY": "false",
            "LEAN_CTX_BYPASS_HINTS": "off",
            "LEAN_CTX_CACHE_DIR": isolated,
            "LEAN_CTX_CONFIG_DIR": isolated,
            "LEAN_CTX_DATA_DIR": isolated,
            "LEAN_CTX_FULL_TOOLS": "0",
            "LEAN_CTX_HEADLESS": "1",
            "LEAN_CTX_MINIMAL": "1",
            "LEAN_CTX_PROJECT_ROOT": str(project_root),
            "LEAN_CTX_STATE_DIR": isolated,
        },
    }
