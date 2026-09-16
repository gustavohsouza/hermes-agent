"""Durable, exactly-once Watchdog closure and tier-3 reporting."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from watchdog_boundary import Journal, KEY_RE, canonicalize_event

WHATSAPP_HOME_DM = "whatsapp:117484669640820@lid"
CLASSIFICATIONS = frozenset({
    "actionable", "transient/self-healing", "false positive", "informational", "tier-3",
})


class ClosureError(ValueError):
    pass


class DeliveryPort(Protocol):
    def send(self, *, target: str, text: str, idempotency_key: str) -> None: ...


@dataclass(frozen=True)
class ClosureOutcome:
    classification: str
    summary: str
    treatment_warranted: bool
    actions: str
    verification: str
    residual_risk: str

    def validate(self, *, for_completion: bool = False) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise ClosureError("invalid_classification")
        if for_completion and self.classification == "tier-3":
            raise ClosureError("tier3_requires_decision")
        for name in ("summary", "actions", "verification", "residual_risk"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ClosureError(f"missing_{name}")
            if len(value.encode("utf-8")) > 4096:
                raise ClosureError(f"oversized_{name}")
        if not isinstance(self.treatment_warranted, bool):
            raise ClosureError("invalid_treatment_warranted")


def _report_key(kind: str, stable_key: str, task_id: str, occurrence_id: str) -> str:
    if not isinstance(stable_key, str) or not KEY_RE.fullmatch(stable_key):
        raise ClosureError("invalid_stable_key")
    digest = hashlib.sha256(
        json.dumps([kind, stable_key, task_id, occurrence_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"wd-{kind}-v1-{digest[:32]}"


def _closure_text(stable_key: str, outcome: ClosureOutcome) -> str:
    return (
        "Watchdog incident closed\n"
        f"Alert key: {stable_key}\n"
        f"Summary: {outcome.summary}\n"
        f"Classification: {outcome.classification}\n"
        f"Treatment warranted: {'yes' if outcome.treatment_warranted else 'no'}\n"
        f"Actions: {outcome.actions}\n"
        f"Verification: {outcome.verification}\n"
        f"Residual risk: {outcome.residual_risk}"
    )


class ClosureReporter:
    def __init__(self, journal: Journal, delivery: DeliveryPort):
        self.journal = journal
        self.delivery = delivery

    def complete(self, *, stable_key: str, task_id: str, occurrence_id: str, outcome: ClosureOutcome,
                 verification_source: str = "independent") -> bool:
        outcome.validate(for_completion=True)
        if verification_source != "independent":
            raise ClosureError("independent_verification_required")
        report_key = _report_key("closure", stable_key, task_id, occurrence_id)
        inserted = self.journal.queue_report(
            report_key, stable_key, task_id, "closure",
            {"target": WHATSAPP_HOME_DM, "text": _closure_text(stable_key, outcome),
             "outcome": asdict(outcome)},
        )
        if inserted:
            self.deliver_pending()
        return self.journal.report_state(report_key) == "sent" and inserted

    def escalate_tier3(self, *, stable_key: str, task_id: str, occurrence_id: str, summary: str,
                       verification: str, residual_risk: str) -> bool:
        outcome = ClosureOutcome("tier-3", summary, False, "Await Gustavo's decision.",
                                 verification, residual_risk)
        outcome.validate()
        report_key = _report_key("tier3", stable_key, task_id, occurrence_id)
        text = (
            "Watchdog tier-3 decision required\n"
            f"Alert key: {stable_key}\nSummary: {summary}\n"
            f"Verification: {verification}\nResidual risk: {residual_risk}"
        )
        inserted = self.journal.queue_report(
            report_key, stable_key, task_id, "tier3",
            {"target": WHATSAPP_HOME_DM, "text": text, "outcome": asdict(outcome)},
        )
        if inserted:
            self.deliver_pending()
        return self.journal.report_state(report_key) == "sent" and inserted

    def deliver_pending(self) -> int:
        failures = 0
        while True:
            claimed = self.journal.claim_report()
            if claimed is None:
                break
            report_key, _kind, payload, claim_token = claimed
            try:
                self.delivery.send(target=payload["target"], text=payload["text"],
                                   idempotency_key=report_key)
                self.journal.mark_report_sent(report_key, claim_token)
            except Exception:
                self.journal.mark_report_failed(report_key, claim_token, "closure_delivery_failed")
                failures += 1
                break
        return failures


def deterministic_heartbeat_outcome(event: Mapping[str, Any]) -> ClosureOutcome:
    try:
        canonical = canonicalize_event(event).payload
    except ValueError as exc:
        raise ClosureError("heartbeat_not_healthy") from exc
    if canonical["mode"] != "daily" or canonical["active_keys"]:
        raise ClosureError("heartbeat_not_healthy")
    return ClosureOutcome(
        classification="informational",
        summary="Daily Watchdog heartbeat reported no active alerts.",
        treatment_warranted=False,
        actions="No action; recorded as healthy telemetry.",
        verification="Validated daily mode with an explicitly empty active-key snapshot.",
        residual_risk="Point-in-time validation only; later checks may surface new alerts.",
    )