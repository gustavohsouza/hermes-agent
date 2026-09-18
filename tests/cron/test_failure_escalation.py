"""Invariant tests for Claude-first cron failure escalation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


def _capture(home):
    path = home / "cron" / "failure-escalations" / "test-capture.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _run_escalation_process(home, *, mode="one", start=100):
    code = r'''
import json
import sys
from pathlib import Path
from cron.failure_escalation import escalate_cron_failure
home = Path(sys.argv[1])
mode = sys.argv[2]
start = float(sys.argv[3])
results = []
count = 5 if mode == "five" else 2 if mode == "two" else 1
for number in range(count):
    suffix = number + (5 if mode == "two" else 0) if mode != "one" else 0
    result = escalate_cron_failure(
        {"id": f"job-{suffix}", "name": "Morning brief"},
        "agent", f"failure-{suffix}", hermes_home=home, now=start + number,
    )
    results.append({"id": result.escalation_id, "deduped": result.deduped})
print(json.dumps(results))
'''
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return subprocess.run(
        [sys.executable, "-c", code, str(home), mode, str(start)],
        check=True, capture_output=True, text=True, env=env,
    )


def test_dedupe_survives_process_restart(tmp_path):
    first = json.loads(_run_escalation_process(tmp_path, start=100).stdout)
    second = json.loads(_run_escalation_process(tmp_path, start=200).stdout)

    assert first[0]["id"] == second[0]["id"]
    assert second[0]["deduped"] is True
    assert len(_capture(tmp_path)) == 1


def test_rate_cap_survives_process_restart(tmp_path):
    _run_escalation_process(tmp_path, mode="five", start=100)
    _run_escalation_process(tmp_path, mode="two", start=200)

    capture = _capture(tmp_path)
    assert len([item for item in capture if item["kind"] == "escalation"]) == 5
    assert len([item for item in capture if item["kind"] == "overflow_digest"]) == 1
    state = json.loads(
        (tmp_path / "cron" / "failure-escalations" / "rate-state.json").read_text()
    )
    assert state["suppressed_count"] == 2


def test_concurrent_processes_capture_identical_failure_once(tmp_path):
    start_file = tmp_path / "start"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", r'''
import sys
import time
from pathlib import Path
from cron.failure_escalation import escalate_cron_failure
start_file = Path(sys.argv[2])
while not start_file.exists():
    time.sleep(0.001)
escalate_cron_failure(
    {"id": "job-1", "name": "Morning brief"}, "agent", "same concurrent error",
    hermes_home=Path(sys.argv[1]), now=100,
)
''', str(tmp_path), str(start_file)],
            env={key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"},
        )
        for _ in range(8)
    ]
    start_file.touch()
    assert [process.wait(timeout=10) for process in processes] == [0] * 8

    assert len(_capture(tmp_path)) == 1
    queue_paths = list((tmp_path / "cron" / "failure-escalations").glob("cron-*.json"))
    assert len(queue_paths) == 1
    queue_path = queue_paths[0]
    record = json.loads(queue_path.read_text())
    assert record["occurrences"] == 8


def test_pytest_gate_captures_and_never_calls_live_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "gate::test")
    live_sender = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(), "agent", "synthetic failure", hermes_home=tmp_path,
        bridge_send=live_sender, live=True,
    )

    live_sender.assert_not_called()
    assert result.status == "captured_test"
    capture = _capture(tmp_path)
    assert capture[0]["escalation_id"] == result.escalation_id
    assert capture[0]["message"].endswith("raw failure evidence: synthetic failure\n")


def test_transport_requires_explicit_live_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    live_sender = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(), "agent", "not explicitly live", hermes_home=tmp_path,
        bridge_send=live_sender,
    )

    live_sender.assert_not_called()
    assert result.status == "captured_test"
    assert _capture(tmp_path)[0]["message"].endswith(
        "raw failure evidence: not explicitly live\n"
    )


def test_pytest_gate_survives_current_test_env_removal(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    live_sender = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(), "agent", "still fenced", hermes_home=tmp_path,
        bridge_send=live_sender, live=True,
    )

    live_sender.assert_not_called()
    assert result.status == "captured_test"


def test_evidence_is_bounded_string_and_never_leaks_mock_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "normalization::test")
    evidence = Mock()
    evidence.__str__ = Mock(return_value="<MagicMock name='token-secret' id='12345'>" + "x" * 20_000)

    result = escalate_cron_failure(
        _job(), "agent", evidence, hermes_home=tmp_path, live=True,
    )

    record = json.loads(result.queue_path.read_text())
    assert len(record["raw_error"]) <= 8192
    assert "MagicMock" not in record["raw_error"]
    assert "id='12345'" not in record["raw_error"]
    assert record["raw_error"].endswith("[truncated]")


def test_dedupe_survives_a_fresh_module_import(tmp_path, monkeypatch):
    import importlib
    import cron.failure_escalation as failure_escalation

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "dedupe::test")
    first = failure_escalation.escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, now=100, live=True,
    )
    reloaded = importlib.reload(failure_escalation)
    second = reloaded.escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, now=200, live=True,
    )

    assert first.escalation_id == second.escalation_id
    assert second.deduped is True
    assert len(_capture(tmp_path)) == 1


def test_rate_cap_emits_one_overflow_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "rate::test")

    for number in range(8):
        escalate_cron_failure(
            _job(id=f"job-{number}"), "agent", f"failure-{number}",
            hermes_home=tmp_path, now=100 + number, live=True,
        )

    capture = _capture(tmp_path)
    individual = [item for item in capture if item["kind"] == "escalation"]
    digests = [item for item in capture if item["kind"] == "overflow_digest"]
    assert len(individual) == 5
    assert len(digests) == 1
    assert digests[0]["suppressed_count"] == 1
    state = json.loads(
        (tmp_path / "cron" / "failure-escalations" / "rate-state.json").read_text()
    )
    assert state["suppressed_count"] == 3


def test_rate_cap_state_survives_module_restart(tmp_path, monkeypatch):
    import importlib
    import cron.failure_escalation as failure_escalation

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "rate-restart::test")
    for number in range(5):
        failure_escalation.escalate_cron_failure(
            _job(id=f"before-{number}"), "agent", f"failure-{number}",
            hermes_home=tmp_path, now=100 + number, live=True,
        )
    reloaded = importlib.reload(failure_escalation)
    reloaded.escalate_cron_failure(
        _job(id="after-1"), "agent", "overflow-1", hermes_home=tmp_path, now=200, live=True,
    )
    reloaded.escalate_cron_failure(
        _job(id="after-2"), "agent", "overflow-2", hermes_home=tmp_path, now=201, live=True,
    )

    capture = _capture(tmp_path)
    assert len([item for item in capture if item["kind"] == "escalation"]) == 5
    assert len([item for item in capture if item["kind"] == "overflow_digest"]) == 1
    state = json.loads(
        (tmp_path / "cron" / "failure-escalations" / "rate-state.json").read_text()
    )
    assert state["suppressed_count"] == 2


def test_corrupt_rate_state_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "corrupt-rate::test")
    queue = tmp_path / "cron" / "failure-escalations"
    queue.mkdir(parents=True)
    (queue / "rate-state.json").write_text("not json")

    result = escalate_cron_failure(
        _job(), "agent", "failure", hermes_home=tmp_path, now=100, live=True,
    )

    assert result.status == "captured_test"
    assert _capture(tmp_path)[0]["kind"] == "overflow_digest"


def test_escalation_failure_is_local_and_non_recursive(tmp_path):
    import subprocess
    import sys

    code = """
