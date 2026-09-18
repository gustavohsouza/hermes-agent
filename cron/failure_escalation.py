"""Durable Claude-first escalation for cron failures.

Every cron failure is handed to Claude before an operator notice is attempted.  The
handoff is durable even when the local Claude bridge is unavailable.  Failures in
this mechanism are deliberately non-recursive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEDUPE_SECONDS = 24 * 60 * 60
_RETRY_SECONDS = 5 * 60
_MAX_RETRIES = 24
_BRIDGE_URL = "http://127.0.0.1:8787"


@dataclass(frozen=True)
class EscalationResult:
    escalation_id: str
    status: str
    public_error: str
    deduped: bool = False
    queue_path: Optional[Path] = None


def _redact(text: str) -> str:
    """Redact before persistence, logging, or transport; fail closed."""
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception:
        return "[REDACTED - redaction failed]"


def _redact_field(value: object) -> str:
    return _redact(str(value or ""))


def _bridge_send(message: str) -> tuple[bool, str]:
    payload = json.dumps({"text": message, "from": "hermes"}).encode("utf-8")
    request = urllib.request.Request(
        _BRIDGE_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - loopback URL
            body = response.read(16_384).decode("utf-8", errors="replace")
            if 200 <= response.status < 300:
                return True, body or f"HTTP {response.status}"
            return False, f"HTTP {response.status}: {body}"
    except (OSError, urllib.error.URLError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _report(job: dict, failure_class: str, raw_error: str, escalation_id: str) -> str:
    return (
        "[ESCALATION]\n"
        "Claude-first cron failure handoff. Review the evidence, attempt joint repair, and return "
        "a concrete resolution or unresolved diagnosis to Hermes. Do not contact the user directly.\n\n"
        f"escalation_id={escalation_id}\n"
        f"job_id={_redact_field(job.get('id'))}\n"
        f"job_name={_redact_field(job.get('name'))}\n"
        f"failure_class={failure_class}\n"
        f"deliver={_redact_field(job.get('deliver', 'local'))}\n"
        f"failure_deliver={_redact_field(job.get('failure_deliver', '(unset)'))}\n"
        f"raw failure evidence: {raw_error}\n"
    )


def escalate_cron_failure(
    job: dict,
    failure_class: str,
    raw_error: object,
    *,
    hermes_home: Optional[Path] = None,
    bridge_send: Optional[Callable[[str], tuple[bool, str]]] = None,
    now: Optional[float] = None,
) -> EscalationResult:
    """Persist and send one Claude handoff, deduping the same blocker for 24h.

    The exact failure text is retained after mandatory secret redaction.  This
    function never raises: escalation-system failures must not recurse.
    """
    timestamp = time.time() if now is None else now
    evidence = _redact(str(raw_error) if raw_error is not None else "<missing failure evidence>")
    job_id = str(job.get("id") or "unknown")
    fingerprint = hashlib.sha256(
        f"{job_id}\0{failure_class}\0{evidence}".encode("utf-8", errors="replace")
    ).hexdigest()
    escalation_id = f"cron-{fingerprint[:16]}"
    home = Path(hermes_home) if hermes_home is not None else _default_hermes_home()
    queue_dir = home / "cron" / "failure-escalations"
    record_path = queue_dir / f"{escalation_id}.json"

    try:
        if record_path.exists():
            prior = json.loads(record_path.read_text(encoding="utf-8"))
            if timestamp - float(prior.get("last_seen_at", 0)) < _DEDUPE_SECONDS:
                prior["last_seen_at"] = timestamp
                prior["occurrences"] = int(prior.get("occurrences", 1)) + 1
                _atomic_json(record_path, prior)
                return EscalationResult(
                    escalation_id, str(prior.get("status") or "queued"),
                    "Failure is pending Claude review.", True, record_path,
                )
    except Exception as exc:
        logger.error("Cron escalation dedupe read failed: %s", _redact(str(exc)))

    report = _report(job, failure_class, evidence, escalation_id)
    record = {
        "schema_version": 1,
        "escalation_id": escalation_id,
        "status": "queued",
        "job_id": job_id,
        "job_name": _redact_field(job.get("name")),
        "failure_class": failure_class,
        "raw_error": evidence,
        "created_at": timestamp,
        "last_seen_at": timestamp,
        "occurrences": 1,
        "retry_count": 0,
        "bridge_error": None,
        "bridge_response": None,
    }
    # Durability precedes transport: even a process death during POST leaves a
    # retryable handoff, and no user-facing failure path can overtake it.
    try:
        _atomic_json(record_path, record)
    except Exception as exc:
        logger.error("Cron escalation durable queue write failed: %s", _redact(str(exc)))
        return EscalationResult(
            escalation_id, "queue_failed", "Failure escalation could not be persisted."
        )

    sender = bridge_send or _bridge_send
    accepted = False
    bridge_detail = "bridge send not attempted"
    try:
        accepted, bridge_detail = sender(report)
    except Exception as exc:
        bridge_detail = f"{type(exc).__name__}: {exc}"
    bridge_detail = _redact(str(bridge_detail))
    status = "pending_claude_review" if accepted else "queued"
    record.update({
        "status": status,
        "bridge_error": None if accepted else bridge_detail,
        "bridge_response": bridge_detail if accepted else None,
    })
    try:
        _atomic_json(record_path, record)
    except Exception as exc:
        # The pre-send queued record is still durable. Do not recurse.
        logger.error("Cron escalation acknowledgement write failed: %s", _redact(str(exc)))
        status = "queued"
    return EscalationResult(
        escalation_id, status, "Failure is pending Claude review.", False, record_path
    )


def retry_queued_escalations(
    *, hermes_home: Optional[Path] = None,
    bridge_send: Optional[Callable[[str], tuple[bool, str]]] = None,
    now: Optional[float] = None,
) -> int:
    """Retry due queued handoffs; return the number accepted by the bridge.

    Queue maintenance is intentionally self-contained and non-recursive. A bad
    record cannot prevent other queued failures from being retried.
    """
    timestamp = time.time() if now is None else now
    home = Path(hermes_home) if hermes_home is not None else _default_hermes_home()
    queue_dir = home / "cron" / "failure-escalations"
    sender = bridge_send or _bridge_send
    accepted_count = 0
    try:
        paths = list(queue_dir.glob("*.json"))
    except OSError as exc:
        logger.error("Cron escalation queue scan failed: %s", _redact(str(exc)))
        return 0

    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("status") != "queued":
                continue
            retry_count = int(record.get("retry_count", 0))
            last_attempt = float(record.get("last_attempt_at", record.get("created_at", 0)))
            if retry_count >= _MAX_RETRIES:
                if record.get("status") != "dead_letter":
                    record["status"] = "dead_letter"
                    record["dead_lettered_at"] = timestamp
                    _atomic_json(path, record)
                continue
            if timestamp - last_attempt < _RETRY_SECONDS:
                continue
            report = _report(
                {"id": record.get("job_id"), "name": record.get("job_name")},
                str(record.get("failure_class") or "unknown"),
                str(record.get("raw_error") or "<missing failure evidence>"),
                str(record.get("escalation_id") or path.stem),
            )
            ok, detail = sender(report)
            record["retry_count"] = retry_count + 1
            record["last_attempt_at"] = timestamp
            if ok:
                record["status"] = "pending_claude_review"
                record["bridge_response"] = _redact(str(detail))
                record["bridge_error"] = None
                accepted_count += 1
            else:
                record["bridge_error"] = _redact(str(detail))
            _atomic_json(path, record)
        except Exception as exc:
            logger.error("Cron escalation queue retry failed for %s: %s", path.name, _redact(str(exc)))
    return accepted_count


def _default_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
