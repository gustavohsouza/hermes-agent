import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from watchdog_boundary import IntakeError, Journal, canonicalize_event, incident_key, read_json_stdin


BASE = {
    "version": 1,
    "mode": "check",
    "observed_at": "2026-09-09T17:05:33.139815-03:00",
    "message": "ATENCAO: café 🚨\nexact opaque message",
    "active_keys": ["jobs-mortos", "proxy-erros"],
    "new_keys": ["proxy-erros"],
    "gone_keys": [],
    "text": {"summary": "summary", "new": {"proxy-erros": "new detail"}},
    "diagnostics": {"source": "watchdog.sh", "exit_code": 0},
}


class CanonicalizationTests(unittest.TestCase):
    def canonical(self, payload):
        return canonicalize_event(payload).payload

    def test_unicode_message_round_trips_without_normalization(self):
        payload = self.canonical(BASE)
        self.assertEqual(payload["message"], BASE["message"])
        self.assertEqual(payload["message"].encode("utf-8"), BASE["message"].encode("utf-8"))

    def test_key_order_and_duplicates_produce_same_identity(self):
        scrambled = dict(BASE, active_keys=["proxy-erros", "jobs-mortos", "proxy-erros"])
        canonical = canonicalize_event(BASE)
        variant = canonicalize_event(scrambled)
        self.assertEqual(variant.payload["active_keys"], ["jobs-mortos", "proxy-erros"])
        self.assertEqual(variant.event_id, canonical.event_id)

    def test_text_changes_event_identity_not_incident_key(self):
        changed = dict(BASE, text={"summary": "different", "new": {"proxy-erros": "different"}})
        self.assertNotEqual(canonicalize_event(BASE).event_id, canonicalize_event(changed).event_id)
        self.assertEqual(incident_key("proxy-erros"), incident_key("proxy-erros"))
        self.assertNotIn("different", incident_key("proxy-erros"))

    def test_summary_and_diagnostic_truncate_on_utf8_boundary(self):
        payload = dict(BASE, text={"summary": "🚨" * 1000}, diagnostics={"source": "é" * 600})
        canonical = self.canonical(payload)
        self.assertLessEqual(len(canonical["text"]["summary"].encode("utf-8")), 2048)
        self.assertTrue(canonical["text"]["summary"].endswith("…[truncated]"))
        self.assertLessEqual(len(canonical["diagnostics"]["source"].encode("utf-8")), 512)
        self.assertTrue(canonical["diagnostics"]["source"].endswith("…[truncated]"))

    def test_invalid_inputs_reject_before_journal_write(self):
        cases = [
            dict(BASE, mode="CHECK"),
            dict(BASE, observed_at="not-a-date"),
            dict(BASE, message="bad\x00message"),
            dict(BASE, active_keys=["INVALID"]),
            dict(BASE, diagnostics={"secret": "should reject"}),
            {key: value for key, value in BASE.items() if key != "active_keys"},
            dict(BASE, mode="daily", active_keys=[]),
            dict(BASE, mode="daily", active_keys=[], new_keys=[]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            for payload in cases:
                with self.assertRaises(IntakeError):
                    journal.accept(payload)
            self.assertEqual(journal.event_count(), 0)

    def test_daily_explicit_empty_snapshot_is_valid(self):
        payload = {key: value for key, value in BASE.items() if key not in ("new_keys", "gone_keys")}
        payload.update(mode="daily", active_keys=[])
        self.assertEqual(self.canonical(payload)["active_keys"], [])

    def test_json_stdin_rejects_trailing_and_hostile_serialization(self):
        encoded = json.dumps(BASE, ensure_ascii=False).encode("utf-8")
        self.assertEqual(read_json_stdin(io.BytesIO(encoded))["message"], BASE["message"])
        with self.assertRaises(IntakeError):
            read_json_stdin(io.BytesIO(encoded + b" {}"))
        with self.assertRaises(IntakeError):
            read_json_stdin(io.BytesIO(b"{\xff}"))
        with self.assertRaises(IntakeError):
            read_json_stdin(io.BytesIO(b"x" * (65536 + 1)))


class JournalTests(unittest.TestCase):
    def test_duplicate_delivery_and_crash_retry_are_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            accepted = journal.accept(BASE)
            self.assertTrue(accepted.inserted)
            self.assertEqual(journal.pending_deliveries(), [(accepted.event_id, "proxy-erros", "NEW")])
            duplicate = journal.accept(BASE)
            self.assertFalse(duplicate.inserted)
            self.assertEqual(journal.pending_deliveries(), [(accepted.event_id, "proxy-erros", "NEW")])
            journal.acknowledge(accepted.event_id, "proxy-erros", "NEW", "t_123")
            self.assertEqual(journal.pending_deliveries(), [])
            self.assertEqual(journal.task_id(accepted.event_id, "proxy-erros", "NEW"), "t_123")

    def test_full_digest_collision_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / "journal.sqlite3")
            accepted = journal.accept(BASE)
            with journal.connection:
                journal.connection.execute(
                    "UPDATE watchdog_events SET full_digest = ? WHERE event_id = ?",
                    ("0" * 64, accepted.event_id),
                )
            with self.assertRaises(IntakeError):
                journal.accept(BASE)


if __name__ == "__main__":
    unittest.main()