import json
import sys
from pathlib import Path
from cron.failure_escalation import escalate_cron_failure
calls = []
def failing_sender(message):
    calls.append(message)
    raise RuntimeError('bridge exploded')
result = escalate_cron_failure(
    {'id': 'job-1', 'name': 'Morning brief'}, 'agent', 'original failure',
    hermes_home=Path(sys.argv[1]), bridge_send=failing_sender, live=True,
)
print(json.dumps({'status': result.status, 'calls': len(calls), 'path': str(result.queue_path)}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], text=True, capture_output=True, check=True,
        env={key: value for key, value in __import__("os").environ.items()
             if key != "PYTEST_CURRENT_TEST"},
    )

    outcome = json.loads(completed.stdout)
    assert outcome["status"] == "queued"
    assert outcome["calls"] == 1
    record = json.loads(__import__("pathlib").Path(outcome["path"]).read_text())
    assert "bridge exploded" in record["bridge_error"]
    failures = tmp_path / "cron" / "failure-escalations" / "local-failures.jsonl"
    entries = [json.loads(line) for line in failures.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["operation"] == "bridge_send"
    assert "bridge exploded" in entries[0]["detail"]


def test_failed_durable_escalation_suppresses_normal_failure_delivery(monkeypatch):
    from cron import scheduler
    from cron.failure_escalation import EscalationResult

    deliveries = Mock()
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda *args: EscalationResult("id", "queue_failed", "failed"),
    )
    monkeypatch.setattr(scheduler, "_deliver_result", deliveries)

    d = scheduler._RunDelivery(job=_job(deliver="origin"), success=False, error="agent exploded")
    scheduler._save_compose_deliver(
        d, scheduler._FireOwnership(d.job, None), "", "output",
        adapters={}, loop=None, verbose=False, execution_token=None,
    )

    deliveries.assert_not_called()


