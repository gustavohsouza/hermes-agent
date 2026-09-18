"""Invariant tests for Claude-first cron failure escalation."""

from __future__ import annotations

import json
import threading
from unittest.mock import Mock, patch

from cron.failure_escalation import escalate_cron_failure, retry_queued_escalations


def _job(**overrides):
    return {
        "id": "job-1",
        "name": "Morning brief",
        "deliver": "local",
        "failure_deliver": "local",
        **overrides,
    }


def test_no_agent_script_failure_preserves_raw_stderr_and_escalates(tmp_path):
    bridge = Mock(return_value=(True, "accepted"))

    result = escalate_cron_failure(
        _job(no_agent=True), "script", "exit 7\nstderr: AZURE_TOKEN missing",
        hermes_home=tmp_path, bridge_send=bridge,
    )

    assert result.status == "pending_claude_review"
    report = bridge.call_args.args[0]
    assert report.startswith("[ESCALATION]\n")
    assert "exit 7\nstderr: AZURE_TOKEN missing" in report
    assert "failure_deliver=local" in report
    assert "AZURE_TOKEN missing" not in result.public_error


def test_monitor_and_agent_failures_are_escalated(tmp_path):
    bridge = Mock(return_value=(True, "accepted"))

    for failure_class, raw in (
        ("monitor", "monitor_url fetch failed: HTTP 503"),
        ("agent", "ProviderError: model unavailable"),
        ("preflight", "provider credential missing: exact detail"),
        ("scheduler", "RuntimeError: ticker exploded"),
    ):
        result = escalate_cron_failure(
            _job(id=f"job-{failure_class}"), failure_class, raw,
            hermes_home=tmp_path, bridge_send=bridge,
        )
        assert result.status == "pending_claude_review"
        assert raw in bridge.call_args.args[0]


def test_na_like_output_is_an_explicit_failure_not_healthy(tmp_path):
    bridge = Mock(return_value=(True, "accepted"))

    result = escalate_cron_failure(
        _job(), "script", "n/a", hermes_home=tmp_path, bridge_send=bridge,
    )

    assert result.status == "pending_claude_review"
    assert "raw failure evidence: n/a" in bridge.call_args.args[0].lower()
    assert result.public_error


def test_bridge_unavailable_creates_durable_queue_record(tmp_path):
    result = escalate_cron_failure(
        _job(), "agent", "ProviderError: offline", hermes_home=tmp_path,
        bridge_send=Mock(return_value=(False, "connection refused")),
    )

    assert result.status == "queued"
    assert result.queue_path is not None and result.queue_path.exists()
    record = json.loads(result.queue_path.read_text())
    assert record["status"] == "queued"
    assert record["retry_count"] == 0
    assert record["raw_error"] == "ProviderError: offline"
    assert "connection refused" in record["bridge_error"]


def test_job_metadata_is_redacted_before_transport_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda text: text.replace("secret-name", "[REDACTED]")
    )
    bridge = Mock(return_value=(True, "accepted"))

    result = escalate_cron_failure(
        _job(name="secret-name"), "agent", "boom", hermes_home=tmp_path, bridge_send=bridge,
    )

    assert "secret-name" not in bridge.call_args.args[0]
    assert result.queue_path is not None
    assert json.loads(result.queue_path.read_text())["job_name"] == "[REDACTED]"


def test_queue_is_persisted_before_bridge_attempt(tmp_path):
    def bridge(_message):
        queued = list((tmp_path / "cron" / "failure-escalations").glob("*.json"))
        assert len(queued) == 1
        assert json.loads(queued[0].read_text())["status"] == "queued"
        return True, "accepted"

    result = escalate_cron_failure(
        _job(), "agent", "boom", hermes_home=tmp_path, bridge_send=bridge,
    )

    assert result.status == "pending_claude_review"


def test_identical_failure_is_deduped_for_24_hours(tmp_path):
    bridge = Mock(return_value=(True, "accepted"))

    first = escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, bridge_send=bridge,
    )
    second = escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, bridge_send=bridge,
    )

    assert first.escalation_id == second.escalation_id
    assert second.deduped is True
    assert bridge.call_count == 1


def test_queued_failure_is_retried_after_backoff(tmp_path):
    first = escalate_cron_failure(
        _job(), "agent", "offline", hermes_home=tmp_path,
        bridge_send=Mock(return_value=(False, "connection refused")), now=100,
    )
    bridge = Mock(return_value=(True, "accepted"))

    accepted = retry_queued_escalations(
        hermes_home=tmp_path, bridge_send=bridge, now=401,
    )

    assert accepted == 1
    assert first.queue_path is not None
    assert json.loads(first.queue_path.read_text())["status"] == "pending_claude_review"
    assert bridge.call_count == 1


def test_exhausted_queue_record_is_retained_as_dead_letter(tmp_path):
    first = escalate_cron_failure(
        _job(), "agent", "offline", hermes_home=tmp_path,
        bridge_send=Mock(return_value=(False, "connection refused")), now=100,
    )
    assert first.queue_path is not None
    record = json.loads(first.queue_path.read_text())
    record["retry_count"] = 24
    first.queue_path.write_text(json.dumps(record))

    assert retry_queued_escalations(hermes_home=tmp_path, now=500) == 0

    retained = json.loads(first.queue_path.read_text())
    assert retained["status"] == "dead_letter"
    assert retained["raw_error"] == "offline"


