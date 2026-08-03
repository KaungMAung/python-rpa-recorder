from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from rpa.external_run_log import build_run_log_payload, send_external_run_log
from rpa.models import ProjectSettings
from rpa.scheduler import FlowSchedule, RunHistoryEntry, mark_started
from ui.main_window import MainWindow


class Response:
    def __init__(self, status_code: int = 200, value=None, text: str = "") -> None:
        self.status_code = status_code
        self.value = {} if value is None else value
        self.text = text

    def json(self):
        return self.value


def enabled_settings() -> ProjectSettings:
    return ProjectSettings(
        send_run_log_to_sharepoint=True,
        run_log_webhook_url="https://example.test/trigger?sig=top-secret",
        run_log_timeout_seconds=30,
    )


def history_entry(status: str = "COMPLETED_VERIFIED", error: str | None = None) -> RunHistoryEntry:
    return RunHistoryEntry(
        started_at="2026-08-01T01:00:00+00:00",
        finished_at="2026-08-01T01:00:02+00:00",
        duration_seconds=2.0,
        status=status,
        failed_step=2 if error else None,
        error=error,
        attempts=3,
        source="Manual",
        run_id="run-123",
        retry_count=1,
        fallback_executed=True,
    )


def test_external_run_log_is_disabled_by_default(monkeypatch) -> None:
    called = False

    def post(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("rpa.webhook.requests.post", post)
    assert send_external_run_log(ProjectSettings(), "Flow A", history_entry()) is False
    assert called is False


def test_payload_uses_canonical_history_and_step_results(monkeypatch) -> None:
    monkeypatch.setattr("rpa.external_run_log.platform.node", lambda: "PC-01")
    monkeypatch.setattr("rpa.external_run_log.getpass.getuser", lambda: "operator")
    payload = build_run_log_payload(
        "Flow A", history_entry("Failed", "target missing"),
        [{"status": "completed"}, {"status": "failed"}, {"status": "skipped"}],
    )
    assert payload == {
        "run_id": "run-123", "flow_name": "Flow A", "status": "Failed",
        "started_at": "2026-08-01T01:00:00+00:00",
        "finished_at": "2026-08-01T01:00:02+00:00", "duration_seconds": 2.0,
        "machine_name": "PC-01", "user_name": "operator", "source": "Manual",
        "error": "target missing", "failed_step": 2, "attempts": 3,
        "retry_count": 1, "fallback_executed": "True", "evidence_path": "",
        "step_count": 3,
        "completed_step_count": 1, "failed_step_count": 1, "skipped_step_count": 1,
    }


def test_payload_normalizes_null_and_boolean_values_for_webhook_schema(monkeypatch) -> None:
    """failed_step/error/evidence_path default safely and fallback_executed is a string."""
    monkeypatch.setattr("rpa.external_run_log.platform.node", lambda: "PC-01")
    monkeypatch.setattr("rpa.external_run_log.getpass.getuser", lambda: "operator")
    entry = RunHistoryEntry(
        started_at="2026-08-01T01:00:00+00:00",
        finished_at="2026-08-01T01:00:02+00:00",
        duration_seconds=2.0,
        status="COMPLETED_VERIFIED",
        failed_step=None,
        error=None,
        attempts=1,
        source="Manual",
        run_id="run-456",
        evidence_path=None,
        retry_count=0,
        fallback_executed=False,
    )
    payload = build_run_log_payload("Flow A", entry, [])
    assert payload["failed_step"] == 0
    assert payload["error"] == ""
    assert payload["evidence_path"] == ""
    assert payload["fallback_executed"] == "False"
    assert isinstance(payload["fallback_executed"], str)
    assert isinstance(payload["attempts"], int)
    assert isinstance(payload["retry_count"], int)
    assert isinstance(payload["duration_seconds"], (int, float))
    assert isinstance(payload["started_at"], str)
    assert isinstance(payload["finished_at"], str)
    assert isinstance(payload["flow_name"], str)
    assert isinstance(payload["run_id"], str)
    assert isinstance(payload["source"], str)
    assert isinstance(payload["status"], str)


@pytest.mark.parametrize("status", ["COMPLETED_VERIFIED", "Failed"])
def test_completed_and_failed_runs_are_posted(monkeypatch, status) -> None:
    calls = []
    monkeypatch.setattr(
        "rpa.webhook.requests.post",
        lambda url, json, timeout: calls.append((url, json, timeout)) or Response(),
    )
    assert send_external_run_log(enabled_settings(), "Flow A", history_entry(status)) is True
    assert calls[0][1]["status"] == status
    assert calls[0][2] == 30


@pytest.mark.parametrize(
    "failure",
    [
        requests.Timeout("timed out"),
        requests.ConnectionError("connection failed at https://example.test/trigger?sig=top-secret"),
        Response(503),
    ],
)
def test_http_failures_only_warn_and_preserve_result(monkeypatch, failure) -> None:
    def post(*_args, **_kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr("rpa.webhook.requests.post", post)
    logs = []
    entry = history_entry("Failed", "original execution error")
    assert send_external_run_log(enabled_settings(), "Flow A", entry, log=logs.append) is False
    assert entry.status == "Failed"
    assert entry.error == "original execution error"
    assert len(logs) == 1 and "warning" in logs[0].lower()
    assert "top-secret" not in logs[0]


def test_active_history_is_saved_before_external_delivery() -> None:
    events = []
    schedule = FlowSchedule("Flow A")
    mark_started(schedule, datetime(2026, 8, 1, tzinfo=timezone.utc), run_id="run-123")

    class Store:
        def get(self, _flow_name):
            return schedule

        def set(self, _schedule):
            events.append("set")

        def save(self):
            events.append("save")

    window = SimpleNamespace(
        _active_history_flow="Flow A",
        schedule_store=Store(),
        _active_secret_values=set(),
        project=SimpleNamespace(settings=enabled_settings()),
        _mask_evidence_value=lambda value: value,
        _post_external_run_log=lambda *_args: events.append("external"),
    )
    MainWindow._finish_active_history(window, "COMPLETED_VERIFIED", None, None, 1)
    assert events == ["set", "save", "external"]
