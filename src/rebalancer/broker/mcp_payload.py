"""Decode MCP tool responses as they arrive in Claude Code hook payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def decode(tool_response: Any) -> Any:
    """Best-effort JSON payload of an MCP tool response.

    Handles structuredContent, {"content": [text blocks]}, a bare list of text
    blocks, a JSON string, or an already-decoded object.
    """
    if isinstance(tool_response, Mapping):
        if "structuredContent" in tool_response:
            return tool_response["structuredContent"]
        if isinstance(tool_response.get("content"), list):
            return decode(tool_response["content"])
        return tool_response
    if (
        isinstance(tool_response, list)
        and all(isinstance(block, Mapping) and block.get("type") == "text" for block in tool_response)
        and tool_response
    ):
        return decode("".join(str(block.get("text", "")) for block in tool_response))
    if isinstance(tool_response, str):
        try:
            return json.loads(tool_response)
        except ValueError:
            return tool_response
    return tool_response


def find_order_id(payload: Any) -> str | None:
    """The broker order id in a place-order response, if one can be found."""
    if isinstance(payload, Mapping):
        for key in ("order_id", "id"):
            value = payload.get(key)
            if isinstance(value, str | int) and str(value):
                return str(value)
        nested = payload.get("order")
        if nested is not None:
            return find_order_id(nested)
    return None
