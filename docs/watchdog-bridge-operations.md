# Watchdog bridge operator runbook

This runbook applies to the bridge at this repository revision. It distinguishes installation from producer activation: `watchdog_install.py` installs the intake executable, but deliberately does not edit `~/hermes/bin/watchdog.sh` or `~/.hermes/config.yaml`.

## Installed layout

Run the installer from the reviewed repository checkout:

    python3 watchdog_install.py --profile "$HOME/.hermes" --source "$PWD"

It installs these exact paths:

- `~/.hermes/lib/watchdog-bridge/watchdog_boundary.py`
- `~/.hermes/lib/watchdog-bridge/watchdog_kanban.py`
- `~/.hermes/lib/watchdog-bridge/watchdog_runtime.py`
- `~/.hermes/bin/watchdog-kanban-intake`

The launcher is mode `0700`. Replaced files are copied before replacement to `~/.hermes/backups/watchdog-bridge/<UTC timestamp>/`, preserving their paths relative to `~/.hermes`. A first installation has no backup for a path that did not previously exist.

The installed runtime writes its owner-only journal to:

    ~/.hermes/state/watchdog-intake.sqlite3

The state directory is forced to `0700`; the database and extant WAL/SHM sidecars are forced to `0600`.

## Activation boundary

Installation alone does not activate the bridge. The current producer is `~/hermes/bin/watchdog.sh`; an operator must explicitly connect the producer's already-computed snapshot to the installed launcher. Do not pass human alert text as shell syntax, construct JSON with string concatenation, or execute event data through a command interpreter.

The safe downstream call is a process API with a fixed argv and encoded JSON on stdin:

    import json
    import subprocess
    from pathlib import Path

    launcher = Path.home() / ".hermes/bin/watchdog-kanban-intake"
    encoded = json.dumps(structured_watchdog_event, ensure_ascii=False).encode("utf-8")
    subprocess.run([str(launcher)], input=encoded, check=True)

`structured_watchdog_event` must already satisfy the schema in `watchdog-kanban-intake-contract-v1.md`. The runtime accepts exactly `--json-stdin`; it has no argv-payload mode. In a Python producer, the equivalent in-process integration is:

    from pathlib import Path
    from watchdog_runtime import submit_watchdog_event

    submit_watchdog_event(
        structured_watchdog_event,
        Path.home() / ".hermes/state/watchdog-intake.sqlite3",
    )

The runtime calls the Hermes Kanban Python API directly. Board resolution follows Hermes's normal board resolver. The adapter intentionally uses the literal assignee `foreman`; it does not read `kanban.orchestrator_profile`. The installed runtime uses linked recurrence cards rather than reopening an existing card, while preserving `linked_incident_task_id` metadata.

## Activation verification

Verify the installed launcher and modules without creating a board card:

    python3 -c 'import pathlib; p=pathlib.Path.home()/".hermes"; paths=[p/"bin/watchdog-kanban-intake",p/"lib/watchdog-bridge/watchdog_boundary.py",p/"lib/watchdog-bridge/watchdog_kanban.py",p/"lib/watchdog-bridge/watchdog_runtime.py"]; print([(str(q),oct(q.stat().st_mode&0o777)) for q in paths])'

The command must list all four paths. The launcher mode must be `0o700`.

Then feed a heartbeat through the exact production entrypoint with a process API, not a shell pipeline:

    python3 -c 'import datetime,json,pathlib,subprocess; event={"version":1,"mode":"check","observed_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"message":"watchdog bridge activation heartbeat","active_keys":[],"new_keys":[],"gone_keys":[],"diagnostics":{"source":"activation-smoke"}}; subprocess.run([str(pathlib.Path.home()/".hermes/bin/watchdog-kanban-intake")],input=json.dumps(event).encode("utf-8"),check=True)'

A heartbeat persists one event and performs no Kanban operation. Verify the journal structurally:

    python3 -c 'import pathlib,sqlite3; p=pathlib.Path.home()/".hermes/state/watchdog-intake.sqlite3"; c=sqlite3.connect(p); print(c.execute("select state,count(*) from watchdog_events group by state order by state").fetchall()); print(c.execute("select delivery_state,outcome_code,count(*) from watchdog_incident_deliveries group by delivery_state,outcome_code order by delivery_state,outcome_code").fetchall())'

