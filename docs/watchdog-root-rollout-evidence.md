# Watchdog-to-Foreman rollout evidence

Audited on 2026-09-16 against commit `3f58a2e9a2d7c75fbc154eb78ef5c5c2ae08a85f` and the live profile.

## Acceptance matrix

| Criterion | Evidence |
| --- | --- |
| Foreman owns normal intake | `tests/test_watchdog_lifecycle_e2e.py:134-143`; live cards `t_d6c20bb9`, `t_128db05b`, and `t_9d11a5b2` are `created_by=watchdog`, assigned to `foreman`, and carry TTL 86400/max-runs 3. |
| Stable-key idempotency | `tests/test_watchdog_lifecycle_e2e.py:118-143`; live journal maps each of `cron-falhando`, `jobs-mortos`, and `proxy-erros` to one incident key and one task. |
| RESOLVIDO is not proof | `tests/test_watchdog_lifecycle_e2e.py:145-169` rejects `verification_source=watchdog_resolvido`; `tests/test_watchdog_closure.py` covers the same boundary directly. |
| Recurrence retained | `tests/test_watchdog_lifecycle_e2e.py:171-179` verifies a linked recurrence with bounded policy. |
| Failed intake cannot silently drop alerts | `bin/watchdog_dispatch.py:61-86` atomically spools before submission and retains failure; `tests/test_watchdog_lifecycle_e2e.py:215-238,258-269` verifies recovery and explicit fallback. |
| Raw initial alert is suppressed | `bin/watchdog.sh:61-95` sends normal events only to the intake bridge; `tests/test_watchdog_lifecycle_e2e.py:143` proves no delivery before closure. |
| Closure/tier-3 is durable and exactly once | `tests/test_watchdog_lifecycle_e2e.py:145-169,240-256`; delivery uses stable idempotency keys and retries pending records. |
| Bounded operation | Live cards have `max_runtime_seconds=86400` and `max_retries=3`; their bodies say `TTL 24h; max-runs 3`; continuation policy is also tested at `tests/test_watchdog_lifecycle_e2e.py:139-141,179`. |
| Historical incidents seeded | Live board contains the five requested 2026-09-09 categories: car log (`t_f5f4851f`), markdown links (`t_c9ae1426`), dead extract-atoms-drain (`t_51fb825d`), Azure proxy timeouts (`t_3124a037`), and ledger versus Azure billing mismatch (`t_affddb11`). |
| Non-delivering smoke | Archived live card `t_f4465637` was created by `watchdog`; normal smoke/intake did not create an outbound closure record. |
| Producers activated | `launchctl print` reports both `com.gustavo.brain-watchdog` (08:30 calendar trigger, 8 runs, last exit 0) and `com.gustavo.brain-watchdog-hourly` (3600-second interval, 23 runs, last exit 0), both targeting `/Users/gustavosouza/hermes/bin/watchdog.sh`. Installed bridge files exist at the fixed profile paths. |
| Journal/board evidence | Live journal contains five accepted event IDs and per-key state for three current producer keys. Subsequent events are `UNCHANGED_ACTIVE` and do not create duplicate cards. |

## Verification

    python -m unittest tests.test_watchdog_dispatch tests.test_watchdog_intake tests.test_watchdog_kanban tests.test_watchdog_closure tests.test_watchdog_lifecycle_e2e -v
    Ran 49 tests in 4.904s
    OK

    git diff --check
    # no output, exit 0

    python -m compileall -q watchdog_boundary.py watchdog_kanban.py watchdog_runtime.py watchdog_closure.py bin/watchdog_dispatch.py
    # no output, exit 0

## Remediation deployment (2026-09-16)

The reviewed bridge was redeployed with `python watchdog_install.py --profile /Users/gustavosouza/.hermes`. Repository and installed SHA-256 pairs now match:

- `watchdog_boundary.py`: `356de36a85c297d24abdcb75bccc38b04730be9c86670b24fa9adc3a7657e5c7`
- `watchdog_kanban.py`: `daca1b14f6a91b350739587ce9ad8b78cf5ab55bed9cdfe13f08a20f4ee80dfb`
- `watchdog_runtime.py`: `e6377898ac4a906ec702842c5cbf03deee4071021ab42f478870aa9ba71124ac`
- `watchdog_closure.py`: `099a7280d40a65eaf6130441aabb8d8d1be037e1681cbd480d8642207c5cd466`

The focused command in Verification was rerun and reported `Ran 49 tests ... OK`. Read-only `launchctl print` reported 8 daily runs and 23 hourly runs, both with last exit code 0. A heartbeat submitted through the installed launcher produced event `wd-v1-73ea8a3995e6065b01f48984d8361294` with `HEARTBEAT/submitted/heartbeat_only`, no Kanban task ID, and zero pending or failed delivery rows.

## Residual risk and downstream verification

The live journal records the first board submissions with outcome code `kanban_submission_failed` even though the same rows are in `submitted` state and have valid task IDs. This is misleading historical telemetry, not dropped work: board cards exist, later producer passes are `UNCHANGED_ACTIVE`, and no duplicate cards appeared. The open `proxy-erros` card remains normal incident work for Foreman and is not a rollout defect.

The historical seed matrix is not yet complete: the board has car-log (`t_f5f4851f`), markdown links (`t_c9ae1426`), dead extract-atoms-drain (`t_51fb825d`), Azure proxy timeouts (`t_3124a037`), and ledger/billing (`t_affddb11`), but no separate gateway-restart/EX_TEMPFAIL incident. Ledger/billing does not replace the gateway-restart category required by root acceptance criterion 9. Downstream installation/seeding card `t_33ffd4c5` must remediate and verify that missing historical incident before root approval.

The live producer also intentionally differs from this branch in its newer Azure proxy attribution block, but it still lacks this branch's strict `v3` stable-key sanitization. Downstream card `t_33ffd4c5` must reconcile that isolated boundary change without overwriting the independently deployed proxy repair.
