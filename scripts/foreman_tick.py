#!/usr/bin/env python3
"""Deterministic, bounded Foreman sweep over the shared default board."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


STATUSES = ("running", "blocked", "review", "ready")
VOLATILE_KEYS = {"now", "oldest_ready_age_seconds"}
BOARD_ENV_KEYS = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_DELEGATED_CHILD_CONTEXT",
)


def _stable(value):
    if isinstance(value, dict):
        return {
            key: _stable(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_KEYS
            and not key.endswith("_at")
            and "time" not in key
            and "age" not in key
            and "seconds" not in key
        }
    if isinstance(value, list):
        return sorted((_stable(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _command_env(default_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in BOARD_ENV_KEYS:
        env.pop(key, None)
    inherited_path = env.get("PATH", "")
    path_parts = [str(default_home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    path_parts.extend(part for part in inherited_path.split(os.pathsep) if part)
    env.update({
        "HERMES_HOME": str(default_home),
        "HERMES_PROFILE": "default",
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_DB": str(default_home / "kanban.db"),
        "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
    })
    return env


def _json_command(hermes: str, env: dict[str, str], *args: str):
    completed = subprocess.run(
        [hermes, *args], env=env, capture_output=True, text=True,
        check=False, timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "command failed")
    return json.loads(completed.stdout)


def _snapshot(hermes: str, env: dict[str, str]) -> str:
    lines = [
        json.dumps(_stable(_json_command(hermes, env, "kanban", "stats", "--json")), sort_keys=True),
        json.dumps(_stable(_json_command(hermes, env, "kanban", "diagnostics", "--json")), sort_keys=True),
    ]
    for status in STATUSES:
        rows = _json_command(hermes, env, "kanban", "list", "--status", status, "--json")
        projected = sorted(
            ({key: row.get(key) for key in ("id", "assignee", "status", "title")} for row in rows),
            key=lambda row: str(row.get("id")),
        )
        lines.append(json.dumps(projected, sort_keys=True))
    return "\n".join(lines) + "\n"


def main() -> int:
    default_home = Path(os.environ.get("FOREMAN_DEFAULT_HOME", Path.home() / ".hermes")).resolve()
    state = Path(os.environ.get("FOREMAN_STATE_DIR", default_home / "state" / "foreman_sweep"))
    state.mkdir(parents=True, exist_ok=True)
    env = _command_env(default_home)
    hermes = os.environ.get("FOREMAN_HERMES_BIN") or shutil.which("hermes", path=env["PATH"]) or "hermes"
    timeout = int(os.environ.get("FOREMAN_CHAT_TIMEOUT_SECONDS", "480"))

    lock = (state / "lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    try:
        snapshot = _snapshot(hermes, env)
        digest = hashlib.sha256(snapshot.encode()).hexdigest()[:16]
        last_hash = state / "last_hash"
        if last_hash.exists() and last_hash.read_text().strip() == digest:
            return 0

        prompt = (
            "Sweep. Board snapshot follows. Apply your SOUL: find stalled or blocked work, "
            "decide what is yours to decide, comment and unblock, escalate only tier 3. "
            "Reply [SILENT] if nothing needs a decision.\n\n" + snapshot
        )
        try:
            completed = subprocess.run(
                [hermes, "-p", "foreman", "chat", "-q", prompt],
                env=env, capture_output=True, text=True, check=False, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"fleet-foreman-sweep: foreman chat timed out after {timeout}s", file=sys.stderr)
            return 1
        if completed.returncode:
            print(completed.stderr.strip() or completed.stdout.strip() or "foreman chat failed", file=sys.stderr)
            return completed.returncode

        last_hash.write_text(digest + "\n")
        output = completed.stdout.strip()
        if output and output != "[SILENT]":
            print(output)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"fleet-foreman-sweep: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
