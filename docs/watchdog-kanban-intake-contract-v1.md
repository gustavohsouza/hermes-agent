# Watchdog to Kanban intake contract v1

Status: implementation contract

## Scope and trust boundary

This contract converts an already-computed Watchdog snapshot into a durable event before any Kanban mutation. It does not execute Watchdog text, build shell commands from it, or reinterpret it as structured data. `message` is opaque UTF-8 data.

The current producer is `~/hermes/bin/watchdog.sh`:

- it emits stable keys separately from human text (`add`, lines 32-36);
- it persists and diffs the `active` key collection (lines 312-323);
- check mode emits NEW and RESOLVIDO text (lines 328-335).

The destination is the default shared Kanban DB at `~/.hermes/kanban.db`. Hermes resolves boards as: explicit `board` argument, `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_DB`, then current-board file, then `default` (`hermes_cli/kanban_db.py`, lines 3-9). The existing DB has a `tasks.idempotency_key` column and index. Foreman is configured as `kanban.orchestrator_profile: foreman` in `~/.hermes/config.yaml`.

## Invocation and wire format

The implementation accepts exactly one of:

1. A structured in-process mapping supplied to the intake API.
2. One UTF-8 JSON document supplied through standard input.

A command-line wrapper must use a fixed executable path and fixed option names. It must never form a shell command from any event field, use `eval`, call a shell, or place event content in a command argument. The implemented integration is stdin:

    ~/.hermes/bin/watchdog-kanban-intake

The wrapper must read bounded bytes, decode strict UTF-8, parse one JSON object, and reject trailing non-whitespace content.

Top-level schema:

    {
      "version": 1,
      "mode": "check" | "daily",
      "observed_at": "RFC3339 timestamp with offset",
      "message": "exact Watchdog message",
      "active_keys": ["stable-key", ...],
      "new_keys": ["stable-key", ...],
      "gone_keys": ["stable-key", ...],
      "text": {
        "summary": "human-readable bounded summary",
        "new": {"stable-key": "optional bounded text"},
        "gone": {"stable-key": "optional bounded text"}
      },
      "diagnostics": {"optional": "bounded scalar context"}
    }

Unknown top-level fields are rejected. `text` and `diagnostics` may be omitted and normalize to empty objects. `check` requires all three key collections. `daily` requires `active_keys` even when it is explicitly empty: absence rejects before any journal write, while `"active_keys": []` is a valid empty snapshot. For `daily`, `new_keys` and `gone_keys` are forbidden and reject if present; intake derives those transitions solely from the two durable snapshots. This makes an omitted snapshot fail closed rather than resolving every open incident.

## Bounds and validation

All strings are Unicode scalar text after strict UTF-8 decoding. Reject NUL (`U+0000`) and C0 controls other than tab, LF, and CR in `message`, summaries, and diagnostic strings. Do not normalize Unicode content. Normalize stable keys only as specified below.

| Field | Type and limit | Treatment |
|---|---|---|
| encoded event | UTF-8 JSON object, max 64 KiB | reject if over limit or invalid JSON |
| version | integer exactly `1` | reject otherwise |
| mode | exact lower-case `check` or `daily` | reject otherwise |
| observed_at | RFC3339, UTC offset required, year 2000-2100 | reject otherwise |
| message | string, max 16,384 UTF-8 bytes | reject, never truncate, because it is the exact opaque Watchdog message |
| active/new/gone key input | arrays of at most 256 strings | `check`: all required; `daily`: only `active_keys` required and `new_keys`/`gone_keys` forbidden; reject type/count violations; canonicalize valid values |
| stable key | 1-96 ASCII chars matching `[a-z0-9][a-z0-9._-]{0,95}` | reject invalid keys |
| text.summary | string, max 2,048 UTF-8 bytes | deterministic truncate with `…[truncated]` suffix |
| text per key | object with canonical keys, at most 256 entries; each string max 1,024 UTF-8 bytes | discard entries for unknown keys; truncate string deterministically |
| diagnostics | object, at most 32 entries; keys match stable-key rule; values scalar string/number/bool/null | reject malformed shape; strings truncate to 512 UTF-8 bytes; numbers must be finite |

Truncation is byte-aware: retain the longest valid UTF-8 prefix such that the retained bytes plus the literal UTF-8 suffix `…[truncated]` fit the field limit. A truncation marker is recorded in persisted normalization metadata. Secrets are not redacted by guessing. Instead, only allowlisted diagnostic keys may enter durable storage or logs; all other diagnostic keys are rejected. The initial allowlist is `source`, `exit_code`, `collector`, and `duration_ms`. Never log `message`, text, or diagnostic values at info level; logs may include only event ID, canonical keys, mode, and validation reason.

## Canonicalization and invariants

