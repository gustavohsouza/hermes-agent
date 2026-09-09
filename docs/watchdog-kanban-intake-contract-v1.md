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
2. One UTF-8 JSON document supplied through standard input or an argv value that contains only serialized JSON.

A command-line wrapper must use a fixed executable path and fixed option names. It must never form a shell command from any event field, use `eval`, call a shell, or place event content in a command argument other than the single JSON payload passed directly to the process API. The preferred integration is stdin:

    watchdog-intake --json-stdin

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

Unknown top-level fields are rejected. `text` and `diagnostics` may be omitted and normalize to empty objects. Missing collections normalize to empty arrays only for `daily`; `check` requires every collection so that its transition semantics are explicit.

## Bounds and validation

All strings are Unicode scalar text after strict UTF-8 decoding. Reject NUL (`U+0000`) and C0 controls other than tab, LF, and CR in `message`, summaries, and diagnostic strings. Do not normalize Unicode content. Normalize stable keys only as specified below.

| Field | Type and limit | Treatment |
|---|---|---|
| encoded event | UTF-8 JSON object, max 64 KiB | reject if over limit or invalid JSON |
| version | integer exactly `1` | reject otherwise |
| mode | exact lower-case `check` or `daily` | reject otherwise |
| observed_at | RFC3339, UTC offset required, year 2000-2100 | reject otherwise |
| message | string, max 16,384 UTF-8 bytes | reject, never truncate, because it is the exact opaque Watchdog message |
| active/new/gone key input | arrays of at most 256 strings | reject type/count violations; canonicalize valid values |
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
- `daily` may have all collections empty and is then heartbeat-only.
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
      PRIMARY KEY(event_id, stable_key, transition)
    )

Within one `BEGIN IMMEDIATE` transaction: insert an event in `accepted` state, insert its pending deliveries, and commit. Only then may the worker submit Kanban actions. A duplicate full digest is a successful no-op and resumes any pending/failed deliveries. A crash between journal commit and submission is recovered by selecting pending/failed deliveries. A crash after Kanban submission but before journal acknowledgement is recovered by querying/creating with the deterministic incident idempotency key, then recording the task ID. The implementation must not mark `submitted` until task ID is known and the journal update commits.

## Transition semantics

Process all keys in sorted order. An event can contain mixed transitions.

| Condition per stable key | Transition | Required effect |
|---|---|---|
| key in `new_keys`, no open incident | NEW | create or wake one incident via its stable idempotency key |
| key active, not new, already open | unchanged-active | record event linkage; no duplicate task or alert |
| key in `gone_keys` with known incident | GONE / RESOLVIDO | append bounded resolution context and wake/update matching card; never auto-close it |
| gone key with no known incident | gone-orphan | durable no-op plus diagnostic code; no task creation |
| no keys changed | heartbeat-only | persist event; no Kanban task action |
| key active after a prior gone incident | recurrence | reopen the previous task when transport supports it and policy permits; otherwise create one explicitly linked recurrence task |
| duplicate event identity | duplicate | no-op except unfinished delivery recovery |
| event older than the most recent applied event for a key | stale | persist as stale; no state/task mutation |
| same timestamp but distinct full digest for a key | out-of-order conflict | persist and fail delivery for manual review; do not guess ordering |

At all times, a stable key has at most one open incident. A GONE event never closes a card automatically. `daily` supplies the full active snapshot, and `check` supplies transition collections. For `daily`, keys removed from the previous accepted active snapshot become derived GONE transitions only when the event is newer than the prior snapshot; keys added become derived NEW transitions.

## Board and assignee resolution

The intake code resolves board and target through configuration/API calls, not alert text:

1. Resolve the shared board using the normal Hermes resolution chain; the installed shared default is `~/.hermes/kanban.db`.
2. Resolve the target assignee from `kanban.orchestrator_profile`, currently `foreman`; reject an empty/nonexistent profile.
3. Call the Kanban API with structured title/body/idempotency fields. Do not invoke `hermes kanban` through a shell.

No profile configuration was modified by this contract task. If a later implementation changes a live profile/config file, it must first copy a timestamped backup into its task workspace.

## Acceptance tests required of the implementation cards

- structured mapping and stdin JSON produce the same canonical payload and event ID;
- invalid JSON, over-64-KiB input, NUL/control input, malformed timestamp, invalid mode, invalid key, and non-finite diagnostics reject before journal writes;
- Unicode message preservation is byte-for-byte after UTF-8 decoding and re-encoding;
- bounded summary/diagnostic truncation is deterministic and valid UTF-8;
- order and duplicate variation in key collections yields the same canonical collection and identity;
- text variation changes event ID but not the stable incident idempotency key;
- duplicate delivery and simulated crash after journal commit resume without a second card;
- NEW, unchanged, GONE, mixed, heartbeat, recurrence, stale, and out-of-order cases execute the effects listed above.