The second query must include a `submitted`, `heartbeat_only` row. Activation of the real producer is complete only after its application-owned hook uses one of the safe calls above and a representative event is visible in the journal. A NEW event also creates or reuses a card through the shared board's idempotency key; do not use a synthetic NEW event on the live board merely as a smoke test.

## Retention and safe backup

Retain the journal, WAL/SHM files, and board cards through rollback. Before cleanup, verify there is no pending or failed delivery:

    python3 -c 'import pathlib,sqlite3; p=pathlib.Path.home()/".hermes/state/watchdog-intake.sqlite3"; c=sqlite3.connect(p); rows=c.execute("select event_id,stable_key,transition,delivery_state,outcome_code from watchdog_incident_deliveries where delivery_state in (\"pending\",\"failed\") order by event_id,stable_key,transition").fetchall(); print(rows); raise SystemExit(bool(rows))'

Create a WAL-aware, standalone SQLite backup. This uses SQLite's backup API rather than copying the live database and sidecars:

    python3 -c 'import datetime,pathlib,sqlite3; src=pathlib.Path.home()/".hermes/state/watchdog-intake.sqlite3"; stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"); dst=pathlib.Path.home()/f".hermes/backups/watchdog-bridge/journal-{stamp}.sqlite3"; dst.parent.mkdir(parents=True,exist_ok=True); source=sqlite3.connect(src); target=sqlite3.connect(dst); source.backup(target); target.close(); source.close(); dst.chmod(0o600); print(dst)'

Verify the printed backup before deleting any state:

    python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("pragma integrity_check").fetchone()[0])' /absolute/path/printed/by/the/backup/command

Cleanup is manual and is permitted only after the pending query returns `[]`, the backup reports `ok`, and the operator has accepted that journal history will no longer be available for retry or audit.

After those three conditions are met and the producer hook is disabled, remove only
the journal and its SQLite sidecars with fixed paths:

    python3 -c 'import pathlib; p=pathlib.Path.home()/".hermes/state/watchdog-intake.sqlite3"; [(q.unlink() if q.exists() else None) for q in (p,p.with_name(p.name+"-wal"),p.with_name(p.name+"-shm"))]'

Do not remove `~/.hermes/kanban.db` or Watchdog-created cards. The bridge has no
automatic retention window or cleanup job.

## Rollback

1. Disable the application-owned producer hook. Do not delete cards or journal state.
2. Identify the exact installation timestamp directory under `~/.hermes/backups/watchdog-bridge/`.
3. Restore every file present in that timestamp directory to the same relative path beneath `~/.hermes`.
4. Remove an installed bridge path only if it did not exist before installation and therefore has no corresponding backup.
5. Retain `~/.hermes/state/watchdog-intake.sqlite3` and its WAL/SHM sidecars.

Back up these operator-owned paths before rollback or cleanup:

- deployed files: `~/.hermes/lib/watchdog-bridge/watchdog_boundary.py`,
  `watchdog_kanban.py`, and `watchdog_runtime.py`;
- launcher: `~/.hermes/bin/watchdog-kanban-intake`;
- journal: `~/.hermes/state/watchdog-intake.sqlite3` (use the SQLite backup API
  above rather than a live file copy);
- producer hook, if the operator changed it: `~/hermes/bin/watchdog.sh`;
- profile configuration, only if the operator changed it: `~/.hermes/config.yaml`.

The installer backs up only the deployed files and launcher. Back up the producer
hook or profile configuration separately before changing either one.

For example, restore one timestamp with a fixed Python file operation after replacing the timestamp literal with the directory selected in step 2:

    python3 -c 'import pathlib,shutil; profile=pathlib.Path.home()/".hermes"; backup=profile/"backups/watchdog-bridge/20260910T020000Z"; [(p.parent.mkdir(parents=True,exist_ok=True),shutil.copy2(src,p)) for src in backup.rglob("*") if src.is_file() for p in [profile/src.relative_to(backup)]]'

If the launcher did not exist before installation, disable the hook first and remove `~/.hermes/bin/watchdog-kanban-intake` after restoring all backed-up files. Likewise remove `~/.hermes/lib/watchdog-bridge/` only when none of its deployed files existed before installation. Re-enable the bridge later against the retained journal to retry pending/failed deliveries safely.