A stable-key collection canonicalizes by validating each key, removing duplicate values, and sorting ASCII lexicographically. The persisted event always contains canonical `active_keys`, `new_keys`, and `gone_keys`.

Required invariants after canonicalization:

- `new_keys` is a subset of `active_keys`.
- `gone_keys` is disjoint from `active_keys`.
- no key occurs in both `new_keys` and `gone_keys`.
- a `daily` event with `active_keys: []` is an explicit empty full snapshot, not a heartbeat; omitted `active_keys` is invalid.
- `daily` contains no caller-supplied `new_keys` or `gone_keys`; both are derived after ordering is accepted.
- `check` may have all collections empty only as a heartbeat-only check event.

Violation rejects the entire event before any persistent write.

## Deterministic identities

Use canonical JSON with sorted keys, compact separators, and UTF-8, after validation and truncation. Calculate SHA-256 over this exact identity projection:

    {
      "version": 1,
      "mode": mode,
      "observed_at": observed_at normalized to UTC RFC3339 with microseconds,
      "message": message,
      "active_keys": active_keys,
      "new_keys": new_keys,
      "gone_keys": gone_keys,
      "text": text,
      "diagnostics": diagnostics
    }

`event_id` is `wd-v1-` plus the first 32 lowercase hex characters of that digest. The full digest is persisted for collision detection. If an existing `event_id` has a different full digest, fail closed.

Incident idempotency keys must not include message, summary, or diagnostic text. For each stable key, derive:

    wd-incident-v1-<key>-<first 24 hex of SHA256("wd-incident-v1\0" + key)>

This key is bounded (maximum 136 characters), stable across rewording, and suitable for `tasks.idempotency_key`. A task title/body may use safely bounded display text only after the event is durable; task submission must never use an alert-derived shell command.

## Durability and retry protocol

Persist accepted data in an intake-owned SQLite journal located beneath the implementation workspace/state directory, not in the Watchdog state JSON. The journal has these minimum tables:

    watchdog_events(
      event_id TEXT PRIMARY KEY,
      full_digest TEXT NOT NULL UNIQUE,
      payload_json TEXT NOT NULL,
      received_at TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('accepted','submitting','submitted','failed')),
      attempt_count INTEGER NOT NULL DEFAULT 0,
      last_error_code TEXT,
      submitted_at TEXT
    )

    watchdog_incident_deliveries(
      event_id TEXT NOT NULL REFERENCES watchdog_events(event_id),
      stable_key TEXT NOT NULL,
      transition TEXT NOT NULL,
      incident_key TEXT NOT NULL,
      kanban_task_id TEXT,
      delivery_state TEXT NOT NULL CHECK(delivery_state IN ('pending','submitted','failed')),
      outcome_code TEXT,
      PRIMARY KEY(event_id, stable_key, transition)
    )

    watchdog_incident_state(
      stable_key TEXT PRIMARY KEY,
      lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN ('open','gone')),
      incident_key TEXT NOT NULL UNIQUE,
      kanban_task_id TEXT,
      latest_applied_at TEXT NOT NULL,
      latest_applied_event_id TEXT NOT NULL REFERENCES watchdog_events(event_id),
      latest_applied_digest TEXT NOT NULL,
      recurrence_root_event_id TEXT REFERENCES watchdog_events(event_id),
      recurrence_version INTEGER NOT NULL DEFAULT 0
    )

The state row is the sole ordering and one-open-incident authority for a key. `latest_applied_at`, `latest_applied_event_id`, and `latest_applied_digest` record the last state-changing transition, not merely receipt. `recurrence_root_event_id` identifies the first incident event in the task lineage and `recurrence_version` increments only when a gone lineage becomes active again.

Within one `BEGIN IMMEDIATE` transaction: insert an event in `accepted` state; read and compare every affected incident-state row; derive transitions; insert pending deliveries; and insert/update every state row whose transition changes lifecycle or recovery marker. The transaction rejects same-time/different-digest conflicts and records stale events without modifying state rows. Commit before any Kanban action. Only then may the worker submit Kanban actions. A duplicate full digest is a successful no-op and resumes any pending/failed deliveries. A crash between journal commit and submission is recovered by selecting pending/failed deliveries. A crash after Kanban submission but before journal acknowledgement is recovered by querying/creating with the deterministic incident idempotency key, then recording the task ID. The implementation must not mark `submitted` until task ID is known and the journal update commits. The task ID write and delivery acknowledgement are atomic in a subsequent `BEGIN IMMEDIATE` transaction; retries may only reuse that row's deterministic incident key.

## Transition semantics

Process all keys in sorted order. An event can contain mixed transitions.

