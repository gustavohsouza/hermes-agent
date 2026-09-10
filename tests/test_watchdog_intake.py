import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from watchdog_boundary import IntakeError, Journal, canonical_identity_projection, canonicalize_event, incident_key, read_json_stdin

BASE = {
    "version": 1, "mode": "check", "observed_at": "2026-09-09T17:05:33.139815-03:00",
    "message": "ATENCAO: café 🚨\nexact opaque message", "active_keys": ["jobs-mortos", "proxy-erros"],
    "new_keys": ["proxy-erros"], "gone_keys": [],
    "text": {"summary": "summary", "new": {"proxy-erros": "new detail"}},
    "diagnostics": {"source": "watchdog.sh", "exit_code": 0},
}


def check(at, active=(), new=(), gone=(), message="check"):
    return {"version": 1, "mode": "check", "observed_at": at, "message": message,
            "active_keys": list(active), "new_keys": list(new), "gone_keys": list(gone)}


def daily(at, active, message="snapshot"):
    return {"version": 1, "mode": "daily", "observed_at": at, "message": message, "active_keys": list(active)}


class CanonicalizationTests(unittest.TestCase):
    def test_all_documented_field_and_identity_boundaries(self):
        at_limit = dict(
            BASE,
            message="m" * 16384,
            active_keys=[f"k{i:03d}" for i in range(256)],
            new_keys=[],
            text={
                "summary": "s" * 2048,
                "new": {},
                "gone": {},
            },
            diagnostics={
                "source": "s" * 512,
                "collector": "c" * 512,
                "exit_code": 1,
                "duration_ms": 2,
            },
        )
        payload = canonicalize_event(at_limit).payload
        self.assertEqual(16384, len(payload["message"].encode("utf-8")))
        self.assertEqual(256, len(payload["active_keys"]))
        self.assertEqual(2048, len(payload["text"]["summary"].encode("utf-8")))
        self.assertEqual(512, len(payload["diagnostics"]["source"].encode("utf-8")))
        maximum_key = "k" * 96
        derived = incident_key(maximum_key)
        self.assertEqual(136, len(derived.encode("ascii")))
        self.assertIn(maximum_key, derived)

        rejected = [
            dict(BASE, message="m" * 16385),
            dict(BASE, active_keys=[f"k{i:03d}" for i in range(257)], new_keys=[]),
            dict(BASE, active_keys=["k" * 97], new_keys=[]),
            dict(BASE, diagnostics={"source": "ok", "unknown": "no"}),
        ]
        for value in rejected:
            with self.subTest(value=list(value)):
                with self.assertRaises(IntakeError):
                    canonicalize_event(value)

        truncated = canonicalize_event(dict(
            BASE,
            text={
                "summary": "s" * 2049,
                "new": {"proxy-erros": "n" * 1025},
            },
            diagnostics={"source": "d" * 513},
        )).payload
        self.assertLessEqual(len(truncated["text"]["summary"].encode("utf-8")), 2048)
        self.assertLessEqual(len(truncated["text"]["new"]["proxy-erros"].encode("utf-8")), 1024)
        self.assertLessEqual(len(truncated["diagnostics"]["source"].encode("utf-8")), 512)
        self.assertEqual(
            ["diagnostics", "text_new", "text_summary"],
            truncated["normalization"]["truncated_fields"],
        )

    def test_unicode_message_round_trips_without_normalization(self):
        payload = canonicalize_event(BASE).payload
        self.assertEqual(payload["message"].encode("utf-8"), BASE["message"].encode("utf-8"))

    def test_key_order_and_duplicates_produce_same_identity(self):
        scrambled = dict(BASE, active_keys=["proxy-erros", "jobs-mortos", "proxy-erros"])
        self.assertEqual(canonicalize_event(BASE).event_id, canonicalize_event(scrambled).event_id)

    def test_text_changes_event_identity_not_incident_key(self):
        changed = dict(BASE, text={"summary": "different", "new": {"proxy-erros": "different"}})
        self.assertNotEqual(canonicalize_event(BASE).event_id, canonicalize_event(changed).event_id)
        self.assertNotIn("different", incident_key("proxy-erros"))

    def test_truncation_and_persisted_identity_projection_are_deterministic(self):
        event = canonicalize_event(dict(BASE, text={"summary": "🚨" * 1000}, diagnostics={"source": "é" * 600}))
        self.assertTrue(event.payload["text"]["summary"].endswith("…[truncated]"))
        self.assertTrue(event.payload["diagnostics"]["source"].endswith("…[truncated]"))
        self.assertEqual(event.full_digest, __import__("hashlib").sha256(
            json.dumps(canonical_identity_projection(event.payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_invalid_inputs_reject_before_journal_write(self):
        cases = [dict(BASE, mode="CHECK"), dict(BASE, observed_at="not-a-date"),
                 dict(BASE, observed_at="2026-09-09 17:05:33-03:00"), dict(BASE, message="bad\x00message"),
                 dict(BASE, active_keys=["INVALID"]), dict(BASE, diagnostics={"secret": "no"}),
                 {k: v for k, v in BASE.items() if k != "active_keys"}, dict(BASE, mode="daily", active_keys=[]),
                 dict(BASE, mode="daily", active_keys=[], new_keys=[])]
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            for payload in cases:
                with self.assertRaises(IntakeError): journal.accept(payload)
            self.assertEqual(journal.event_count(), 0)

    def test_daily_explicit_empty_snapshot_is_valid(self):
        payload = {k: v for k, v in BASE.items() if k not in ("new_keys", "gone_keys")}
        self.assertEqual(canonicalize_event(dict(payload, mode="daily", active_keys=[])).payload["active_keys"], [])

    def test_json_stdin_rejects_trailing_and_hostile_serialization(self):
        encoded = json.dumps(BASE, ensure_ascii=False).encode()
        self.assertEqual(read_json_stdin(io.BytesIO(encoded))["message"], BASE["message"])
        for bad in (encoded + b" {}", b"{\xff}", b"x" * 65537):
            with self.assertRaises(IntakeError): read_json_stdin(io.BytesIO(bad))


class JournalTests(unittest.TestCase):
    def test_journal_enforces_owner_only_storage_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir(mode=0o755)
            os.chmod(state, 0o755)
            path = state / "journal.sqlite3"
            path.touch(mode=0o644)
            os.chmod(path, 0o644)
            journal = Journal(path)
            self.addCleanup(journal.close)
            self.assertEqual(0o700, state.stat().st_mode & 0o777)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                if sidecar.exists():
                    self.assertEqual(0o600, sidecar.stat().st_mode & 0o777)

    def outcomes(self, journal):
        return [tuple(row) for row in journal.connection.execute(
            "SELECT stable_key,transition,delivery_state,COALESCE(outcome_code,'') FROM watchdog_incident_deliveries ORDER BY event_id,stable_key,transition")]

    def test_restart_duplicate_and_acknowledgement_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"; journal = Journal(path)
            accepted = journal.accept(BASE); journal.close(); journal = Journal(path)
            self.assertFalse(journal.accept(BASE).inserted)
            self.assertEqual(
                journal.pending_deliveries(),
                [(accepted.event_id, "jobs-mortos", "RECOVERED_ACTIVE"), (accepted.event_id, "proxy-erros", "NEW")],
            )
            journal.acknowledge(accepted.event_id, "jobs-mortos", "RECOVERED_ACTIVE", "t_122")
            journal.acknowledge(accepted.event_id, "proxy-erros", "NEW", "t_123"); journal.close(); journal = Journal(path)
            self.assertEqual(journal.pending_deliveries(), [])
            self.assertEqual(journal.task_id(accepted.event_id, "proxy-erros", "NEW"), "t_123")
            self.assertEqual(journal.incident_task_id("jobs-mortos"), "t_122")
            self.assertEqual(journal.incident_task_id("proxy-erros"), "t_123")

    def test_task_id_survives_gone_and_recurrence_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"; journal = Journal(path)
            opened = journal.accept(check("2026-09-09T10:00:00Z", ["x"], ["x"]))
            journal.acknowledge(opened.event_id, "x", "NEW", "t_123")
            journal.accept(check("2026-09-09T11:00:00Z", [], [], ["x"]))
            journal.close(); journal = Journal(path)
            self.assertEqual(journal.incident_task_id("x"), "t_123")
            journal.accept(check("2026-09-09T12:00:00Z", ["x"]))
            self.assertEqual(journal.incident_task_id("x"), "t_123")
            self.assertIn(("x", "RECURRENCE", "pending", "recurrence"), self.outcomes(journal))

    def test_acknowledgement_fails_closed_when_incident_has_other_task_id(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            accepted = journal.accept(check("2026-09-09T10:00:00Z", ["x"], ["x"]))
            journal.connection.execute("UPDATE watchdog_incident_state SET kanban_task_id='t_existing' WHERE stable_key='x'")
            journal.connection.commit()
            with self.assertRaisesRegex(IntakeError, "incident_task_id_conflict"):
                journal.acknowledge(accepted.event_id, "x", "NEW", "t_new")
            self.assertEqual(journal.task_id(accepted.event_id, "x", "NEW"), None)
            self.assertEqual(journal.incident_task_id("x"), "t_existing")

    def test_migrates_prior_journal_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = __import__("sqlite3").connect(path)
            connection.executescript("""
            CREATE TABLE watchdog_events (event_id TEXT PRIMARY KEY, full_digest TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, received_at TEXT NOT NULL, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, submitted_at TEXT);
            CREATE TABLE watchdog_incident_deliveries (event_id TEXT NOT NULL, stable_key TEXT NOT NULL, transition TEXT NOT NULL, incident_key TEXT NOT NULL, kanban_task_id TEXT, delivery_state TEXT NOT NULL, PRIMARY KEY(event_id, stable_key, transition));
            CREATE TABLE watchdog_incident_state (stable_key TEXT PRIMARY KEY, lifecycle_state TEXT NOT NULL, incident_key TEXT NOT NULL UNIQUE, kanban_task_id TEXT, latest_applied_at TEXT NOT NULL, latest_applied_event_id TEXT NOT NULL, latest_applied_digest TEXT NOT NULL, recurrence_root_event_id TEXT, recurrence_version INTEGER NOT NULL DEFAULT 0);
            INSERT INTO watchdog_events VALUES('old','digest','{}','2026-09-09T00:00:00Z','accepted',0,NULL,NULL);
            INSERT INTO watchdog_incident_deliveries VALUES('old','x','NEW','incident',NULL,'pending');
            INSERT INTO watchdog_incident_state VALUES('x','open','incident',NULL,'2026-09-09T00:00:00Z','old','digest','old',0);
            """)
            connection.commit(); connection.close()
            journal = Journal(path)
            self.assertEqual(journal.connection.execute("SELECT event_id FROM watchdog_events").fetchone(), ("old",))
            self.assertEqual(journal.connection.execute("SELECT stable_key, outcome_code FROM watchdog_incident_deliveries").fetchone(), ("x", None))
            self.assertEqual(journal.connection.execute("SELECT COUNT(*) FROM watchdog_daily_snapshot").fetchone(), (0,))
            journal.close(); journal = Journal(path)
            self.assertEqual(journal.connection.execute("SELECT COUNT(*) FROM watchdog_events").fetchone(), (1,))

    def test_check_transition_matrix_has_durable_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            journal.accept(check("2026-09-09T10:00:00Z", ["x"], ["x"]))
            journal.accept(check("2026-09-09T11:00:00Z", ["x"], ["x"]))
            journal.accept(check("2026-09-09T12:00:00Z", ["x"]))
            journal.accept(check("2026-09-09T13:00:00Z", [], [], ["x"]))
            journal.accept(check("2026-09-09T14:00:00Z", [], [], ["x"]))
            journal.accept(check("2026-09-09T15:00:00Z", [], [], ["orphan"]))
            journal.accept(check("2026-09-09T16:00:00Z"))
            expected = {("x", "NEW", "pending", ""), ("x", "DUPLICATE_NEW", "submitted", "duplicate_new"),
                        ("x", "UNCHANGED_ACTIVE", "submitted", "unchanged_active"), ("x", "GONE", "pending", ""),
                        ("x", "DUPLICATE_GONE", "submitted", "duplicate_gone"), ("orphan", "GONE_ORPHAN", "submitted", "gone_orphan"),
                        ("__event__", "HEARTBEAT", "submitted", "heartbeat_only")}
            self.assertEqual(set(self.outcomes(journal)), expected)

    def test_recovered_active_and_recurrence_create_durable_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            journal.accept(check("2026-09-09T10:00:00Z", ["x"]))
            journal.accept(check("2026-09-09T11:00:00Z", [], [], ["x"]))
            journal.accept(check("2026-09-09T12:00:00Z", ["x"]))
            outcomes = self.outcomes(journal)
            self.assertIn(("x", "RECOVERED_ACTIVE", "pending", "active_without_known_incident"), outcomes)
            self.assertIn(("x", "RECURRENCE", "pending", "recurrence"), outcomes)
            self.assertEqual(journal.connection.execute("SELECT lifecycle_state,recurrence_version FROM watchdog_incident_state WHERE stable_key='x'").fetchone(), ("open", 1))

    def test_daily_ordering_stale_and_same_time_conflict_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            journal.accept(daily("2026-09-09T10:00:00Z", ["x"]))
            journal.accept(daily("2026-09-09T11:00:00Z", []))
            journal.accept(daily("2026-09-09T09:00:00Z", ["x"], "stale"))
            journal.accept(daily("2026-09-09T11:00:00Z", ["x"], "conflict"))
            outcomes = self.outcomes(journal)
            self.assertIn(("__event__", "STALE", "submitted", "stale"), outcomes)
            self.assertIn(("__event__", "OUT_OF_ORDER_CONFLICT", "failed", "out_of_order_conflict"), outcomes)
            self.assertEqual(journal.connection.execute("SELECT observed_at,active_keys_json FROM watchdog_daily_snapshot").fetchone(), ("2026-09-09T11:00:00.000000Z", "[]"))

    def test_per_key_same_time_conflict_and_stale_do_not_mutate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            journal.accept(check("2026-09-09T10:00:00Z", ["x"], ["x"]))
            journal.accept(check("2026-09-09T09:00:00Z", [], [], ["x"], "stale"))
            journal.accept(check("2026-09-09T10:00:00Z", [], [], ["x"], "conflict"))
            self.assertEqual(journal.connection.execute("SELECT lifecycle_state FROM watchdog_incident_state WHERE stable_key='x'").fetchone(), ("open",))
            self.assertIn(("x", "OUT_OF_ORDER_CONFLICT", "failed", "out_of_order_conflict"), self.outcomes(journal))


if __name__ == "__main__": unittest.main()
