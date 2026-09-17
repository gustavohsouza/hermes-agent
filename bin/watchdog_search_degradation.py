"""Classify degraded G-brain search results stored in Hermes state.db."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

DEGRADATION_MARKER = "keyword_only_no_embedding_provider"


def _is_gbrain_search_tool(tool_name: str | None) -> bool:
    """Accept persisted MCP name variants for G-brain search/query tools."""
    if not tool_name:
        return False
    normalized = tool_name.lower().replace("__", "_")
    return normalized in {"mcp_gbrain_search", "mcp_gbrain_query"}


def _unwrap_tool_result(content: str) -> str | None:
    """Return the serialized MCP payload, excluding persisted call summaries."""
    if content.startswith("[tool_call]"):
        return None
    if content.startswith("<untrusted_tool_result"):
        start = content.find("\n\n")
        end = content.rfind("\n</untrusted_tool_result")
        if start < 0 or end < 0:
            return None
        return content[start + 2 : end]
    return content


def _json_values(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_values(child)
    elif isinstance(value, str) and value[:1] in "[{":
        try:
            nested = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _json_values(nested)


def _has_degradation_metadata(content: str) -> bool:
    payload = _unwrap_tool_result(content)
    if payload is None:
        return False
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return any(
        isinstance(value, dict)
        and isinstance(value.get("degraded"), list)
        and DEGRADATION_MARKER in value["degraded"]
        for value in _json_values(decoded)
    )


def count_degraded_search_results(
    connection: sqlite3.Connection,
    *,
    since: float,
) -> int:
    """Count genuine recent G-brain search/query results carrying degradation."""
    rows = connection.execute(
        """select tool_name, content
           from messages
           where role = 'tool'
             and timestamp > ?
             and content like ?""",
        (since, f"%{DEGRADATION_MARKER}%"),
    )
    return sum(
        1
        for tool_name, content in rows
        if _is_gbrain_search_tool(tool_name)
        and _has_degradation_metadata(content)
    )
