"""Regression coverage for the bounded fleet foreman sweep script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "foreman_tick.py"


def _fake_hermes(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "hermes"
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys, time\n"
        "with open(os.environ['CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env': {k: os.environ.get(k) for k in "
        "['HERMES_HOME', 'HERMES_PROFILE', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_DB', "
        "'HERMES_KANBAN_TASK', 'HERMES_KANBAN_LOGS_ROOT', "
        "'HERMES_KANBAN_DISPATCH_IN_GATEWAY', 'HERMES_DELEGATED_CHILD_CONTEXT', "
        "'HERMES_TENANT']}}) + '\\n')\n"
        "if sys.argv[1:3] == ['kanban', 'stats']:\n"
        "    print(json.dumps({'now': 'volatile', 'ready': 1}))\n"
        "elif sys.argv[1:3] == ['kanban', 'diagnostics']:\n"
        "    print('[]')\n"
        "elif sys.argv[1:3] == ['kanban', 'list']:\n"
        "    print(json.dumps([{'id': 't1', 'title': 'x', 'assignee': 'foreman', "
        "'status': sys.argv[sys.argv.index('--status') + 1], 'created_at': 1, "
        "'priority': int(os.environ.get('FAKE_PRIORITY', '1'))}]))\n"
        "elif 'chat' in sys.argv:\n"
        "    time.sleep(float(os.environ.get('FAKE_CHAT_SLEEP', '0')))\n"
        "    print(os.environ.get('FAKE_CHAT_OUTPUT', '[SILENT]'))\n"
    )
    fake.chmod(0o755)
    return fake, calls


def _run(tmp_path: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    fake, calls = _fake_hermes(tmp_path)
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "CALLS": str(calls),
        "FOREMAN_HERMES_BIN": str(fake),
        "FOREMAN_STATE_DIR": str(tmp_path / "state"),
        "FOREMAN_CHAT_TIMEOUT_SECONDS": "2",
        "HERMES_KANBAN_TASK": "t_parent",
        "HERMES_KANBAN_LOGS_ROOT": str(tmp_path / "logs"),
        "HERMES_KANBAN_DISPATCH_IN_GATEWAY": "1",
        "HERMES_DELEGATED_CHILD_CONTEXT": str(tmp_path / ".hermes"),
        "HERMES_TENANT": "inherited-tenant",
    })
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True,
        timeout=10, check=False,
    )


def test_sweep_reads_default_board_without_inherited_worker_fence(tmp_path):
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    board_calls = [call for call in calls if call["argv"][:1] == ["kanban"]]
    assert len(board_calls) == 6
    assert all(call["env"]["HERMES_HOME"] == str(tmp_path / ".hermes") for call in board_calls)
    assert all(call["env"]["HERMES_PROFILE"] == "default" for call in board_calls)
    assert all(call["env"]["HERMES_KANBAN_TASK"] is None for call in board_calls)
    assert all(call["env"]["HERMES_KANBAN_LOGS_ROOT"] is None for call in board_calls)
    assert all(call["env"]["HERMES_KANBAN_DISPATCH_IN_GATEWAY"] is None for call in board_calls)
    assert all(call["env"]["HERMES_DELEGATED_CHILD_CONTEXT"] is None for call in board_calls)
    assert all(call["env"]["HERMES_TENANT"] is None for call in board_calls)


def test_silent_chat_is_success_and_same_snapshot_skips_next_chat(tmp_path):
    first = _run(tmp_path, FAKE_CHAT_OUTPUT="plain output without banner")
    second = _run(tmp_path, FAKE_CHAT_OUTPUT="must not be emitted")

    assert first.returncode == second.returncode == 0
    assert first.stdout == "plain output without banner\n"
    assert second.stdout == ""
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum("chat" in call["argv"] for call in calls) == 1


def test_board_state_change_outside_summary_fields_triggers_chat(tmp_path):
    first = _run(tmp_path, FAKE_PRIORITY="1")
    second = _run(tmp_path, FAKE_PRIORITY="2")

    assert first.returncode == second.returncode == 0
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum("chat" in call["argv"] for call in calls) == 2


def test_chat_timeout_is_bounded_and_snapshot_retries(tmp_path):
    first = _run(tmp_path, FOREMAN_CHAT_TIMEOUT_SECONDS="1", FAKE_CHAT_SLEEP="3")
    second = _run(tmp_path)

    assert first.returncode == 1
    assert "timed out after 1s" in first.stderr
    assert second.returncode == 0
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum("chat" in call["argv"] for call in calls) == 2


def test_overlapping_sweep_exits_without_reading_board(tmp_path):
    fake, calls = _fake_hermes(tmp_path)
    state = tmp_path / "state"
    state.mkdir()

    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "CALLS": str(calls),
        "FOREMAN_HERMES_BIN": str(fake),
        "FOREMAN_STATE_DIR": str(state),
    })
    holder_env = env | {"FAKE_CHAT_SLEEP": "2"}
    holder = subprocess.Popen(
        [sys.executable, str(SCRIPT)], env=holder_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 3
    while not calls.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls.exists(), "first sweep did not acquire the lock"

    result = subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True,
        timeout=3, check=False,
    )
    holder.communicate(timeout=5)

    assert result.returncode == 0
    recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
    assert sum("chat" in call["argv"] for call in recorded) == 1
