import tempfile
import unittest
from pathlib import Path

from watchdog_boundary import Journal
from watchdog_closure import (
    ClosureError,
    ClosureOutcome,
    ClosureReporter,
    deterministic_heartbeat_outcome,
)


class FakeDelivery:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []
        self.delivered = {}

    def send(self, *, target, text, idempotency_key):
        self.calls.append((target, text, idempotency_key))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("gateway unavailable")
        self.delivered.setdefault(idempotency_key, text)


class ClosureReportingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Journal(Path(self.directory.name) / "journal.sqlite3")
        self.addCleanup(self.journal.close)

    def outcome(self, classification="actionable"):
        return ClosureOutcome(
            classification=classification,
            summary="Proxy transport errors stopped",
            treatment_warranted=classification == "actionable",
            actions="Restarted the bounded proxy worker.",
            verification="Three consecutive checks returned zero transport errors.",
            residual_risk="A provider outage can recur.",
        )

    def test_completion_requires_full_contract_and_rejects_tier3(self):
        with self.assertRaisesRegex(ClosureError, "tier3_requires_decision"):
            self.outcome("tier-3").validate(for_completion=True)
        with self.assertRaisesRegex(ClosureError, "missing_verification"):
            ClosureOutcome("informational", "summary", False, "No action.", "", "None.").validate()

    def test_verified_completion_delivers_exactly_once(self):
        delivery = FakeDelivery()
        reporter = ClosureReporter(self.journal, delivery)
        self.assertTrue(reporter.complete(stable_key="proxy-errors", task_id="t_1", occurrence_id="r0", outcome=self.outcome()))
        self.assertFalse(reporter.complete(stable_key="proxy-errors", task_id="t_1", occurrence_id="r0", outcome=self.outcome()))
        self.assertEqual(1, len(delivery.delivered))
        target, text, key = delivery.calls[0]
        self.assertEqual("whatsapp:117484669640820@lid", target)
        self.assertIn("Alert key: proxy-errors", text)
        self.assertIn("Treatment warranted: yes", text)
        self.assertIn("Actions: Restarted", text)
        self.assertIn("Verification: Three consecutive", text)
        self.assertIn("Residual risk: A provider outage", text)
        self.assertTrue(key.startswith("wd-closure-v1-"))

    def test_failed_delivery_is_durable_and_retry_uses_same_identity(self):
        delivery = FakeDelivery(failures=1)
        reporter = ClosureReporter(self.journal, delivery)
        self.assertFalse(reporter.complete(stable_key="proxy-errors", task_id="t_1", occurrence_id="r0", outcome=self.outcome()))
        self.assertEqual(0, reporter.deliver_pending())
        self.assertEqual(2, len(delivery.calls))
        self.assertEqual(delivery.calls[0][2], delivery.calls[1][2])
        self.assertEqual(1, len(delivery.delivered))
        self.assertEqual(0, reporter.deliver_pending())

    def test_tier3_is_only_preclosure_contact_and_is_deduplicated(self):
        delivery = FakeDelivery()
        reporter = ClosureReporter(self.journal, delivery)
        self.assertTrue(reporter.escalate_tier3(
            stable_key="billing-choice", task_id="t_2", occurrence_id="r0",
            summary="Choose whether to disable synthesis.",
            verification="Spend crossed the owner-defined decision boundary.",
            residual_risk="Continued spend until a decision is recorded.",
        ))
        self.assertFalse(reporter.escalate_tier3(
            stable_key="billing-choice", task_id="t_2", occurrence_id="r0",
            summary="Choose whether to disable synthesis.",
            verification="Spend crossed the owner-defined decision boundary.",
            residual_risk="Continued spend until a decision is recorded.",
        ))
        self.assertEqual(1, len(delivery.delivered))
        self.assertIn("tier-3 decision required", delivery.calls[0][1])

    def test_resolvido_cannot_complete_incident(self):
        reporter = ClosureReporter(self.journal, FakeDelivery())
        with self.assertRaisesRegex(ClosureError, "independent_verification_required"):
            reporter.complete(
                stable_key="x", task_id="t_1", occurrence_id="r0", outcome=self.outcome(),
                verification_source="watchdog_resolvido",
            )
        with self.assertRaisesRegex(ClosureError, "independent_verification_required"):
            reporter.complete(
                stable_key="x", task_id="t_1", occurrence_id="r0", outcome=self.outcome(),
                verification_source="",
            )

    def test_recurrence_has_a_distinct_report_identity(self):
        delivery = FakeDelivery()
        reporter = ClosureReporter(self.journal, delivery)
        self.assertTrue(reporter.complete(
            stable_key="proxy-errors", task_id="t_1", occurrence_id="r0", outcome=self.outcome()))
        self.assertTrue(reporter.complete(
            stable_key="proxy-errors", task_id="t_1", occurrence_id="r1", outcome=self.outcome()))
        self.assertEqual(2, len(delivery.delivered))

    def test_healthy_daily_heartbeat_can_auto_close_only_when_deterministic(self):
        outcome = deterministic_heartbeat_outcome({
            "version": 1, "mode": "daily", "active_keys": [],
            "observed_at": "2026-09-16T12:00:00Z", "message": "Brain OK",
        })
        self.assertEqual("informational", outcome.classification)
        self.assertFalse(outcome.treatment_warranted)
        with self.assertRaisesRegex(ClosureError, "heartbeat_not_healthy"):
            deterministic_heartbeat_outcome({"mode": "daily", "active_keys": ["x"]})
        with self.assertRaisesRegex(ClosureError, "heartbeat_not_healthy"):
            deterministic_heartbeat_outcome({
                "version": True, "mode": "daily", "active_keys": [],
                "observed_at": "2026-09-16T12:00:00Z", "message": "Brain OK",
            })


if __name__ == "__main__":
    unittest.main()
