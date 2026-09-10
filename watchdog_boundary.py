"""Safe, durable intake boundary for Watchdog event snapshots."""
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
RFC3339_OFFSET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
TRUNCATION_SUFFIX = "…[truncated]"
DIAGNOSTIC_KEYS = frozenset({"source", "exit_code", "collector", "duration_ms"})
TOP_LEVEL_FIELDS = frozenset({"version", "mode", "observed_at", "message", "active_keys", "new_keys", "gone_keys", "text", "diagnostics"})
IDENTITY_FIELDS = ("version", "mode", "observed_at", "message", "active_keys", "new_keys", "gone_keys", "text", "diagnostics")


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_identity_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The exact persisted identity projection; derived outcomes never alter it."""
    return {field: payload[field] for field in IDENTITY_FIELDS}


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
    retained = encoded[: limit - len(TRUNCATION_SUFFIX.encode("utf-8"))]
    while retained:
        try:
            truncations.append(field)
            return retained.decode("utf-8") + TRUNCATION_SUFFIX
        except UnicodeDecodeError as exc:
            retained = retained[:exc.start]
    raise IntakeError(f"oversized_{field}")


def _keys(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 256 or any(not isinstance(key, str) or not KEY_RE.fullmatch(key) for key in value):
        raise IntakeError(f"invalid_{field}")
    return sorted(set(value))


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not RFC3339_OFFSET_RE.fullmatch(value):
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
        if isinstance(item, str): result[key] = _bounded_string(item, 512, "diagnostics", truncations)
        elif item is None or isinstance(item, bool): result[key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item): result[key] = item
        else: raise IntakeError("invalid_diagnostics")
    return {key: result[key] for key in sorted(result)}


def canonicalize_event(raw: Mapping[str, Any]) -> CanonicalEvent:
    if not isinstance(raw, Mapping) or set(raw) - TOP_LEVEL_FIELDS: raise IntakeError("invalid_event")
    if raw.get("version") != 1 or isinstance(raw.get("version"), bool): raise IntakeError("invalid_version")
    mode = raw.get("mode")
    if mode not in {"check", "daily"}: raise IntakeError("invalid_mode")
    if "active_keys" not in raw: raise IntakeError("missing_active_keys")
    if mode == "check" and ("new_keys" not in raw or "gone_keys" not in raw): raise IntakeError("missing_transition_collection")
    if mode == "daily" and ("new_keys" in raw or "gone_keys" in raw): raise IntakeError("daily_transition_collection_forbidden")
    truncations: list[str] = []
    active, new, gone = _keys(raw["active_keys"], "active_keys"), _keys(raw.get("new_keys", []), "new_keys"), _keys(raw.get("gone_keys", []), "gone_keys")
    if not set(new).issubset(active) or set(gone) & set(active) or set(new) & set(gone): raise IntakeError("invalid_key_relationship")
    payload = {"version": 1, "mode": mode, "observed_at": _timestamp(raw.get("observed_at")),
               "message": _bounded_string(raw.get("message"), 16384, "message", truncations, exact=True),
               "active_keys": active, "new_keys": new, "gone_keys": gone,
               "text": _text(raw.get("text"), set(active), set(gone), truncations), "diagnostics": _diagnostics(raw.get("diagnostics"), truncations)}
    if truncations: payload["normalization"] = {"truncated_fields": sorted(set(truncations))}
    digest = hashlib.sha256(_canonical_json(canonical_identity_projection(payload)).encode("utf-8")).hexdigest()
    return CanonicalEvent(payload, f"wd-v1-{digest[:32]}", digest)


def incident_key(stable_key: str) -> str:
    if not isinstance(stable_key, str) or not KEY_RE.fullmatch(stable_key): raise IntakeError("invalid_stable_key")
    digest = hashlib.sha256(b"wd-incident-v1\0" + stable_key.encode("ascii")).hexdigest()
    return f"wd-incident-v1-{stable_key}-{digest[:24]}"


def read_json_stdin(stream: BinaryIO) -> dict[str, Any]:
    encoded = stream.read(MAX_ENCODED_EVENT + 1)
    if len(encoded) > MAX_ENCODED_EVENT: raise IntakeError("oversized_encoded_event")
    try:
        trimmed = encoded.decode("utf-8").lstrip(); value, end = json.JSONDecoder().raw_decode(trimmed)
        if trimmed[end:].strip(): raise IntakeError("trailing_json_content")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise IntakeError("invalid_json") from exc
    if not isinstance(value, dict): raise IntakeError("invalid_json_object")
    return value


class Journal:
    """SQLite journal whose delivery rows are the only downstream work queue."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._secure_path(path.parent, 0o700)
        self.connection = sqlite3.connect(path)
        self._secure_path(path, 0o600)
        self.connection.execute("PRAGMA foreign_keys = ON"); self.connection.execute("PRAGMA journal_mode = WAL"); self._create_schema()
        for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if sidecar.exists():
                self._secure_path(sidecar, 0o600)

    @staticmethod
    def _secure_path(path: Path, mode: int) -> None:
        try:
            path.chmod(mode)
        except OSError as exc:
            raise IntakeError("insecure_journal_permissions") from exc
        if path.stat().st_mode & 0o777 != mode:
            raise IntakeError("insecure_journal_permissions")

    def close(self) -> None: self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS watchdog_events (event_id TEXT PRIMARY KEY, full_digest TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, received_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('accepted','submitting','submitted','failed')), attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, submitted_at TEXT);
        CREATE TABLE IF NOT EXISTS watchdog_incident_deliveries (event_id TEXT NOT NULL REFERENCES watchdog_events(event_id), stable_key TEXT NOT NULL, transition TEXT NOT NULL, incident_key TEXT NOT NULL, kanban_task_id TEXT, delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending','submitted','failed')), outcome_code TEXT, PRIMARY KEY(event_id, stable_key, transition));
        CREATE TABLE IF NOT EXISTS watchdog_incident_state (stable_key TEXT PRIMARY KEY, lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('open','gone')), incident_key TEXT NOT NULL UNIQUE, kanban_task_id TEXT, latest_applied_at TEXT NOT NULL, latest_applied_event_id TEXT NOT NULL REFERENCES watchdog_events(event_id), latest_applied_digest TEXT NOT NULL, recurrence_root_event_id TEXT REFERENCES watchdog_events(event_id), recurrence_version INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS watchdog_daily_snapshot (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), observed_at TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES watchdog_events(event_id), full_digest TEXT NOT NULL, active_keys_json TEXT NOT NULL);
        """)
        delivery_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(watchdog_incident_deliveries)")
        }
        if "outcome_code" not in delivery_columns:
            self.connection.execute(
                "ALTER TABLE watchdog_incident_deliveries ADD COLUMN outcome_code TEXT"
            )
        self.connection.commit()

    def _insert_event(self, event: CanonicalEvent) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.connection.execute("INSERT INTO watchdog_events(event_id,full_digest,payload_json,received_at,state) VALUES(?,?,?,?,?)", (event.event_id, event.full_digest, _canonical_json(event.payload), now, "accepted"))

    def _delivery(self, event: CanonicalEvent, key: str, transition: str, state: str, code: str | None = None) -> None:
        self.connection.execute("INSERT INTO watchdog_incident_deliveries(event_id,stable_key,transition,incident_key,delivery_state,outcome_code) VALUES(?,?,?,?,?,?)", (event.event_id, key, transition, incident_key(key) if key != "__event__" else "wd-event-outcome-v1", state, code))

    def _state(self, key: str) -> tuple[str, str, str] | None:
        return self.connection.execute("SELECT lifecycle_state,latest_applied_at,latest_applied_digest FROM watchdog_incident_state WHERE stable_key=?", (key,)).fetchone()

    def _ordering(self, event: CanonicalEvent, key: str) -> str:
        state = self._state(key)
        if state is None or event.payload["observed_at"] > state[1]: return "newer"
        if event.payload["observed_at"] < state[1]: return "stale"
        return "same" if event.full_digest != state[2] else "duplicate"

    def _advance(self, event: CanonicalEvent, key: str, lifecycle: str, recurrence: int = 0) -> None:
        self.connection.execute("INSERT INTO watchdog_incident_state(stable_key,lifecycle_state,incident_key,latest_applied_at,latest_applied_event_id,latest_applied_digest,recurrence_root_event_id,recurrence_version) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(stable_key) DO UPDATE SET lifecycle_state=excluded.lifecycle_state,latest_applied_at=excluded.latest_applied_at,latest_applied_event_id=excluded.latest_applied_event_id,latest_applied_digest=excluded.latest_applied_digest,recurrence_version=watchdog_incident_state.recurrence_version+excluded.recurrence_version", (key, lifecycle, incident_key(key), event.payload["observed_at"], event.event_id, event.full_digest, event.event_id, recurrence))

    def _apply(self, event: CanonicalEvent, key: str, kind: str) -> None:
        ordering = self._ordering(event, key)
        if ordering == "stale": self._delivery(event, key, "STALE", "submitted", "stale"); return
        if ordering in {"same", "duplicate"}: self._delivery(event, key, "OUT_OF_ORDER_CONFLICT", "failed", "out_of_order_conflict"); return
        state = self._state(key); lifecycle = state[0] if state else None
        if kind == "gone":
            if lifecycle == "open": self._delivery(event, key, "GONE", "pending"); self._advance(event, key, "gone")
            elif lifecycle == "gone": self._delivery(event, key, "DUPLICATE_GONE", "submitted", "duplicate_gone")
            else: self._delivery(event, key, "GONE_ORPHAN", "submitted", "gone_orphan")
        elif kind == "new":
            if lifecycle == "open": self._delivery(event, key, "DUPLICATE_NEW", "submitted", "duplicate_new")
            elif lifecycle == "gone": self._delivery(event, key, "RECURRENCE", "pending", "recurrence"); self._advance(event, key, "open", 1)
            else: self._delivery(event, key, "NEW", "pending"); self._advance(event, key, "open")
        else:
            if lifecycle == "open": self._delivery(event, key, "UNCHANGED_ACTIVE", "submitted", "unchanged_active")
            elif lifecycle == "gone": self._delivery(event, key, "RECURRENCE", "pending", "recurrence"); self._advance(event, key, "open", 1)
            else: self._delivery(event, key, "RECOVERED_ACTIVE", "pending", "active_without_known_incident"); self._advance(event, key, "open")

    def accept(self, raw: Mapping[str, Any]) -> AcceptedEvent:
        event = canonicalize_event(raw); self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute("SELECT full_digest FROM watchdog_events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing[0] != event.full_digest: raise IntakeError("event_id_collision")
                self.connection.commit(); return AcceptedEvent(event.event_id, False)
            self._insert_event(event); payload = event.payload
            if payload["mode"] == "daily":
                snapshot = self.connection.execute("SELECT observed_at,full_digest,active_keys_json FROM watchdog_daily_snapshot WHERE singleton=1").fetchone()
                if snapshot and payload["observed_at"] < snapshot[0]: self._delivery(event, "__event__", "STALE", "submitted", "stale")
                elif snapshot and payload["observed_at"] == snapshot[0] and event.full_digest != snapshot[1]: self._delivery(event, "__event__", "OUT_OF_ORDER_CONFLICT", "failed", "out_of_order_conflict")
                else:
                    prior = set(json.loads(snapshot[2])) if snapshot else set(); current = set(payload["active_keys"])
                    for key in sorted(current - prior): self._apply(event, key, "new")
                    for key in sorted(current & prior): self._apply(event, key, "active")
                    for key in sorted(prior - current): self._apply(event, key, "gone")
                    self.connection.execute("INSERT INTO watchdog_daily_snapshot(singleton,observed_at,event_id,full_digest,active_keys_json) VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at,event_id=excluded.event_id,full_digest=excluded.full_digest,active_keys_json=excluded.active_keys_json", (payload["observed_at"], event.event_id, event.full_digest, _canonical_json(payload["active_keys"])))
            else:
                new, gone, active = set(payload["new_keys"]), set(payload["gone_keys"]), set(payload["active_keys"])
                for key in sorted(new): self._apply(event, key, "new")
                for key in sorted(active - new): self._apply(event, key, "active")
                for key in sorted(gone): self._apply(event, key, "gone")
                if not active and not gone: self._delivery(event, "__event__", "HEARTBEAT", "submitted", "heartbeat_only")
            self.connection.commit()
        except Exception:
            self.connection.rollback(); raise
        return AcceptedEvent(event.event_id, True)

    def pending_deliveries(self) -> list[tuple[str, str, str]]:
        return [tuple(row) for row in self.connection.execute("SELECT event_id,stable_key,transition FROM watchdog_incident_deliveries WHERE delivery_state IN ('pending','failed') ORDER BY event_id,stable_key,transition")]

    def delivery_details(self, event_id: str, stable_key: str, transition: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT d.incident_key,d.kanban_task_id,d.outcome_code,s.kanban_task_id,s.recurrence_version,"
            "e.payload_json FROM watchdog_incident_deliveries d JOIN watchdog_events e ON e.event_id=d.event_id "
            "LEFT JOIN watchdog_incident_state s ON s.stable_key=d.stable_key "
            "WHERE d.event_id=? AND d.stable_key=? AND d.transition=?",
            (event_id, stable_key, transition),
        ).fetchone()
        if row is None:
            raise IntakeError("unknown_delivery")
        return {"incident_key": row[0], "delivery_task_id": row[1], "outcome_code": row[2],
                "incident_task_id": row[3], "recurrence_version": row[4],
                "payload": json.loads(row[5])}

    def record_failure(self, event_id: str, stable_key: str, transition: str, code: str) -> None:
        if not code or len(code) > 128:
            raise IntakeError("invalid_failure_code")
        with self.connection:
            if self.connection.execute(
                "UPDATE watchdog_incident_deliveries SET delivery_state='failed',outcome_code=? "
                "WHERE event_id=? AND stable_key=? AND transition=?",
                (code, event_id, stable_key, transition),
            ).rowcount != 1:
                raise IntakeError("unknown_delivery")

    def acknowledge(self, event_id: str, stable_key: str, transition: str, task_id: str, *, replace_incident_task_id: bool = False) -> None:
        if not task_id: raise IntakeError("invalid_task_id")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            delivery = self.connection.execute(
                "SELECT kanban_task_id FROM watchdog_incident_deliveries WHERE event_id=? AND stable_key=? AND transition=?",
                (event_id, stable_key, transition),
            ).fetchone()
            if delivery is None: raise IntakeError("unknown_delivery")
            if delivery[0] not in (None, task_id): raise IntakeError("delivery_task_id_conflict")
            incident = self.connection.execute(
                "SELECT kanban_task_id FROM watchdog_incident_state WHERE stable_key=?", (stable_key,)
            ).fetchone()
            if incident is None: raise IntakeError("unknown_incident_state")
            if incident[0] not in (None, task_id) and not replace_incident_task_id: raise IntakeError("incident_task_id_conflict")
            if incident[0] != task_id:
                self.connection.execute(
                    "UPDATE watchdog_incident_state SET kanban_task_id=? WHERE stable_key=?",
                    (task_id, stable_key),
                )
            self.connection.execute(
                "UPDATE watchdog_incident_deliveries SET kanban_task_id=?,delivery_state='submitted' WHERE event_id=? AND stable_key=? AND transition=?",
                (task_id, event_id, stable_key, transition),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback(); raise

    def task_id(self, event_id: str, stable_key: str, transition: str) -> str | None:
        row = self.connection.execute("SELECT kanban_task_id FROM watchdog_incident_deliveries WHERE event_id=? AND stable_key=? AND transition=?", (event_id, stable_key, transition)).fetchone()
        return row[0] if row else None

    def incident_task_id(self, stable_key: str) -> str | None:
        row = self.connection.execute(
            "SELECT kanban_task_id FROM watchdog_incident_state WHERE stable_key=?", (stable_key,)
        ).fetchone()
        return row[0] if row else None

    def event_count(self) -> int: return int(self.connection.execute("SELECT COUNT(*) FROM watchdog_events").fetchone()[0])