def test_failed_durable_escalation_suppresses_crash_failure_delivery(monkeypatch):
    from cron import scheduler
    from cron.failure_escalation import EscalationResult

    crash_delivery = Mock()
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *args: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda *args: {})
    monkeypatch.setattr(scheduler, "run_job", Mock(side_effect=RuntimeError("agent exploded")))
    monkeypatch.setattr(
        scheduler, "_escalate_cron_failure",
        lambda *args: EscalationResult("id", "queue_failed", "failed"),
    )
    monkeypatch.setattr(scheduler, "_deliver_crash_failure", crash_delivery)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *args, **kwargs: None)
    monkeypatch.setattr("agent.secret_scope.set_secret_scope", lambda *args: None)
    monkeypatch.setattr("agent.secret_scope.build_profile_secret_scope", lambda *args: None)
    monkeypatch.setattr("agent.secret_scope.reset_secret_scope", lambda *args: None)
    monkeypatch.setattr("tools.terminal_scope.install_profile_terminal_scope", lambda *args: None)
    monkeypatch.setattr("tools.terminal_scope.reset_terminal_scope", lambda *args: None)

    assert scheduler._run_one_job_body(_job(), execution_token=None) is False
    crash_delivery.assert_not_called()


def test_execution_creation_failure_is_escalated_before_return(monkeypatch):
    from cron import scheduler

    escalations = Mock()
    monkeypatch.setattr(scheduler, "_escalate_cron_failure", escalations)
    monkeypatch.setattr(scheduler, "create_execution", Mock(side_effect=OSError("ledger offline")))
    monkeypatch.setattr(scheduler, "try_register_running_job", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "release_running_job", Mock())

    assert scheduler._submit_with_guard(_job(), Mock(), Mock()) is None
    escalations.assert_called_once()
    assert escalations.call_args.args[1] == "scheduler"
    assert "execution creation failed: OSError: ledger offline" in escalations.call_args.args[2]


def test_executor_submit_failure_is_escalated_before_return(monkeypatch):
    from cron import scheduler

    escalations = Mock()
    pool = Mock()
    pool.submit.side_effect = RuntimeError("executor unavailable")
    monkeypatch.setattr(scheduler, "_escalate_cron_failure", escalations)
    monkeypatch.setattr(scheduler, "create_execution", lambda *args, **kwargs: {"id": "exec-1"})
    monkeypatch.setattr(scheduler, "finish_execution", Mock())
    monkeypatch.setattr(scheduler, "try_register_running_job", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "release_running_job", Mock())
    monkeypatch.setattr(scheduler, "_interpreter_shutting_down", lambda *args: False)

    assert scheduler._submit_with_guard(_job(), pool, Mock()) is None
    escalations.assert_called_once()
    assert escalations.call_args.args[1] == "scheduler"
    assert "executor dispatch failed: RuntimeError: executor unavailable" in escalations.call_args.args[2]


