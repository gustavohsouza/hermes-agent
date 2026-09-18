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
import re
import sys
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
_RATE_WINDOW_SECONDS = 10 * 60
_RATE_MAX = 5
_EVIDENCE_MAX = 8192
_MOCK_RE = re.compile(r"<(?:(?:NonCallable)?MagicMock|Mock)\b[^>]*>")
# Snapshot at import so a test cannot bypass the fence by deleting the env var.
_TEST_PROCESS = bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


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
    return _normalize(value, missing="")


def _normalize(value: object, *, missing: str = "<missing failure evidence>") -> str:
    """Return bounded, redacted text without test-double internals."""
    if value is None:
        text = missing
    else:
        try:
            text = str(value)
        except Exception:
            text = "<unprintable value>"
    text = _MOCK_RE.sub("[test double]", text)
    text = _redact(text)
    marker = "...[truncated]"
    if len(text) > _EVIDENCE_MAX:
        text = text[: _EVIDENCE_MAX - len(marker)] + marker
    return text


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


def _append_capture(queue_dir: Path, payload: dict) -> None:
    queue_dir.mkdir(parents=True, exist_ok=True)
    with (queue_dir / "test-capture.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _rate_decision(queue_dir: Path, timestamp: float) -> tuple[str, int]:
    """Persist the ten-minute rate window and return send/digest/suppress."""
    import fcntl

    state_path = queue_dir / "rate-state.json"
    queue_dir.mkdir(parents=True, exist_ok=True)
    with (queue_dir / ".rate.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"window_started_at": timestamp, "sent_count": 0, "suppressed_count": 0,
                     "digest_emitted": False}
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Corrupt quota state fails closed for the current window.
            state = {"window_started_at": timestamp, "sent_count": _RATE_MAX,
                     "suppressed_count": 0, "digest_emitted": False}
        if timestamp - float(state.get("window_started_at", timestamp)) >= _RATE_WINDOW_SECONDS:
            state = {"window_started_at": timestamp, "sent_count": 0, "suppressed_count": 0,
                     "digest_emitted": False}
        if int(state.get("sent_count", 0)) < _RATE_MAX:
            state["sent_count"] = int(state.get("sent_count", 0)) + 1
            decision = "send"
        else:
            state["suppressed_count"] = int(state.get("suppressed_count", 0)) + 1
            decision = "suppress"
            if not state.get("digest_emitted"):
                state["digest_emitted"] = True
                decision = "digest"
        _atomic_json(state_path, state)
        return decision, int(state.get("suppressed_count", 0))


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
    live: bool = False,
) -> EscalationResult:
    """Persist and send one Claude handoff, deduping the same blocker for 24h.

    The exact failure text is retained after mandatory secret redaction.  This
    function never raises: escalation-system failures must not recurse.
    """
    timestamp = time.time() if now is None else now
    evidence = _normalize(raw_error)
    job_id = _normalize(job.get("id") or "unknown")
    failure_class = _normalize(failure_class, missing="unknown")
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

    # Pytest is a hard delivery fence. Explicit live=True is also required outside
    # tests, so accidental callers can only write the local capture ledger.
    test_mode = _TEST_PROCESS or not live
    try:
        decision, suppressed_count = _rate_decision(queue_dir, timestamp)
        if decision == "suppress":
            return EscalationResult(
                escalation_id, "rate_limited", "Failure is pending Claude review.", False, record_path
            )
        kind = "overflow_digest" if decision == "digest" else "escalation"
        message = (
            "[ESCALATION DIGEST]\nAdditional cron failures are being aggregated locally "
            "by the 5-per-10-minute safety cap.\n"
            if decision == "digest" else report
        )
        if test_mode:
            _append_capture(queue_dir, {
                "kind": kind, "escalation_id": escalation_id, "message": message,
                "suppressed_count": suppressed_count, "timestamp": timestamp,
            })
            record["status"] = "captured_test"
            _atomic_json(record_path, record)
            return EscalationResult(
                escalation_id, "captured_test", "Failure is pending Claude review.", False, record_path
            )
        report = message
    except Exception as exc:
        logger.error("Cron escalation local safety gate failed: %s", _normalize(exc))
        return EscalationResult(
            escalation_id, "queue_failed", "Failure escalation could not be persisted.",
            False, record_path,
        )

    sender = bridge_send or _bridge_send
    accepted = False
    bridge_detail = "bridge send not attempted"
    try:
        accepted, bridge_detail = sender(report)
    except Exception as exc:
        bridge_detail = f"{type(exc).__name__}: {_normalize(exc)}"
    bridge_detail = _normalize(bridge_detail)
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
    live: bool = False,
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
            decision, suppressed_count = _rate_decision(queue_dir, timestamp)
            if decision == "suppress":
                continue
            if decision == "digest":
                report = (
                    "[ESCALATION DIGEST]\nAdditional cron failures are being aggregated locally "
                    "by the 5-per-10-minute safety cap.\n"
                )
            if _TEST_PROCESS or not live:
                _append_capture(queue_dir, {
                    "kind": "overflow_digest" if decision == "digest" else "retry",
                    "escalation_id": record.get("escalation_id") or path.stem,
                    "message": report, "suppressed_count": suppressed_count, "timestamp": timestamp,
                })
                continue
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