| Condition per stable key | Transition | Required effect |
|---|---|---|
| key in `new_keys`, no state row or gone state row | NEW | create or wake one incident via its stable idempotency key; set state `open`, advance latest-applied marker; if previously gone, this is recurrence and increments recurrence version |
| key in `new_keys`, already open | duplicate-NEW | no task or alert; record delivery as no-op and do not advance latest-applied marker |
| key active, not new, already open | unchanged-active | record event linkage; no duplicate task or alert; do not advance latest-applied marker |
| key active, not new, with no state row | recovered-active | create/wake one incident with deterministic key, set `open`, and advance latest-applied marker; delivery records diagnostic `active_without_known_incident` |
| key in `gone_keys` with open incident | GONE / RESOLVIDO | append bounded resolution context and wake/update matching card; never auto-close it; set state `gone` and advance latest-applied marker |
| key in `gone_keys` with gone incident | duplicate-GONE | durable no-op, no task action, and do not advance latest-applied marker |
| gone key with no known incident | gone-orphan | durable no-op plus diagnostic code; no task creation |
| no keys changed | heartbeat-only | persist event; no Kanban task action |
| key active after a prior gone incident | recurrence | process only if newer than the gone marker; reopen the previous task when transport supports it and policy permits, otherwise create one explicitly linked recurrence task; set `open`, increment recurrence version, and advance latest-applied marker |
| duplicate event identity | duplicate | no-op except unfinished delivery recovery |
| event older than the most recent applied event for a key | stale | persist as stale; no state/task mutation |
| same timestamp but distinct full digest for a key | out-of-order conflict | persist and fail delivery for manual review; do not guess ordering |

At all times, a stable key has at most one open incident. A GONE event never closes a card automatically. `daily` supplies the full active snapshot, and `check` supplies explicit transition collections. For `daily`, after the snapshot passes per-key ordering, keys removed from the prior accepted daily snapshot derive GONE and keys added derive NEW; only the explicit `active_keys: []` can derive removal of all prior keys. The latest accepted daily snapshot is replaced atomically with the event/state update. Recurrence ordering is therefore: stale/identical events are no-ops, a newer GONE sets `gone`, and only a later NEW or derived-added key may increment recurrence. Same-time distinct digests remain conflicts rather than recurrence guesses.

## Board and assignee resolution

The intake code resolves board and target through configuration/API calls, not alert text:

1. Resolve the shared board using the normal Hermes resolution chain; the installed shared default is `~/.hermes/kanban.db`.
2. Use the bridge's fixed `foreman` assignee; the current runtime does not read `kanban.orchestrator_profile`.
3. Call the Kanban API with structured title/body/idempotency fields. Do not invoke `hermes kanban` through a shell.

No profile configuration was modified by this contract task. If a later implementation changes a live profile/config file, it must first copy a timestamped backup into its task workspace.

## Submission adapter and operator procedure

`watchdog_kanban.KanbanSubmissionAdapter` is the transport boundary. It accepts a `Journal` and a structured `KanbanPort`; it never calls a shell, builds a command, or derives a board, assignee, path, or query from event content. The port contract is:

    create_or_update(title, body, assignee, idempotency_key, metadata) -> task_id
    update(task_id, body, wake=False, reopen=False, metadata=None) -> task_id

After `Journal.accept(event)`, construct the adapter with the configured `foreman` assignee and call `submit_pending()`. The adapter reads only durable `pending` or `failed` delivery rows. It maps `NEW` and recovered-active transitions to `create_or_update` with the stable incident key, `GONE` to a waking `update` with `reopen=False`, and recurrence to a waking reopen or a new card with `linked_incident_task_id` metadata. Heartbeats and unchanged-active rows have no pending board operation.

Install the reviewed source with `watchdog_install.py` as described below. Do not
store the journal in `watchdog-state.json`; the concrete runtime always uses
`~/.hermes/state/watchdog-intake.sqlite3`. Retention is operationally owned by the
deployment. The installer, not an operator-authored copy command, creates
timestamped backups of replaced bridge files. To roll back, stop the producer hook
and restore that backup; do not delete the journal or board cards, because retained
pending deliveries are the recovery record. Re-enable the adapter later against
the same journal to resume safely.

Example integration is in-process and does not use interpolation or placeholders:

    from pathlib import Path
    from watchdog_runtime import submit_watchdog_event

    failures = submit_watchdog_event(
        structured_watchdog_event,
        Path.home() / ".hermes/state/watchdog-intake.sqlite3",
    )
    if failures:
        raise RuntimeError("watchdog_kanban_submission_failed")

### Default-profile installation, activation, retention, and rollback

