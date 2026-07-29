"""Immutable LeanCTX provider-residency profiles."""

from collections.abc import Collection


LEANCTX_PROFILE_LEAN = "lean"
LEANCTX_PROFILE_FULL = "full"
DEFAULT_LEANCTX_PROFILE = LEANCTX_PROFILE_LEAN
LEANCTX_PROFILES = (LEANCTX_PROFILE_LEAN, LEANCTX_PROFILE_FULL)

LEAN_RESIDENT_NAMES = frozenset(
    {
        "mcp__leanctx__ctx_read",
        "mcp__leanctx__ctx_search",
        "mcp__leanctx__ctx_tree",
        "mcp__leanctx__ctx_shell",
    }
)
FULL_RESIDENT_NAMES = frozenset(
    {
        *LEAN_RESIDENT_NAMES,
        "mcp__leanctx__ctx_graph",
        "mcp__leanctx__ctx_impact",
        "mcp__leanctx__ctx_callgraph",
        "mcp__leanctx__ctx_patch",
        "mcp__leanctx__ctx_expand",
    }
)
ALL_LEANCTX_NAMES = frozenset(
    {
        *FULL_RESIDENT_NAMES,
        "mcp__leanctx__ctx_knowledge",
        "mcp__leanctx__ctx_overview",
    }
)


def validate_leanctx_profile(value: object) -> str:
    """Return one supported profile or reject the value."""
    if not isinstance(value, str) or value not in LEANCTX_PROFILES:
        raise ValueError("LeanCTX profile must be lean or full")
    return value


def resident_tool_names(profile: object) -> Collection[str]:
    """Return the exact provider-resident tool names for one profile."""
    selected = validate_leanctx_profile(profile)
    return (
        LEAN_RESIDENT_NAMES
        if selected == LEANCTX_PROFILE_LEAN
        else FULL_RESIDENT_NAMES
    )
