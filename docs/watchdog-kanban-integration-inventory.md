# Watchdog intake integration inventory

Read-only findings made on 2026-09-09. Paths are intentionally external to this task workspace and are not modified by this task.

| Integration point | Evidence | Contract implication |
|---|---|---|
| Watchdog producer | `~/hermes/bin/watchdog.sh:32-36` creates `key|text`; `:301-323` serializes current active keys and writes `~/.hermes/watchdog-state.json`; `:328-335` emits transition output | Emit a structured event immediately after the existing diff is calculated. Keep `message` independent from keys. The current state JSON is not a durable delivery journal. |
| Daily supplementary signals | `~/hermes/bin/watchdog_v3.py:32-156` prints human `ALARM`/`INFO` lines | Treat these lines as opaque message/diagnostic source data only. They do not establish stable identity unless a stable key is supplied by `watchdog.sh`. |
| Existing board | `~/.hermes/kanban.db` has `tasks`, `task_events`, `task_runs`, `task_links`, comments, attachments, and notify subscriptions. `tasks.idempotency_key` exists and is indexed. | Use a separate intake journal first, then structured Kanban API calls keyed by bounded deterministic incident ID. |
| Board resolution | `~/.hermes/hermes-agent/hermes_cli/kanban_db.py:3-9`; connector `kanban_db_connect.py:667-733` | Do not hard-code an alert-derived board name. Default board resolution selects the installed shared DB when no explicit board is configured. |
| Foreman routing | `~/.hermes/config.yaml:385-399` has `kanban.orchestrator_profile: foreman`; profiles include `foreman`. | Resolve from config/API; pass the constant profile value as structured data, never interpolate the alert text. |
| Existing safety controls | `kanban_db_connect.py:642-659` applies WAL, `synchronous=FULL`, foreign keys, secure delete, and cell checks. | Intake journal must use its own SQLite transaction and equivalent crash-aware semantics before external submission. |

No external file, profile configuration, state file, or database was changed. No live backup was necessary. If an implementation task changes a live configuration or profile file, it must first put a timestamped backup into that task's workspace.