def test_post_tick_cleanup_failure_is_escalated(monkeypatch):
    from cron import scheduler

    escalations = Mock()
    monkeypatch.setattr(scheduler, "_escalate_cron_failure", escalations)
    monkeypatch.setattr(
        "tools.mcp_tool_lifecycle._kill_orphaned_mcp_children",
        Mock(side_effect=RuntimeError("cleanup exploded")),
    )

    scheduler._sweep_mcp_orphans()

    escalations.assert_called_once()
    assert escalations.call_args.args[1] == "scheduler"
    assert "post-tick MCP orphan cleanup failed: RuntimeError: cleanup exploded" in (
        escalations.call_args.args[2]
    )


def test_no_agent_script_failure_preserves_raw_stderr_and_escalates(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(no_agent=True), "script", "exit 7\nstderr: AZURE_TOKEN missing",
        hermes_home=tmp_path, bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert result.status == "captured_test"
    report = _capture(tmp_path)[0]["message"]
    assert report.startswith("[ESCALATION]\n")
    assert "exit 7\nstderr: AZURE_TOKEN missing" in report
    assert "failure_deliver=local" in report
    assert "AZURE_TOKEN missing" not in result.public_error


def test_monitor_and_agent_failures_are_escalated(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))

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
        assert result.status == "captured_test"
        assert raw in _capture(tmp_path)[-1]["message"]
    bridge.assert_not_called()


def test_na_like_output_is_an_explicit_failure_not_healthy(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(), "script", "n/a", hermes_home=tmp_path, bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert result.status == "captured_test"
    assert "raw failure evidence: n/a" in _capture(tmp_path)[0]["message"].lower()
    assert result.public_error


def test_test_gate_creates_durable_local_record_without_bridge(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))
    result = escalate_cron_failure(
        _job(), "agent", "ProviderError: offline", hermes_home=tmp_path,
        bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert result.status == "captured_test"
    assert result.queue_path is not None and result.queue_path.exists()
    record = json.loads(result.queue_path.read_text())
    assert record["status"] == "captured_test"
    assert record["retry_count"] == 0
    assert record["raw_error"] == "ProviderError: offline"
    assert record["bridge_error"] is None


def test_job_metadata_is_redacted_before_transport_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.redact.redact_sensitive_text",
        lambda text: text.replace("secret-name", "[REDACTED]")
    )
    bridge = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(name="secret-name"), "agent", "boom", hermes_home=tmp_path, bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert "secret-name" not in _capture(tmp_path)[0]["message"]
    assert result.queue_path is not None
    assert json.loads(result.queue_path.read_text())["job_name"] == "[REDACTED]"


def test_queue_is_persisted_before_capture(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))

    result = escalate_cron_failure(
        _job(), "agent", "boom", hermes_home=tmp_path, bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert result.status == "captured_test"
    assert result.queue_path is not None and result.queue_path.exists()
    assert len(_capture(tmp_path)) == 1


def test_identical_failure_is_deduped_for_24_hours(tmp_path):
    bridge = Mock(side_effect=AssertionError("production sender called"))

    first = escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, bridge_send=bridge,
    )
    second = escalate_cron_failure(
        _job(), "agent", "same exact error", hermes_home=tmp_path, bridge_send=bridge,
    )

    bridge.assert_not_called()
    assert first.escalation_id == second.escalation_id
    assert second.deduped is True
    assert len(_capture(tmp_path)) == 1


def test_queued_failure_retry_is_captured_under_pytest(tmp_path):
    first = escalate_cron_failure(
        _job(), "agent", "offline", hermes_home=tmp_path, now=100,
    )
    assert first.queue_path is not None
    record = json.loads(first.queue_path.read_text())
    record["status"] = "queued"
    first.queue_path.write_text(json.dumps(record))
    bridge = Mock(side_effect=AssertionError("production sender called"))

    accepted = retry_queued_escalations(
        hermes_home=tmp_path, bridge_send=bridge, now=401,
    )

    bridge.assert_not_called()
    assert accepted == 0
    assert json.loads(first.queue_path.read_text())["status"] == "queued"
    assert _capture(tmp_path)[-1]["kind"] == "retry"


def test_exhausted_queue_record_is_retained_as_dead_letter(tmp_path):
    first = escalate_cron_failure(
        _job(), "agent", "offline", hermes_home=tmp_path, now=100,
    )
    assert first.queue_path is not None
    record = json.loads(first.queue_path.read_text())
    record["status"] = "queued"
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
