"""Safe, durable intake boundary for Watchdog event snapshots.

This module accepts data, never commands. Callers provide a mapping or exactly one JSON
object. Validation and journal commit always precede downstream Kanban work.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping

MAX_ENCODED_EVENT = 64 * 1024
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
TRUNCATION_SUFFIX = "…[truncated]"
DIAGNOSTIC_KEYS = frozenset({"source", "exit_code", "collector", "duration_ms"})
TOP_LEVEL_FIELDS = frozenset({"version", "mode", "observed_at", "message", "active_keys", "new_keys", "gone_keys", "text", "diagnostics"})


class IntakeError(ValueError):
    """Validation or journal failure; callers must not submit downstream work."""


@dataclass(frozen=True)
class CanonicalEvent:
    payload: dict[str, Any]
    event_id: str
    full_digest: str


@dataclass(frozen=True)
class AcceptedEvent:
    event_id: str
    inserted: bool


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _bounded_string(value: Any, limit: int, field: str, truncations: list[str], *, exact: bool = False) -> str:
    if not isinstance(value, str):
        raise IntakeError(f"invalid_{field}")
    if "\x00" in value or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise IntakeError(f"invalid_{field}_control")
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if exact:
        raise IntakeError(f"oversized_{field}")
    suffix = TRUNCATION_SUFFIX.encode("utf-8")
    retained = encoded[: limit - len(suffix)]
    while retained:
        try:
            truncations.append(field)
            return retained.decode("utf-8") + TRUNCATION_SUFFIX
        except UnicodeDecodeError as exc:
            retained = retained[:exc.start]
    raise IntakeError(f"oversized_{field}")


def _keys(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise IntakeError(f"invalid_{field}")
    if any(not isinstance(key, str) or not KEY_RE.fullmatch(key) for key in value):
        raise IntakeError(f"invalid_{field}")
    return sorted(set(value))


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise IntakeError("invalid_observed_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntakeError("invalid_observed_at") from exc
    if parsed.tzinfo is None or not 2000 <= parsed.year <= 2100:
        raise IntakeError("invalid_observed_at")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any, active: set[str], gone: set[str], truncations: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value) - {"summary", "new", "gone"}:
        raise IntakeError("invalid_text")
    result: dict[str, Any] = {}
    if "summary" in value:
        result["summary"] = _bounded_string(value["summary"], 2048, "text_summary", truncations)
    for name, permitted in (("new", active), ("gone", gone)):
        if name not in value:
            continue
        entries = value[name]
        if not isinstance(entries, Mapping) or len(entries) > 256:
            raise IntakeError(f"invalid_text_{name}")
        normalized = {}
        for key, detail in entries.items():
            if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                raise IntakeError(f"invalid_text_{name}")
            if key in permitted:
                normalized[key] = _bounded_string(detail, 1024, f"text_{name}", truncations)
        if normalized:
            result[name] = {key: normalized[key] for key in sorted(normalized)}
    return result


def _diagnostics(value: Any, truncations: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 32:
        raise IntakeError("invalid_diagnostics")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key not in DIAGNOSTIC_KEYS:
            raise IntakeError("invalid_diagnostics")
        if isinstance(item, str):
            result[key] = _bounded_string(item, 512, "diagnostics", truncations)
        elif item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
            result[key] = item
        else:
            raise IntakeError("invalid_diagnostics")
    return {key: result[key] for key in sorted(result)}


def canonicalize_event(raw: Mapping[str, Any]) -> CanonicalEvent:
    if not isinstance(raw, Mapping) or set(raw) - TOP_LEVEL_FIELDS:
        raise IntakeError("invalid_event")
    if raw.get("version") != 1 or isinstance(raw.get("version"), bool):
        raise IntakeError("invalid_version")
    mode = raw.get("mode")
    if mode not in {"check", "daily"}:
        raise IntakeError("invalid_mode")
    if "active_keys" not in raw:
        raise IntakeError("missing_active_keys")
    if mode == "check" and ("new_keys" not in raw or "gone_keys" not in raw):
        raise IntakeError("missing_transition_collection")
    if mode == "daily" and ("new_keys" in raw or "gone_keys" in raw):
        raise IntakeError("daily_transition_collection_forbidden")
    truncations: list[str] = []
    active = _keys(raw["active_keys"], "active_keys")
    new = _keys(raw.get("new_keys", []), "new_keys")
    gone = _keys(raw.get("gone_keys", []), "gone_keys")
    if not set(new).issubset(active) or set(gone) & set(active) or set(new) & set(gone):
        raise IntakeError("invalid_key_relationship")
    payload: dict[str, Any] = {
        "version": 1, "mode": mode, "observed_at": _timestamp(raw.get("observed_at")),
        "message": _bounded_string(raw.get("message"), 16384, "message", truncations, exact=True),
        "active_keys": active, "new_keys": new, "gone_keys": gone,
        "text": _text(raw.get("text"), set(active), set(gone), truncations),
        "diagnostics": _diagnostics(raw.get("diagnostics"), truncations),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if truncations:
        payload["normalization"] = {"truncated_fields": sorted(set(truncations))}
    return CanonicalEvent(payload, f"wd-v1-{digest[:32]}", digest)


def incident_key(stable_key: str) -> str:
    if not isinstance(stable_key, str) or not KEY_RE.fullmatch(stable_key):
        raise IntakeError("invalid_stable_key")
    digest = hashlib.sha256(b"wd-incident-v1\0" + stable_key.encode("ascii")).hexdigest()
    return f"wd-incident-v1-{stable_key}-{digest[:24]}"


def read_json_stdin(stream: BinaryIO) -> dict[str, Any]:
    encoded = stream.read(MAX_ENCODED_EVENT + 1)
    if len(encoded) > MAX_ENCODED_EVENT:
        raise IntakeError("oversized_encoded_event")
    try:
        text = encoded.decode("utf-8")
        trimmed = text.lstrip()
        value, end = json.JSONDecoder().raw_decode(trimmed)
        if trimmed[end:].strip():
            raise IntakeError("trailing_json_content")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntakeError("invalid_json") from exc
    if not isinstance(value, dict):
        raise IntakeError("invalid_json_object")
    return value


class Journal:
    """SQLite journal. Adapters consume only its pending delivery rows."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS watchdog_events (
          event_id TEXT PRIMARY KEY, full_digest TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
          received_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('accepted','submitting','submitted','failed')),
          attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, submitted_at TEXT);
        CREATE TABLE IF NOT EXISTS watchdog_incident_deliveries (
          event_id TEXT NOT NULL REFERENCES watchdog_events(event_id), stable_key TEXT NOT NULL, transition TEXT NOT NULL,
          incident_key TEXT NOT NULL, kanban_task_id TEXT, delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending','submitted','failed')),
          PRIMARY KEY(event_id, stable_key, transition));
        CREATE TABLE IF NOT EXISTS watchdog_incident_state (
          stable_key TEXT PRIMARY KEY, lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('open','gone')),
          incident_key TEXT NOT NULL UNIQUE, kanban_task_id TEXT, latest_applied_at TEXT NOT NULL,
          latest_applied_event_id TEXT NOT NULL REFERENCES watchdog_events(event_id), latest_applied_digest TEXT NOT NULL,
          recurrence_root_event_id TEXT REFERENCES watchdog_events(event_id), recurrence_version INTEGER NOT NULL DEFAULT 0);
        """)
        self.connection.commit()

    def _last_daily_active(self) -> set[str]:
        row = self.connection.execute(
            "SELECT payload_json FROM watchdog_events WHERE json_extract(payload_json, '$.mode')='daily' "
            "ORDER BY received_at DESC, event_id DESC LIMIT 1"
        ).fetchone()
        return set(json.loads(row[0])["active_keys"]) if row else set()

    def accept(self, raw: Mapping[str, Any]) -> AcceptedEvent:
        event = canonicalize_event(raw)
        with self.connection:
            existing = self.connection.execute("SELECT full_digest FROM watchdog_events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing[0] != event.full_digest:
                    raise IntakeError("event_id_collision")
                return AcceptedEvent(event.event_id, False)
            payload = dict(event.payload)
            if payload["mode"] == "daily":
                prior, current = self._last_daily_active(), set(payload["active_keys"])
                payload["new_keys"], payload["gone_keys"] = sorted(current - prior), sorted(prior - current)
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            self.connection.execute("INSERT INTO watchdog_events(event_id,full_digest,payload_json,received_at,state) VALUES(?,?,?,?,?)", (event.event_id, event.full_digest, _canonical_json(payload), now, "accepted"))
            for key in payload["new_keys"]:
                self.connection.execute("INSERT INTO watchdog_incident_deliveries(event_id,stable_key,transition,incident_key,delivery_state) VALUES(?,?,?,?,?)", (event.event_id, key, "NEW", incident_key(key), "pending"))
            for key in payload["gone_keys"]:
                self.connection.execute("INSERT INTO watchdog_incident_deliveries(event_id,stable_key,transition,incident_key,delivery_state) VALUES(?,?,?,?,?)", (event.event_id, key, "GONE", incident_key(key), "pending"))
        return AcceptedEvent(event.event_id, True)

    def pending_deliveries(self) -> list[tuple[str, str, str]]:
        return [tuple(row) for row in self.connection.execute("SELECT event_id,stable_key,transition FROM watchdog_incident_deliveries WHERE delivery_state IN ('pending','failed') ORDER BY event_id,stable_key,transition")]

    def acknowledge(self, event_id: str, stable_key: str, transition: str, task_id: str) -> None:
        if not task_id:
            raise IntakeError("invalid_task_id")
        with self.connection:
            if self.connection.execute("UPDATE watchdog_incident_deliveries SET kanban_task_id=?,delivery_state='submitted' WHERE event_id=? AND stable_key=? AND transition=?", (task_id, event_id, stable_key, transition)).rowcount != 1:
                raise IntakeError("unknown_delivery")

    def task_id(self, event_id: str, stable_key: str, transition: str) -> str | None:
        row = self.connection.execute("SELECT kanban_task_id FROM watchdog_incident_deliveries WHERE event_id=? AND stable_key=? AND transition=?", (event_id, stable_key, transition)).fetchone()
        return row[0] if row else None

    def event_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM watchdog_events").fetchone()[0])
