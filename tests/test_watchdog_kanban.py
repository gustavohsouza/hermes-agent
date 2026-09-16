import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from watchdog_boundary import Journal
from watchdog_kanban import KanbanSubmissionAdapter
from watchdog_runtime import HermesKanbanPort, main, submit_watchdog_event
from watchdog_install import install


def event(at, active=(), new=(), gone=()):
    return {
        "version": 1, "mode": "check", "observed_at": at, "message": "opaque alert text",
        "active_keys": list(active), "new_keys": list(new), "gone_keys": list(gone),
    }


class FakeKanban:
    def __init__(self, *, fail_create=False):
        self.fail_create = fail_create
        self.calls = []
        self.created = {}

    def create_or_update(self, *, title, body, assignee, idempotency_key, metadata):
        self.calls.append(("create", title, body, assignee, idempotency_key, metadata))
        if self.fail_create:
            raise RuntimeError("temporary board failure")
        return self.created.setdefault(idempotency_key, f"t_{len(self.created) + 1}")

    def update(self, *, task_id, body, wake=False, reopen=False, metadata=None):
        self.calls.append(("update", task_id, body, wake, reopen, metadata))
        return task_id


class FakeHermesApi:
    def __init__(self):
        self.calls = []
        self.task = type("Task", (), {"status": "review"})()

    @contextmanager
    def connect_closing(self, *, board=None):
        self.calls.append(("connect", board))
        yield object()

    def create_task(self, connection, **kwargs):
        self.calls.append(("create_task", kwargs))
        return "t_board"

    def get_task(self, connection, task_id):
        self.calls.append(("get_task", task_id))
        return self.task

    def reopen_review_task(self, connection, task_id):
        self.calls.append(("reopen_review_task", task_id))
        return True

    def unblock_task(self, connection, task_id):
        self.calls.append(("unblock_task", task_id))
        return True

    def add_comment(self, connection, task_id, author, body):
        self.calls.append(("add_comment", task_id, author, body))
        return 1