def test_failure_delivery_happens_only_after_escalation(monkeypatch):
    from cron import scheduler

    events = []
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda job, kind, error: events.append(("escalate", kind, error)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda *args, **kwargs: events.append(("deliver", kwargs.get("for_failure"))) or None,
    )
    monkeypatch.setattr(
        scheduler, "_upsert_incident_for_failure", lambda *args, **kwargs: (False, "incident")
    )
    monkeypatch.setattr(scheduler, "_mark_incident_alerted", lambda *args: None)
    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda *args, **kwargs: [{}])

    d = scheduler._RunDelivery(job=_job(deliver="origin"), success=False, error="agent exploded")
    fence = scheduler._FireOwnership(d.job, None)
    scheduler._save_compose_deliver(
        d, fence, "", "output", adapters={}, loop=None, verbose=False, execution_token=None,
    )

    assert events[0] == ("escalate", "agent", "agent exploded")
    assert events[1] == ("deliver", True)


def test_failure_deliver_local_cannot_disable_escalation(monkeypatch):
    from cron import scheduler

    escalations = []
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda job, kind, error: escalations.append((kind, error)),
    )
    monkeypatch.setattr(
        scheduler, "_upsert_incident_for_failure", lambda *args, **kwargs: (False, "incident")
    )
    delivery = Mock(return_value=None)
    monkeypatch.setattr(scheduler, "_deliver_result", delivery)

    scheduler._escalate_cron_failure(_job(), "agent", "agent exploded")
    scheduler._deliver_crash_failure(_job(), "agent exploded", adapters={}, loop=None)

    assert escalations == [("agent", "agent exploded")]
    delivery.assert_called_once()


def test_preflight_exception_fails_closed(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_cron_preflight_enabled", lambda cfg: True)
    monkeypatch.setattr(
        scheduler, "_preflight_job_config", lambda job, cfg: (_ for _ in ()).throw(ValueError("bad"))
    )
    monkeypatch.setattr("cron.jobs.mark_preflight_alerted", lambda job_id: False)

    result = scheduler._preflight_or_block(_job(), "job-1", "Morning brief", {})

    assert result is not None
    assert result[0] is False
    assert "preflight validator failed: ValueError: bad" in result[3]


def test_delivery_failure_is_escalated_even_after_original_failure(monkeypatch):
    from cron import scheduler

    escalations = []
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda job, kind, error: escalations.append((kind, error)),
        raising=False,
    )
    monkeypatch.setattr(
        scheduler, "_upsert_incident_for_failure", lambda *args, **kwargs: (False, "incident")
    )
    monkeypatch.setattr(scheduler, "_deliver_result", Mock(return_value="telegram send failed: 500"))
    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda *args, **kwargs: [{}])

    d = scheduler._RunDelivery(job=_job(deliver="origin"), success=False, error="agent exploded")
    fence = scheduler._FireOwnership(d.job, None)
    scheduler._save_compose_deliver(
        d, fence, "", "output", adapters={}, loop=None, verbose=False, execution_token=None,
    )

    assert escalations == [
        ("agent", "agent exploded"),
        ("delivery", "telegram send failed: 500"),
    ]


def test_empty_agent_response_is_escalated_as_failure_before_delivery(monkeypatch):
    from cron import scheduler

    events = []
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda job, kind, error: events.append(("escalate", kind, error)),
    )
    monkeypatch.setattr(
        scheduler, "_upsert_incident_for_failure", lambda *args, **kwargs: (False, "incident")
    )
    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda *args, **kwargs: events.append(("deliver", kwargs["for_failure"])) or None,
    )
    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda *args, **kwargs: [{}])

    d = scheduler._RunDelivery(job=_job(deliver="origin"), success=True, error=None)
    scheduler._save_compose_deliver(
        d, scheduler._FireOwnership(d.job, None), "", "output",
        adapters={}, loop=None, verbose=False, execution_token=None,
    )

    assert d.success is False
    assert events[0][0:2] == ("escalate", "agent")
    assert "empty response" in events[0][2]
    assert len(events) == 1


def test_scheduler_crash_is_escalated(monkeypatch):
    from cron import scheduler_provider

    stop = threading.Event()
    seen = []

    def explode(*args, **kwargs):
        stop.set()
        raise RuntimeError("ticker exploded")

    monkeypatch.setattr("cron.scheduler.tick", explode)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr("cron.jobs.record_ticker_error", lambda *args, **kwargs: None)
    monkeypatch.setattr("cron.jobs.clear_ticker_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        scheduler_provider, "_escalate_scheduler_failure", lambda error: seen.append(str(error))
    )
    monkeypatch.setattr(scheduler_provider.InProcessCronScheduler, "recover_interrupted", lambda s: 0)

    scheduler_provider.InProcessCronScheduler().start(stop, interval=0)

    assert seen == ["ticker exploded"]


def test_scheduler_status_write_failure_is_escalated(monkeypatch):
    from cron import scheduler_provider

    seen = []
    monkeypatch.setattr(
        scheduler_provider, "_escalate_scheduler_failure", lambda error: seen.append(str(error))
    )

    scheduler_provider._guarded_store_write(
        lambda: (_ for _ in ()).throw(OSError("store unavailable")), "heartbeat"
    )

    assert seen == ["store unavailable"]


def test_profile_enumeration_failure_is_escalated(monkeypatch):
    from cron import scheduler_provider

    seen = []
    monkeypatch.setattr(
        scheduler_provider, "_escalate_scheduler_failure", lambda error: seen.append(str(error))
    )

    homes = scheduler_provider._existing_profile_homes(
        lambda: (_ for _ in ()).throw(RuntimeError("enumerator failed"))
    )

    assert homes == []
    assert seen == ["enumerator failed"]
