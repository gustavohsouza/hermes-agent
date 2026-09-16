"""Concrete, in-process Watchdog bridge to Hermes's shared Kanban API."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping

from watchdog_boundary import Journal, read_json_stdin
from watchdog_kanban import KanbanSubmissionAdapter


class HermesKanbanPort:
    """Structured KanbanPort backed by ``hermes_cli.kanban_db``.

    The bridge passes event data as API arguments only. It never invokes a shell
    or constructs a command from Watchdog content.
    """

    def __init__(self, *, board: str | None = None, api: Any | None = None):
        if api is None:
            from hermes_cli import kanban_db, kanban_db_connect
            api = _HermesApi(kanban_db, kanban_db_connect)
        self.api = api
        self.board = board

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        with self.api.connect_closing(board=self.board) as connection:
            yield connection

    def create_or_update(self, *, title: str, body: str, assignee: str,
                         idempotency_key: str, metadata: dict[str, Any]) -> str:
        with self._connection() as connection:
            task_id = self.api.create_task(
                connection,
                title=title,
                body=_comment(body, metadata),
                assignee=assignee,
                created_by="watchdog",
                idempotency_key=idempotency_key,
                max_runtime_seconds=24 * 60 * 60,
                max_retries=3,
            )
            return task_id

    def update(self, *, task_id: str, body: str, wake: bool = False,
               reopen: bool = False, metadata: dict[str, Any] | None = None) -> str:
        with self._connection() as connection:
            task = self.api.get_task(connection, task_id)
            if task is None:
                raise RuntimeError("unknown_kanban_task")
            if reopen and task.status == "review" and not self.api.reopen_review_task(connection, task_id):
                raise RuntimeError("kanban_reopen_failed")
            if wake and task.status in {"blocked", "scheduled"} and not self.api.unblock_task(connection, task_id):
                raise RuntimeError("kanban_wake_failed")
            self.api.add_comment(connection, task_id, "watchdog", _comment(body, metadata or {}))
            return task_id


class _HermesApi:
    def __init__(self, kanban_db: Any, kanban_db_connect: Any):
        self._db = kanban_db
        self._connect = kanban_db_connect

    def connect_closing(self, *, board: str | None):
        return self._connect.connect_closing(board=board)

    def create_task(self, connection: Any, **kwargs: Any) -> str:
        return self._db.create_task(connection, **kwargs)

    def get_task(self, connection: Any, task_id: str) -> Any:
        return self._db.get_task(connection, task_id)

    def reopen_review_task(self, connection: Any, task_id: str) -> bool:
        return self._db.reopen_review_task(connection, task_id)

    def unblock_task(self, connection: Any, task_id: str) -> bool:
        return self._db.unblock_task(connection, task_id)

    def add_comment(self, connection: Any, task_id: str, author: str, body: str) -> int:
        return self._db.add_comment(connection, task_id, author, body)


def _comment(body: str, metadata: Mapping[str, Any]) -> str:
    return body + "\n\nWatchdog metadata: " + repr(dict(metadata))


def submit_watchdog_event(event: Mapping[str, Any], journal_path: Path,
                           port: HermesKanbanPort | None = None, *,
                           board: str | None = None) -> int:
    """Persist one structured event and submit durable pending deliveries."""
    journal = Journal(journal_path)
    try:
        journal.accept(event)
        # Hermes can only reopen review-state cards directly. Other recurrence
        # states use the adapter's explicitly linked-card fallback.
        return KanbanSubmissionAdapter(
            journal, port or HermesKanbanPort(board=board), reopen_supported=False,
        ).submit_pending()
    finally:
        journal.close()


def main(argv: list[str] | None = None) -> int:
    """Fixed stdin entrypoint: ``python -m watchdog_runtime --json-stdin``."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["--json-stdin"]:
        raise SystemExit("usage: python -m watchdog_runtime --json-stdin")
    event = read_json_stdin(sys.stdin.buffer)
    state_dir = Path.home() / ".hermes" / "state"
    return submit_watchdog_event(event, state_dir / "watchdog-intake.sqlite3")


if __name__ == "__main__":
    raise SystemExit(main())
