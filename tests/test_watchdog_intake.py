import io
import json
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