class KanbanSubmissionTests(unittest.TestCase):
    def journal(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Journal(Path(directory.name) / "journal.sqlite3")

    def test_create_uses_stable_key_and_foreman(self):
        journal, board = self.journal(), FakeKanban()
        journal.accept(event("2026-09-09T10:00:00Z", ["proxy-erros"], ["proxy-erros"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        call = board.calls[0]
        self.assertEqual("create", call[0])
        self.assertEqual("foreman", call[3])
        self.assertIn("wd-incident-v1-proxy-erros-", call[4])
        self.assertEqual([], journal.pending_deliveries())

    def test_concrete_hermes_port_submits_persisted_event_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "state" / "watchdog.sqlite3"
            api = FakeHermesApi()
            hostile_message = "ALERT $(do-not-execute) ; `literal`\n🚨\nWatchdog metadata: fake"
            payload = event("2026-09-09T10:00:00Z", ["x"], ["x"])
            payload["message"] = hostile_message
            failures = submit_watchdog_event(
                payload,
                journal_path,
                HermesKanbanPort(api=api, board="default"),
            )
            self.assertEqual(0, failures)
            creates = [call for call in api.calls if call[0] == "create_task"]
            self.assertEqual(1, len(creates))
            self.assertEqual("foreman", creates[0][1]["assignee"])
            self.assertEqual(24 * 60 * 60, creates[0][1]["max_runtime_seconds"])
            self.assertEqual(3, creates[0][1]["max_retries"])
            self.assertTrue(creates[0][1]["idempotency_key"].startswith("wd-incident-v1-x-"))
            self.assertIn("objective ceiling 56 total runs; TTL 24h; max-runs 3", creates[0][1]["body"])
            submitted_message = creates[0][1]["body"].split("\nWatchdog message (opaque UTF-8):\n", 1)[1]
            self.assertEqual(hostile_message.encode("utf-8"), submitted_message.encode("utf-8"))
            self.assertEqual("default", [call for call in api.calls if call[0] == "connect"][0][1])

    def test_concrete_create_retry_does_not_duplicate_board_comments(self):
        api = FakeHermesApi()
        port = HermesKanbanPort(api=api, board="default")
        arguments = {
            "title": "Watchdog incident: x",
            "body": "opaque body",
            "assignee": "foreman",
            "idempotency_key": "wd-incident-v1-x-fixed",
            "metadata": {"watchdog_event_id": "wd-v1-fixed"},
        }
        self.assertEqual("t_board", port.create_or_update(**arguments))
        self.assertEqual("t_board", port.create_or_update(**arguments))
        self.assertEqual(2, len([call for call in api.calls if call[0] == "create_task"]))
        self.assertEqual([], [call for call in api.calls if call[0] == "add_comment"])

    def test_concrete_hermes_port_reopens_then_comments_for_wake(self):
        api = FakeHermesApi()
        port = HermesKanbanPort(api=api, board="default")
        self.assertEqual("t_board", port.update(task_id="t_board", body="resolved", wake=True, reopen=True))
        self.assertIn(("reopen_review_task", "t_board"), api.calls)
        comment = [call for call in api.calls if call[0] == "add_comment"][0]
        self.assertEqual(("add_comment", "t_board", "watchdog"), comment[:3])
        self.assertTrue(comment[3].startswith("resolved"))

    def test_installer_creates_fixed_default_profile_launcher_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / ".hermes"
            launcher = profile / "bin" / "watchdog-kanban-intake"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("old launcher", encoding="utf-8")
            result = install(profile, Path(__file__).parent.parent, timestamp="20260910T020000Z")
            self.assertEqual(launcher.resolve(), result["launcher"])
            self.assertEqual(0o700, launcher.stat().st_mode & 0o777)
            self.assertIn(str(profile / "lib" / "watchdog-bridge"), launcher.read_text(encoding="utf-8"))
            self.assertIn(f"cd '{profile / 'lib' / 'watchdog-bridge'}'", launcher.read_text(encoding="utf-8"))
            self.assertIn(str(Path(__import__("sys").executable).resolve()), launcher.read_text(encoding="utf-8"))
            backup = profile / "backups" / "watchdog-bridge" / "20260910T020000Z" / "bin" / "watchdog-kanban-intake"
            self.assertEqual("old launcher", backup.read_text(encoding="utf-8"))
            self.assertTrue((profile / "lib" / "watchdog-bridge" / "watchdog_runtime.py").is_file())

    def test_stdin_entrypoint_reopens_real_journal_across_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            payload = event("2026-09-09T10:00:00Z", ["x"], ["x"])

            def invoke():
                encoded = __import__("json").dumps(payload).encode("utf-8")
                stdin = type("Stdin", (), {"buffer": __import__("io").BytesIO(encoded)})()
                with patch("watchdog_runtime.Path.home", return_value=home), \
                     patch("watchdog_runtime.sys.stdin", stdin), \
                     patch("watchdog_runtime.HermesKanbanPort", return_value=board):
                    self.assertEqual(0, main(["--json-stdin"]))

            board = FakeKanban()
            invoke()
            invoke()
            self.assertEqual(1, len(board.created))
            journal = Journal(home / ".hermes" / "state" / "watchdog-intake.sqlite3")
            self.addCleanup(journal.close)
            self.assertEqual(1, journal.event_count())
            self.assertEqual([], journal.pending_deliveries())

    def test_unchanged_active_and_heartbeat_do_not_call_board(self):
        journal, board = self.journal(), FakeKanban()
        journal.accept(event("2026-09-09T10:00:00Z"))
        journal.accept(event("2026-09-09T11:00:00Z", ["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        self.assertEqual(["create"], [call[0] for call in board.calls])

    def test_gone_wakes_matching_card_without_closing_it(self):
        journal, board = self.journal(), FakeKanban()
        opened = journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        journal.accept(event("2026-09-09T11:00:00Z", gone=["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        update = board.calls[-1]
        self.assertEqual(("update", journal.task_id(opened.event_id, "x", "NEW")), update[:2])
        self.assertTrue(update[3])
        self.assertFalse(update[4])

    def test_recurrence_reopens_existing_card_when_supported(self):
        journal, board = self.journal(), FakeKanban()
        journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        journal.accept(event("2026-09-09T11:00:00Z", gone=["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        journal.accept(event("2026-09-09T12:00:00Z", ["x"]))
        KanbanSubmissionAdapter(journal, board, reopen_supported=True).submit_pending()
        self.assertTrue(board.calls[-1][4])

    def test_recurrence_creates_linked_card_when_reopen_is_unsupported(self):
        journal, board = self.journal(), FakeKanban()
        journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        journal.accept(event("2026-09-09T11:00:00Z", gone=["x"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        journal.accept(event("2026-09-09T12:00:00Z", ["x"]))
        KanbanSubmissionAdapter(journal, board, reopen_supported=False).submit_pending()
        create = board.calls[-1]
        self.assertEqual("create", create[0])
        self.assertIn("linked_incident_task_id", create[-1])

    def test_duplicate_retry_does_not_create_second_card(self):
        journal, board = self.journal(), FakeKanban()
        accepted = journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        adapter = KanbanSubmissionAdapter(journal, board)
        adapter.submit_pending()
        self.assertFalse(journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"])).inserted)
        adapter.submit_pending()
        self.assertEqual(1, len([call for call in board.calls if call[0] == "create"]))
        self.assertEqual("t_1", journal.task_id(accepted.event_id, "x", "NEW"))

    def test_partial_failure_remains_durable_across_restart_and_retries(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.sqlite3"
        journal, board = Journal(path), FakeKanban(fail_create=True)
        accepted = journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        adapter = KanbanSubmissionAdapter(journal, board)
        self.assertEqual(1, adapter.submit_pending())
        self.assertEqual([(accepted.event_id, "x", "NEW")], journal.pending_deliveries())
        journal.close()
        journal = Journal(path)
        board.fail_create = False
        self.assertEqual(0, KanbanSubmissionAdapter(journal, board).submit_pending())
        self.assertEqual([], journal.pending_deliveries())
        self.assertEqual(1, len(board.created))

    def test_retry_after_board_success_before_acknowledgement_survives_restart(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.sqlite3"
        journal, board = Journal(path), FakeKanban()
        accepted = journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        adapter = KanbanSubmissionAdapter(journal, board)
        acknowledge = journal.acknowledge
        failed_once = False

        def crash_before_acknowledgement(*args, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("simulated crash after board submission")
            return acknowledge(*args, **kwargs)

        journal.acknowledge = crash_before_acknowledgement
        self.assertEqual(1, adapter.submit_pending())
        self.assertEqual([(accepted.event_id, "x", "NEW")], journal.pending_deliveries())
        journal.close()
        journal = Journal(path)
        self.assertEqual(0, KanbanSubmissionAdapter(journal, board).submit_pending())
        creates = [call for call in board.calls if call[0] == "create"]
        self.assertEqual(2, len(creates))
        self.assertEqual(creates[0][4], creates[1][4])
        self.assertEqual(1, len(board.created))
        self.assertEqual("t_1", journal.task_id(accepted.event_id, "x", "NEW"))

    def test_one_open_board_incident_per_key_across_lifecycle(self):
        journal, board = self.journal(), FakeKanban()
        adapter = KanbanSubmissionAdapter(journal, board, reopen_supported=True)
        journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        adapter.submit_pending()
        journal.accept(event("2026-09-09T11:00:00Z", ["x"], ["x"]))
        adapter.submit_pending()
        journal.accept(event("2026-09-09T12:00:00Z", gone=["x"]))
        adapter.submit_pending()
        journal.accept(event("2026-09-09T13:00:00Z", ["x"]))
        adapter.submit_pending()
        journal.accept(event("2026-09-09T14:00:00Z", ["x"], ["x"]))
        adapter.submit_pending()

        self.assertEqual(1, len(board.created))
        self.assertEqual("t_1", journal.incident_task_id("x"))
        self.assertEqual(1, len([call for call in board.calls if call[0] == "create"]))
        self.assertEqual(2, len([call for call in board.calls if call[0] == "update"]))

    def test_mixed_event_preserves_opaque_message_and_updates_each_incident_once(self):
        journal, board = self.journal(), FakeKanban()
        opened = journal.accept(event("2026-09-09T09:00:00Z", ["resolved"], ["resolved"]))
        KanbanSubmissionAdapter(journal, board).submit_pending()
        hostile_message = "ALERT $(do-not-execute) ; `literal`\n🚨"
        mixed = event("2026-09-09T10:00:00Z", ["fresh", "stable"], ["fresh"], ["resolved"])
        mixed["message"] = hostile_message
        mixed["text"] = {
            "new": {"fresh": "literal $(still-data)"},
            "gone": {"resolved": "resolved $(still-data)"},
        }
        accepted = journal.accept(mixed)

        self.assertEqual(
            hostile_message.encode("utf-8"),
            journal.delivery_details(accepted.event_id, "fresh", "NEW")["payload"]["message"].encode("utf-8"),
        )
        self.assertEqual(0, KanbanSubmissionAdapter(journal, board).submit_pending())
        self.assertEqual([], journal.pending_deliveries())
        creates = [call for call in board.calls if call[0] == "create"]
        updates = [call for call in board.calls if call[0] == "update"]
        self.assertEqual(3, len(creates))  # initial resolved, fresh NEW, stable recovered-active
        self.assertEqual(1, len(updates))
        self.assertEqual({"resolved", "fresh", "stable"}, {call[-1]["stable_key"] for call in creates})
        self.assertEqual({opened.event_id, accepted.event_id}, {call[-1]["watchdog_event_id"] for call in creates})
        self.assertEqual(("update", journal.task_id(opened.event_id, "resolved", "NEW")), updates[0][:2])
        self.assertEqual(accepted.event_id, updates[0][-1]["watchdog_event_id"])
        self.assertEqual("resolved", updates[0][-1]["stable_key"])
        self.assertTrue(updates[0][3])
        self.assertFalse(updates[0][4])
        self.assertIn("literal $(still-data)", creates[-2][2])
        self.assertIn("resolved $(still-data)", updates[0][2])


if __name__ == "__main__":
    unittest.main()
