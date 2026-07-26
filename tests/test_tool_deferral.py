import json
import unittest

from integrations.common.tool_deferral import (
    TOOL_SEARCH_NAME,
    TOOL_SEARCH_TYPE,
    transform_request,
)


def client_tool(name: str) -> dict:
    return {
        "name": name,
        "description": name,
        "input_schema": {"type": "object", "properties": {}},
    }


def client_tools(count: int, prefix: str = "tool_") -> list[dict]:
    return [client_tool(f"{prefix}{index}") for index in range(count)]


def request(model: str, tools: list[dict]) -> bytes:
    return json.dumps(
        {"model": model, "messages": [], "tools": tools},
        separators=(",", ":"),
    ).encode()


class ToolDeferralTests(unittest.TestCase):
    def test_unknown_model_is_byte_preserving(self) -> None:
        body = request("future-model", client_tools(20))
        result = transform_request(body)
        self.assertFalse(result.transformed)
        self.assertIs(result.body, body)

    def test_fewer_than_twelve_tools_is_byte_preserving(self) -> None:
        body = request("gpt-5.6-sol", client_tools(11))
        result = transform_request(body)
        self.assertFalse(result.transformed)
        self.assertIs(result.body, body)

    def test_existing_tool_search_is_byte_preserving(self) -> None:
        tools = client_tools(12) + [
            {"type": "tool_search_tool_regex_20251119", "name": "tool_search"}
        ]
        body = request("gpt-5.6-sol", tools)
        result = transform_request(body)
        self.assertFalse(result.transformed)
        self.assertIs(result.body, body)

    def test_existing_deferred_tool_is_byte_preserving(self) -> None:
        tools = client_tools(12)
        tools[-1]["defer_loading"] = True
        body = request("gpt-5.6-sol", tools)
        result = transform_request(body)
        self.assertFalse(result.transformed)
        self.assertIs(result.body, body)

    def test_specialized_client_tools_are_deferred(self) -> None:
        tools = [
            client_tool("mcp__leanctx__ctx_read"),
            client_tool("mcp__leanctx__ctx_patch"),
            client_tool("Bash"),
            client_tool("Read"),
            client_tool("mcp__graphify__query"),
            *client_tools(8, prefix="mcp__docker__tool_"),
        ]
        result = transform_request(request("gpt-5.6-sol", tools))
        document = json.loads(result.body)
        by_name = {
            tool.get("name"): tool
            for tool in document["tools"]
            if isinstance(tool, dict) and "name" in tool
        }
        self.assertTrue(result.transformed)
        self.assertNotIn("defer_loading", by_name["mcp__leanctx__ctx_read"])
        self.assertNotIn("defer_loading", by_name["mcp__leanctx__ctx_patch"])
        self.assertNotIn("defer_loading", by_name["Bash"])
        self.assertTrue(by_name["Read"]["defer_loading"])
        self.assertTrue(by_name["mcp__graphify__query"]["defer_loading"])
        self.assertEqual(document["tools"][-1]["type"], TOOL_SEARCH_TYPE)

    def test_server_tool_is_never_deferred(self) -> None:
        tools = client_tools(12)
        tools.append({"type": "web_search_20250305", "name": "web_search"})
        document = json.loads(
            transform_request(request("gpt-5.6-sol", tools)).body
        )
        server_tool = next(
            tool for tool in document["tools"]
            if tool.get("type") == "web_search_20250305"
        )
        self.assertNotIn("defer_loading", server_tool)

    def test_cache_control_moves_to_last_resident_tool(self) -> None:
        tools = [client_tool("Bash"), *client_tools(11)]
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        document = json.loads(
            transform_request(request("gpt-5.6-sol", tools)).body
        )
        resident = [
            tool for tool in document["tools"]
            if not tool.get("defer_loading") and tool.get("name") != TOOL_SEARCH_NAME
        ]
        self.assertEqual(resident[-1]["cache_control"], {"type": "ephemeral"})

    def test_transform_is_idempotent(self) -> None:
        first = transform_request(
            request(
                "gpt-5.6-sol",
                [client_tool("Bash"), *client_tools(11)],
            )
        )
        second = transform_request(first.body)
        self.assertFalse(second.transformed)
        self.assertIs(second.body, first.body)

    def test_invalid_json_is_byte_preserving(self) -> None:
        body = b"{"
        result = transform_request(body)
        self.assertFalse(result.transformed)
        self.assertIs(result.body, body)
