# Watchdog lifecycle fixture map

`tests/test_watchdog_lifecycle_e2e.py` exercises the production dispatch, intake,
SQLite journal, Kanban database adapter, and closure reporter with only external
delivery replaced by an in-memory recorder. `HOME`, `HERMES_KANBAN_DB`, producer
state, spool, journal, and launcher are all temporary.

| Acceptance criterion | Fixture / assertion |
| --- | --- |
| NEW creation and Foreman ownership | `test_complete_incident_lifecycle_is_durable_deduplicated_and_silent_until_closure` creates `proxy-errors` and asserts a Foreman-owned task. |
| Repeated NEW/active deduplication | The second snapshot leaves the task count unchanged. |
| Mixed NEW + GONE | The third snapshot creates `car-timeout` and updates `proxy-errors`. |
| Standalone RESOLVIDO without closure | The fourth snapshot updates the existing card; the delivery recorder remains empty. |
| Independent closure and exactly-once notice | A `watchdog_resolvido` completion is rejected, an independent completion succeeds, and its replay produces no second delivery. |
| Recurrence | A later NEW creates a linked incident card because the concrete runtime uses linked-card fallback. |
| Daily Brain OK | `test_daily_healthy_snapshot_malformed_and_oversized_inputs_create_no_work` asserts no board task and a deterministic informational outcome. |
| Malformed / oversized safety | The same test rejects missing fields and a message over 16 KiB without creating work. |
| Board / launcher recovery and durable retry | `test_unavailable_board_recovers_from_spool_without_duplicate_card` retains one mode-0600 spool file, retries it, and creates one card. |
| Tier-3 routing and unavailable gateway recovery | `test_gateway_retry_and_tier3_are_durable_and_exactly_once` fails one mocked delivery, retries the same idempotency key, and emits one tier-3 message. |
| No raw alert delivery | The lifecycle test asserts the delivery recorder is empty through NEW and GONE intake. |
| Failure fallback | `test_dispatch_failure_is_non_delivering_and_reports_automation_failure` checks fail-closed stderr and the producer's explicit automation-failure fallback. |
| Bounded continuation | Created tasks assert `max_runtime_seconds=86400`, `max_retries=3`, and card policy text `max-runs 3`. |

Run:

    python -m unittest tests.test_watchdog_dispatch tests.test_watchdog_intake tests.test_watchdog_kanban tests.test_watchdog_closure tests.test_watchdog_lifecycle_e2e -v
