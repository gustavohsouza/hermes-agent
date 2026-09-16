"""Isolated end-to-end proof for the Watchdog incident lifecycle.

Fixture-to-acceptance mapping is documented in docs/watchdog-lifecycle-fixtures.md.
Every filesystem path and the Kanban database are temporary. Delivery is an
in-memory port, so this suite cannot contact WhatsApp, macOS, or the gateway.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import kanban_db
from watchdog_boundary import IntakeError, Journal, canonicalize_event
from watchdog_closure import ClosureError, ClosureOutcome, ClosureReporter, deterministic_heartbeat_outcome
from watchdog_runtime import HermesKanbanPort

DISPATCH_PATH = Path(__file__).parents[1] / "bin" / "watchdog_dispatch.py"
SPEC = importlib.util.spec_from_file_location("watchdog_dispatch_e2e", DISPATCH_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load watchdog_dispatch")
dispatch_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch_module)


def snapshot(at: str, *, active=(), new=(), gone=(), message="Watchdog fixture", mode="--check"):
    return {
        "mode": mode,
        "observed_at": at,
        "message": message,
        "active_keys": list(active),
        "new_keys": list(new),
        "gone_keys": list(gone),
        "texts": {key: f"evidence:{key}" for key in active},
    }


class RecordingDelivery:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = []
        self.successful_calls = []
        self.sent = {}

    def send(self, *, target, text, idempotency_key):
        self.calls.append((target, text, idempotency_key))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("mock gateway unavailable")
        self.successful_calls.append((target, text, idempotency_key))
        self.sent.setdefault(idempotency_key, text)


class WatchdogLifecycleE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.board = self.root / "kanban.sqlite3"
        self.spool = self.root / "spool"
        self.state = self.root / "producer-state.json"
        self.journal_path = self.home / ".hermes" / "state" / "watchdog-intake.sqlite3"
        self.launcher = self.root / "watchdog-kanban-intake"
        source = Path(__file__).parents[1]
        self.launcher.write_text(
            "#!/bin/sh\nset -eu\n"
            f"cd {str(source)!r}\n"
            f"exec /usr/bin/env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_TASK "
            f"-u HERMES_KANBAN_RUN_ID {sys.executable!r} -m watchdog_runtime --json-stdin\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o700)
        self.environment = patch.dict(
            os.environ,
            {"HOME": str(self.home), "HERMES_KANBAN_DB": str(self.board)},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        child_env = os.environ.copy()
        child_env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        child_env.pop("HERMES_KANBAN_TASK", None)
        child_env.pop("HERMES_KANBAN_RUN_ID", None)
        self.subprocess_environment = patch.object(dispatch_module.os, "environ", child_env)
        self.subprocess_environment.start()
        self.addCleanup(self.subprocess_environment.stop)
        kanban_db.init_db()

    def dispatch(self, value, *, launcher=None):
        event = dispatch_module.event_from_snapshot(value, self.state)
        ok = dispatch_module.dispatch(
            event, launcher=launcher or self.launcher, spool=self.spool
        )
        if ok:
            dispatch_module._write_state(self.state, event["active_keys"], event["observed_at"])
        return ok

    def board_rows(self):
        with sqlite3.connect(self.board) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT id,title,body,assignee,idempotency_key,max_runtime_seconds,max_retries,status "
                "FROM tasks WHERE created_by='watchdog' ORDER BY created_at,id"
            )]

    def test_complete_incident_lifecycle_is_durable_deduplicated_and_silent_until_closure(self):
        delivery = RecordingDelivery()

        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T10:00:00Z", active=("proxy-errors",), new=("proxy-errors",),
            message="NOVO: provider returned 503; exact evidence",
        )))
        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T10:01:00Z", active=("proxy-errors",),
            message="unchanged active snapshot",
        )))
        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T10:02:00Z", active=("car-timeout",), new=("car-timeout",),
            gone=("proxy-errors",), message="NOVO: car timeout; RESOLVIDO: proxy-errors",
        )))
        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T10:03:00Z", gone=("car-timeout",), message="RESOLVIDO: car-timeout",
        )))

        rows = self.board_rows()
        self.assertEqual(2, len(rows))
        self.assertEqual({"proxy-errors", "car-timeout"}, {
            row["title"].removeprefix("Watchdog incident: ") for row in rows
        })
        self.assertTrue(all(row["assignee"] == "foreman" for row in rows))
        self.assertTrue(all(row["max_runtime_seconds"] == 86400 for row in rows))
        self.assertTrue(all(row["max_retries"] == 3 for row in rows))
        self.assertTrue(all("Watchdog metadata:" in row["body"] for row in rows))
        self.assertEqual([], delivery.calls, "raw initial and RESOLVIDO alerts must not reach Gustavo")

        journal = Journal(self.journal_path)
        self.addCleanup(journal.close)
        self.assertEqual([], journal.pending_deliveries())
        proxy_task = next(row["id"] for row in rows if row["title"].endswith("proxy-errors"))
        outcome = ClosureOutcome(
            classification="actionable",
            summary="Proxy errors stopped.",
            treatment_warranted=True,
            actions="Recovered the proxy worker.",
            verification="Independent probe returned healthy three times.",
            residual_risk="Provider errors can recur.",
        )
        reporter = ClosureReporter(journal, delivery)
        with self.assertRaisesRegex(ClosureError, "independent_verification_required"):
            reporter.complete(
                stable_key="proxy-errors", task_id=proxy_task, occurrence_id="r0",
                outcome=outcome, verification_source="watchdog_resolvido",
            )
        self.assertTrue(reporter.complete(
            stable_key="proxy-errors", task_id=proxy_task, occurrence_id="r0", outcome=outcome
        ))
        self.assertFalse(reporter.complete(
            stable_key="proxy-errors", task_id=proxy_task, occurrence_id="r0", outcome=outcome
        ))
        self.assertEqual(1, len(delivery.sent))

        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T10:04:00Z", active=("proxy-errors",), new=("proxy-errors",),
            message="NOVO: proxy recurrence",
        )))
        recurrence_rows = self.board_rows()
        self.assertEqual(3, len(recurrence_rows))
        recurrence = next(row for row in recurrence_rows if row["id"] not in {item["id"] for item in rows})
        self.assertIn(proxy_task, recurrence["body"])
        self.assertIn("max-runs 3", recurrence["body"])

    def test_daily_healthy_snapshot_malformed_and_oversized_inputs_create_no_work(self):
        self.assertTrue(self.dispatch(snapshot(
            "2026-09-16T12:00:00Z", active=(), new=(), gone=(),
            message="Brain OK", mode="--daily",
        )))
        self.assertEqual([], self.board_rows())
        outcome = deterministic_heartbeat_outcome({
            "version": 1, "mode": "daily", "observed_at": "2026-09-16T12:00:00Z",
            "message": "Brain OK", "active_keys": [],
        })
        self.assertEqual("informational", outcome.classification)
        with self.assertRaises(IntakeError):
            canonicalize_event({"version": 1, "mode": "check", "message": "bad"})
        with self.assertRaises(IntakeError):
            canonicalize_event({
                "version": 1, "mode": "check", "observed_at": "2026-09-16T12:01:00Z",
                "message": "x" * 16385, "active_keys": [], "new_keys": [], "gone_keys": [],
            })
        malformed = subprocess.run(
            [str(self.launcher)], input=b'{"version":1}', capture_output=True,
            env=dict(dispatch_module.os.environ), check=False,
        )
        oversized_event = {
            "version": 1, "mode": "check", "observed_at": "2026-09-16T12:01:00Z",
            "message": "x" * 16385, "active_keys": [], "new_keys": [], "gone_keys": [],
        }
        oversized = subprocess.run(
            [str(self.launcher)], input=json.dumps(oversized_event).encode(), capture_output=True,
            env=dict(dispatch_module.os.environ), check=False,
        )
        self.assertNotEqual(0, malformed.returncode)
        self.assertNotEqual(0, oversized.returncode)
        self.assertEqual([], self.board_rows())

    def test_unavailable_board_recovers_from_spool_without_duplicate_card(self):
        value = snapshot(
            "2026-09-16T13:00:00Z", active=("gateway-restart",), new=("gateway-restart",),
            message="NOVO: gateway restart",
        )
        bad_board_launcher = self.root / "bad-board-launcher"
        source = Path(__file__).parents[1]
        bad_board_launcher.write_text(
            "#!/bin/sh\nset -eu\n"
            f"cd {str(source)!r}\n"
            f"export HERMES_KANBAN_DB={str(self.root)!r}\n"
            f"exec /usr/bin/env -u HERMES_DELEGATED_CHILD_CONTEXT -u HERMES_KANBAN_TASK "
            f"-u HERMES_KANBAN_RUN_ID {sys.executable!r} -m watchdog_runtime --json-stdin\n",
            encoding="utf-8",
        )
        bad_board_launcher.chmod(0o700)
        self.assertFalse(self.dispatch(value, launcher=bad_board_launcher))
        queued = list(self.spool.glob("*.json"))
        self.assertEqual(1, len(queued))
        self.assertEqual(0o700, self.spool.stat().st_mode & 0o777)
        self.assertEqual(0o600, queued[0].stat().st_mode & 0o777)
        self.assertTrue(self.dispatch(value))
        self.assertFalse(any(self.spool.glob("*.json")))
        self.assertEqual(1, len(self.board_rows()))

    def test_gateway_retry_and_tier3_are_durable_and_exactly_once(self):
        journal = Journal(self.journal_path)
        self.addCleanup(journal.close)
        delivery = RecordingDelivery(failures=1)
        reporter = ClosureReporter(journal, delivery)
        self.assertFalse(reporter.escalate_tier3(
            stable_key="billing-choice", task_id="t_fixture", occurrence_id="r0",
            summary="Owner decision required.", verification="Threshold independently checked.",
            residual_risk="Spend continues until a decision.",
        ))
        self.assertEqual(0, reporter.deliver_pending())
        self.assertEqual(2, len(delivery.calls))
        self.assertEqual(delivery.calls[0][2], delivery.calls[1][2])
        self.assertEqual(1, len(delivery.successful_calls))
        self.assertIn("tier-3 decision required", next(iter(delivery.sent.values())))
        self.assertEqual(0, reporter.deliver_pending())
        self.assertEqual(1, len(delivery.sent))

    def test_dispatch_failure_is_non_delivering_and_reports_automation_failure(self):
        result = subprocess.run(
            [sys.executable, str(DISPATCH_PATH), "--state", str(self.state),
             "--spool", str(self.spool), "--launcher", str(self.launcher), "--fixture-json"],
            input=b"not-json", capture_output=True, env=os.environ.copy(), check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"watchdog dispatch failed", result.stderr)
        script = (Path(__file__).parents[1] / "bin" / "watchdog.sh").read_text(encoding="utf-8")
        self.assertIn("Watchdog automation failed", script)
        self.assertNotIn("127.0.0.1:8787", script.split("notify() {", 1)[1].split("\n}\n\n#", 1)[0])
        self.assertEqual([], self.board_rows())


if __name__ == "__main__":
    unittest.main()
