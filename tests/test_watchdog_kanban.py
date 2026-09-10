import tempfile
import unittest
from pathlib import Path

from watchdog_boundary import Journal
from watchdog_kanban import KanbanSubmissionAdapter


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

    def test_partial_failure_remains_durable_and_retries(self):
        journal, board = self.journal(), FakeKanban(fail_create=True)
        accepted = journal.accept(event("2026-09-09T10:00:00Z", ["x"], ["x"]))
        adapter = KanbanSubmissionAdapter(journal, board)
        self.assertEqual(1, adapter.submit_pending())
        self.assertEqual([(accepted.event_id, "x", "NEW")], journal.pending_deliveries())
        board.fail_create = False
        self.assertEqual(0, adapter.submit_pending())
        self.assertEqual([], journal.pending_deliveries())

    def test_retry_after_board_success_before_acknowledgement_reuses_incident_key(self):
        journal, board = self.journal(), FakeKanban()
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
        self.assertEqual(0, adapter.submit_pending())
        creates = [call for call in board.calls if call[0] == "create"]
        self.assertEqual(2, len(creates))
        self.assertEqual(creates[0][4], creates[1][4])
        self.assertEqual("t_1", journal.task_id(accepted.event_id, "x", "NEW"))


if __name__ == "__main__":
    unittest.main()