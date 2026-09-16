#!/usr/bin/env python3
"""Build and durably dispatch Watchdog snapshots without shell interpolation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Any

MAX_EVENT_BYTES = 64 * 1024
MAX_SPOOL_FILES = 1024


def _encoded(event: dict[str, Any]) -> bytes:
    data = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_EVENT_BYTES:
        raise ValueError("event exceeds 64 KiB")
    return data


def _identity(data: bytes) -> str:
    parsed = json.loads(data)
    canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _submit(launcher: Path, data: bytes) -> bool:
    try:
        result = subprocess.run([str(launcher)], input=data, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def dispatch(event: dict[str, Any], *, launcher: Path, spool: Path,
             max_files: int = MAX_SPOOL_FILES) -> bool:
    """Spool before delivery, retry oldest first, and retain every failed event."""
    data = _encoded(event)
    digest = _identity(data)
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(spool, 0o700)
    destination = spool / f"{digest}.json"
    existing = sorted(spool.glob("*.json"))
    if not destination.exists() and len(existing) >= max_files:
        return False
    if not destination.exists():
        _atomic_write(destination, data)

    all_ok = True
    for queued in sorted(spool.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name)):
        queued_data = queued.read_bytes()
        if len(queued_data) > MAX_EVENT_BYTES or _identity(queued_data) != queued.stem:
            all_ok = False
            continue
        if _submit(launcher, queued_data):
            queued.unlink()
        else:
            all_ok = False
            break
    return all_ok and not any(spool.glob("*.json"))


def _read_state(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return [str(key) for key in value.get("active", [])]
    except (OSError, ValueError, TypeError):
        return []


def _write_state(path: Path, active: list[str], observed_at: str) -> None:
    _atomic_write(path, json.dumps({"active": active, "updated_at": observed_at}, indent=2).encode("utf-8"))


def event_from_snapshot(snapshot: dict[str, Any], state_path: Path) -> dict[str, Any]:
    mode = snapshot["mode"]
    active = sorted(set(snapshot.get("active_keys", [])))
    new = sorted(set(snapshot.get("new_keys", [])))
    gone = sorted(set(snapshot.get("gone_keys", [])))
    observed_at = snapshot.get("observed_at") or datetime.now().astimezone().isoformat()
    event: dict[str, Any] = {
        "version": 1,
        "mode": "check" if mode == "--check" else "daily",
        "observed_at": observed_at,
        "message": snapshot["message"],
        "active_keys": active,
        "text": {
            "summary": snapshot["message"][:2048],
            "new": {key: snapshot.get("texts", {}).get(key, "") for key in new},
            "gone": {key: key for key in gone},
        },
        "diagnostics": {"source": "watchdog.sh", "collector": "watchdog"},
    }
    if event["mode"] == "check":
        event["new_keys"] = new
        event["gone_keys"] = gone
    return event


def _snapshot_from_nul(stream: Any) -> dict[str, Any]:
    encoded = stream.buffer.read(MAX_EVENT_BYTES + 1)
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("snapshot exceeds 64 KiB")
    fields = encoded.decode("utf-8").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) < 5:
        raise ValueError("invalid snapshot protocol")
    mode, message, new_text, gone_text, count_text = fields[:5]
    count = int(count_text)
    expected = 5 + count * 2
    if len(fields) != expected:
        raise ValueError("invalid snapshot field count")
    texts = {fields[index]: fields[index + 1] for index in range(5, expected, 2)}
    return {"mode": mode, "message": message, "active_keys": list(texts), "texts": texts,
            "new_keys": [key for key in new_text.split(",") if key],
            "gone_keys": [key for key in gone_text.split(",") if key]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--fixture-json", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = json.load(sys.stdin) if args.fixture_json else _snapshot_from_nul(sys.stdin)
        event = event_from_snapshot(snapshot, args.state)
        success = dispatch(event, launcher=args.launcher, spool=args.spool)
        if success:
            _write_state(args.state, event["active_keys"], event["observed_at"])
        return 0 if success else 1
    except Exception as exc:
        print(f"watchdog dispatch failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