The deployable bridge comprises `watchdog_boundary.py`, `watchdog_kanban.py`, and
`watchdog_runtime.py`. Run `python3 watchdog_install.py --profile "$HOME/.hermes" --source "$PWD"` from a reviewed checkout
to copy those modules into the fixed `~/.hermes/lib/watchdog-bridge/` deployment
and create the owner-only `~/.hermes/bin/watchdog-kanban-intake` launcher. The
installer backs up every replaced deployed module or launcher beneath
`~/.hermes/backups/watchdog-bridge/<UTC timestamp>/` before overwriting it. The
launcher exposes only a JSON stdin interface and invokes
`python -m watchdog_runtime --json-stdin`; it accepts no event-derived command
arguments and passes the decoded mapping only to `submit_watchdog_event()`. That
function persists at `~/.hermes/state/watchdog-intake.sqlite3`, then invokes the
concrete `HermesKanbanPort`, which calls `hermes_cli.kanban_db_connect.connect_closing`,
`create_task`, `get_task`, `reopen_review_task`, and `add_comment` directly. No
`hermes kanban` subprocess or shell interpolation is involved.

Install with the exact command above, then invoke the installed launcher only
from an application-owned Watchdog hook after it has constructed and validated the
structured event. If a different default-profile launcher or `~/.hermes/config.yaml`
must change, copy that file to a timestamped backup before editing it. Configure the
hook to start `~/.hermes/bin/watchdog-kanban-intake` with a fixed argument array and
write the one encoded JSON object to the process's standard input, as shown in the
operator runbook; never interpolate fields into a command string. The port resolves
the normal configured shared board and sends created cards to the constant `foreman`
assignee.

`Journal` creates or repairs the state directory at mode `0700` and its SQLite
file plus extant WAL/SHM sidecars at `0600`; it raises `insecure_journal_permissions`
if it cannot establish those modes. Retain the journal and its WAL/SHM companions
until an operator has verified `pending_deliveries()` is empty and preserved an
offline SQLite backup. Cleanup is an explicit maintenance operation, never part
of intake or rollback. To roll back, disable only the Watchdog hook, restore the
timestamped launcher/config backup, and retain the journal and existing board
cards. Re-enable the same fixed command against the same journal to replay
retained pending or failed deliveries safely.

The executable activation, verification, WAL-aware backup, retention, and rollback
procedure is `watchdog-bridge-operations.md`. Installation does not edit or enable
the producer hook. The installed runtime deliberately creates a linked recurrence
card instead of reopening the prior card, preserving `linked_incident_task_id`.

Downstream callers may invoke the integration directly without shell interpolation:

    submit_watchdog_event(event, Path.home() / ".hermes/state/watchdog-intake.sqlite3")

## Acceptance tests required of the implementation cards

- structured mapping and stdin JSON produce the same canonical payload and event ID;
- invalid JSON, over-64-KiB input, NUL/control input, malformed timestamp, invalid mode, invalid key, and non-finite diagnostics reject before journal writes;
- Unicode message preservation is byte-for-byte after UTF-8 decoding and re-encoding;
- bounded summary/diagnostic truncation is deterministic and valid UTF-8;
- order and duplicate variation in key collections yields the same canonical collection and identity;
- text variation changes event ID but not the stable incident idempotency key;
- duplicate delivery and simulated crash after journal commit resume without a second card;
- a daily payload without `active_keys` rejects before journal write, while a daily payload with explicit `"active_keys": []` is accepted and derives GONE only from a newer prior daily snapshot; supplied `new_keys` or `gone_keys` on daily reject;
- NEW, unchanged, GONE, mixed, heartbeat, recurrence, stale, and out-of-order cases execute the effects listed above.

## Closure and escalation interface

`watchdog_closure.ClosureReporter` owns outbound incident reports. Call
`complete(stable_key, task_id, occurrence_id, outcome, verification_source="independent")` only
after the Foreman task has recorded one allowed classification, treatment or
dismissal, follow-through, empirical verification, and residual risk. A
`verification_source` of `watchdog_resolvido` is rejected: RESOLVIDO remains a
wake/update signal, never closure proof. `tier-3` is rejected as a completion
classification and may contact Gustavo before closure only through
`escalate_tier3(...)`.

Both operations first insert a deterministic report row into
`watchdog_reports`; delivery retries consume only pending/failed rows and reuse
the same `wd-closure-v1-*` or `wd-tier3-v1-*` idempotency key. Sent rows are no
longer selectable, so repeated completion events cannot send another report.
The fixed destination is `whatsapp:117484669640820@lid`. Integrators provide a
`DeliveryPort.send(target, text, idempotency_key)` implementation; unit and E2E
tests must use a test double and must not contact the real gateway.

`deterministic_heartbeat_outcome(event)` permits no-action auto-closure only for
a version-1 daily snapshot whose `active_keys` is explicitly empty. Its output
is a complete informational outcome with point-in-time residual risk; any active
key or non-daily payload rejects.
