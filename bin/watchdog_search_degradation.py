"""Classify degraded G-brain search results stored in Hermes state.db."""
from __future__ import annotations

import sqlite3

DEGRADATION_MARKER = "keyword_only_no_embedding_provider"


def _is_gbrain_search_tool(tool_name: str | None) -> bool:
    """Accept persisted MCP name variants for G-brain search/query tools."""
    if not tool_name:
        return False
    normalized = tool_name.lower().replace("__", "_")
    return normalized in {"mcp_gbrain_search", "mcp_gbrain_query"}


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
        for tool_name, _content in rows
        if _is_gbrain_search_tool(tool_name)
    )
