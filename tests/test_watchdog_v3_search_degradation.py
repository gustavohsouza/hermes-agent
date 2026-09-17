from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

MODULE_PATH = Path(__file__).parents[1] / "bin" / "watchdog_search_degradation.py"
SPEC = importlib.util.spec_from_file_location("watchdog_search_degradation", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load watchdog_search_degradation")
wd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wd)

MARKER = "keyword_only_no_embedding_provider"


@pytest.fixture
def messages_db():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "create table messages (role text, timestamp real, tool_name text, content text)"
    )
    yield connection
    connection.close()


def insert_message(connection, *, tool_name, content, timestamp=200.0):
    connection.execute(
        "insert into messages(role, timestamp, tool_name, content) values (?, ?, ?, ?)",
        ("tool", timestamp, tool_name, content),
    )


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "terminal", "search_files", "delegate_task"],
)
def test_quoted_marker_from_non_gbrain_tools_is_ignored(messages_db, tool_name):
    insert_message(
        messages_db,
        tool_name=tool_name,
        content=f"prior logs and source quote {MARKER}",
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


@pytest.mark.parametrize(
    ("tool_name", "content"),
    [
        (
            "mcp__gbrain__search",
            json.dumps({"_meta": {"retrieval": {"degraded": [MARKER]}}}),
        ),
        (
            "mcp_gbrain_query",
            json.dumps(
                {
                    "result": json.dumps(
                        {"_meta": {"retrieval": {"degraded": [MARKER]}}}
                    )
                }
            ),
        ),
        (
            "mcp__gbrain__search",
            '<untrusted_tool_result source="mcp__gbrain__search">\n'
            "The following content was retrieved from an external source.\n\n"
            + json.dumps(
                {
                    "result": "[]",
                    "_meta": {"retrieval": {"degraded": [MARKER]}},
                }
            )
            + "\n</untrusted_tool_result>",
        ),
    ],
)
def test_gbrain_search_and_query_result_encodings_are_counted(
    messages_db, tool_name, content
):
    insert_message(messages_db, tool_name=tool_name, content=content)

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 1


def test_healthy_gbrain_result_is_ignored(messages_db):
    insert_message(
        messages_db,
        tool_name="mcp__gbrain__search",
        content=json.dumps(
            {"_meta": {"retrieval": {"vector_enabled": True, "degraded": []}}}
        ),
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


def test_healthy_gbrain_result_quoting_marker_is_ignored(messages_db):
    insert_message(
        messages_db,
        tool_name="mcp__gbrain__search",
        content=json.dumps(
            {
                "result": [{"text": f"prior incident mentioned {MARKER}"}],
                "_meta": {
                    "retrieval": {"vector_enabled": True, "degraded": []}
                },
            }
        ),
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


def test_gbrain_tool_call_quoting_marker_is_ignored(messages_db):
    insert_message(
        messages_db,
        tool_name="mcp__gbrain__search",
        content=f'[tool_call]\n{{"query": "find {MARKER}"}}',
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


def test_unrelated_degraded_field_is_ignored(messages_db):
    insert_message(
        messages_db,
        tool_name="mcp__gbrain__search",
        content=json.dumps(
            {
                "result": {"degraded": [MARKER]},
                "_meta": {"retrieval": {"degraded": []}},
            }
        ),
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


def test_old_degraded_gbrain_result_is_ignored(messages_db):
    insert_message(
        messages_db,
        tool_name="mcp__gbrain__search",
        content=MARKER,
        timestamp=99.0,
    )

    assert wd.count_degraded_search_results(messages_db, since=100.0) == 0


def test_watchdog_v3_uses_provenance_aware_detector():
    source = (Path(__file__).parents[1] / "bin" / "watchdog_v3.py").read_text()

    assert "n = count_degraded_search_results(con, since=since)" in source
    assert "content like '%keyword_only_no_embedding_provider%'" not in source
