"""Pure request transformer for deferring non-resident client tools."""

from dataclasses import dataclass
import json


TOOL_SEARCH_TYPE = "tool_search_tool_regex_20251119"
TOOL_SEARCH_NAME = "tool_search_tool_regex"
MINIMUM_TOOLS = 12
VERIFIED_MODELS = frozenset(
    {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "claude-opus-4-8",
    }
)
RESIDENT_NAMES = frozenset(
    {
        "Bash",
        "mcp__leanctx__ctx_read",
        "mcp__leanctx__ctx_search",
        "mcp__leanctx__ctx_tree",
        "mcp__leanctx__ctx_graph",
        "mcp__leanctx__ctx_impact",
        "mcp__leanctx__ctx_callgraph",
        "mcp__leanctx__ctx_patch",
        "mcp__leanctx__ctx_shell",
        "mcp__leanctx__ctx_expand",
    }
)


@dataclass(frozen=True)
class TransformResult:
    body: bytes
    transformed: bool


def _logical_model(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.rsplit("/", 1)[-1]


def is_verified_tool_search_model(model: object) -> bool:
    return _logical_model(model) in VERIFIED_MODELS


def _is_tool_search(tool: object) -> bool:
    return isinstance(tool, dict) and tool.get("type") == TOOL_SEARCH_TYPE


def _is_deferred(tool: object) -> bool:
    return isinstance(tool, dict) and tool.get("defer_loading") is True


def _well_formed_tool(tool: object) -> bool:
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
        return False
    if "type" in tool:
        if not isinstance(tool["type"], str):
            return False
        return "input_schema" not in tool or isinstance(
            tool["input_schema"], dict
        )
    return isinstance(tool.get("input_schema"), dict)


def _eligible_client_tool(tool: object) -> bool:
    if not isinstance(tool, dict):
        return False
    if not isinstance(tool.get("name"), str):
        return False
    if not isinstance(tool.get("input_schema"), dict):
        return False
    return not isinstance(tool.get("type"), str)


def _preserve_cache_breakpoint(tools: list[object]) -> bool:
    if any(
        isinstance(tool, dict)
        and "cache_control" in tool
        and not isinstance(tool["cache_control"], dict)
        for tool in tools
    ):
        return False
    sources = [
        tool
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("defer_loading") is True
        and "cache_control" in tool
    ]
    if not sources:
        return True
    if len(sources) != 1:
        return False
    resident = next(
        (
            tool
            for tool in reversed(tools)
            if _eligible_client_tool(tool)
            and tool.get("name") in RESIDENT_NAMES
        ),
        None,
    )
    if resident is None or "cache_control" in resident:
        return False
    resident["cache_control"] = sources[0].pop("cache_control")
    return True


def transform_request(body: bytes) -> TransformResult:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        return TransformResult(body, False)
    if not isinstance(document, dict):
        return TransformResult(body, False)
    if not is_verified_tool_search_model(document.get("model")):
        return TransformResult(body, False)
    tools = document.get("tools")
    if not isinstance(tools, list) or len(tools) < MINIMUM_TOOLS:
        return TransformResult(body, False)
    if not all(_well_formed_tool(tool) for tool in tools):
        return TransformResult(body, False)
    if any(_is_tool_search(tool) or _is_deferred(tool) for tool in tools):
        return TransformResult(body, False)
    if not any(
        isinstance(tool, dict) and tool.get("name") in RESIDENT_NAMES
        for tool in tools
    ):
        return TransformResult(body, False)

    transformed = []
    for tool in tools:
        copied = dict(tool) if isinstance(tool, dict) else tool
        if _eligible_client_tool(copied) and copied.get("name") not in RESIDENT_NAMES:
            copied["defer_loading"] = True
        transformed.append(copied)
    if not any(
        isinstance(tool, dict) and tool.get("defer_loading") is True
        for tool in transformed
    ):
        return TransformResult(body, False)
    if not _preserve_cache_breakpoint(transformed):
        return TransformResult(body, False)
    transformed.append({"type": TOOL_SEARCH_TYPE, "name": TOOL_SEARCH_NAME})
    document["tools"] = transformed
    return TransformResult(
        json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
        True,
    )
