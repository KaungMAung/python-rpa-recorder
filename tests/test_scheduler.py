from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def test_schedule_next_run_recomputes_without_touching_history() -> None:
    from rpa.scheduler import FlowSchedule, schedule_next_run

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(
        flow_name="demo",
        enabled=True,
        interval_minutes=30,
        last_status="Success",
        last_error="stale",
    )
    schedule_next_run(schedule, now)
    assert schedule.last_status == "Success"
    assert schedule.last_error == "stale"
    next_run = datetime.fromisoformat(schedule.next_run_at)
    assert next_run == now + timedelta(minutes=30)


def test_schedule_disabled_is_never_due() -> None:
    from rpa.scheduler import FlowSchedule, is_due

    schedule = FlowSchedule(flow_name="demo", enabled=False)
    assert is_due(schedule, datetime.now(timezone.utc)) is False


def test_schedule_with_no_next_run_is_due_immediately() -> None:
    from rpa.scheduler import FlowSchedule, is_due

    schedule = FlowSchedule(flow_name="demo", enabled=True)
    assert is_due(schedule, datetime.now(timezone.utc)) is True


def test_schedule_not_due_before_next_run_time() -> None:
    from rpa.scheduler import FlowSchedule, is_due

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(
        flow_name="demo",
        enabled=True,
        interval_minutes=60,
        next_run_at=(now + timedelta(minutes=30)).isoformat(),
    )
    assert is_due(schedule, now) is False


def test_schedule_due_after_next_run_time() -> None:
    from rpa.scheduler import FlowSchedule, is_due

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(
        flow_name="demo",
        enabled=True,
        interval_minutes=60,
        next_run_at=(now - timedelta(minutes=1)).isoformat(),
    )
    assert is_due(schedule, now) is True


def test_mark_started_and_finished_updates_next_run() -> None:
    from rpa.scheduler import FlowSchedule, is_due, mark_finished, mark_started

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(flow_name="demo", enabled=True, interval_minutes=60)
    mark_started(schedule, now)
    assert schedule.last_run_at == now.isoformat()
    assert schedule.last_status == "Running"
    mark_finished(schedule, "success", now)
    assert schedule.last_status == "success"
    assert is_due(schedule, now) is False
    assert is_due(schedule, now + timedelta(minutes=61)) is True


def test_paused_schedule_is_never_due_even_if_enabled() -> None:
    from rpa.scheduler import FlowSchedule, is_due

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(flow_name="demo", enabled=True, paused=True)
    assert is_due(schedule, now) is False


def test_mark_finished_records_duration_and_error() -> None:
    from rpa.scheduler import FlowSchedule, mark_finished, mark_started, STATUS_FAILED

    started = datetime.now(timezone.utc)
    finished = started + timedelta(seconds=42)
    schedule = FlowSchedule(flow_name="demo", enabled=True)
    mark_started(schedule, started)
    mark_finished(schedule, STATUS_FAILED, finished, error="boom")
    assert schedule.last_duration_seconds == pytest.approx(42.0)
    assert schedule.last_status == STATUS_FAILED
    assert schedule.last_error == "boom"


def test_run_history_is_started_then_finalized_with_failed_step() -> None:
    from rpa.scheduler import FlowSchedule, mark_finished, mark_started, STATUS_FAILED, STATUS_RUNNING

    started = datetime.now(timezone.utc)
    finished = started + timedelta(seconds=8)
    schedule = FlowSchedule(flow_name="demo", enabled=True)
    mark_started(schedule, started)
    assert len(schedule.history) == 1
    assert schedule.history[0].status == STATUS_RUNNING
    assert schedule.history[0].finished_at is None

    mark_finished(schedule, STATUS_FAILED, finished, error="target missing", failed_step=4, attempts=3)
    assert len(schedule.history) == 1
    entry = schedule.history[0]
    assert entry.started_at == started.isoformat()
    assert entry.finished_at == finished.isoformat()
    assert entry.duration_seconds == pytest.approx(8.0)
    assert entry.status == STATUS_FAILED
    assert entry.failed_step == 4
    assert entry.error == "target missing"
    assert entry.attempts == 3


def test_run_history_persists_optional_evidence_reference() -> None:
    from rpa.scheduler import FlowSchedule, RunHistoryEntry, mark_started

    schedule = FlowSchedule(flow_name="demo")
    mark_started(schedule, source="Manual", evidence_path="runs/example", run_id="abc123")
    restored = RunHistoryEntry.from_dict(schedule.to_dict()["history"][0])
    assert restored is not None
    assert restored.source == "Manual"
    assert restored.evidence_path == "runs/example"
    assert restored.run_id == "abc123"


def test_mark_finished_clears_error_on_success_after_previous_failure() -> None:
    from rpa.scheduler import FlowSchedule, mark_finished, mark_started, STATUS_SUCCESS

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(flow_name="demo", enabled=True, last_error="old failure")
    mark_started(schedule, now)
    mark_finished(schedule, STATUS_SUCCESS, now)
    assert schedule.last_error is None


def test_mark_skipped_does_not_touch_duration_and_retries_soon() -> None:
    from rpa.scheduler import FlowSchedule, mark_skipped, STATUS_SKIPPED_RUNNING

    now = datetime.now(timezone.utc)
    schedule = FlowSchedule(flow_name="demo", enabled=True, interval_minutes=60)
    mark_skipped(schedule, STATUS_SKIPPED_RUNNING, now)
    assert schedule.last_status == STATUS_SKIPPED_RUNNING
    assert schedule.last_duration_seconds is None
    next_run = datetime.fromisoformat(schedule.next_run_at)
    assert next_run <= now + timedelta(minutes=2)
    assert schedule.history[0].status == STATUS_SKIPPED_RUNNING
    assert schedule.history[0].started_at == now.isoformat()
    assert schedule.history[0].finished_at == now.isoformat()
    assert schedule.history[0].duration_seconds == 0.0


def test_schedule_store_persists_and_reloads(tmp_path: Path) -> None:
    from rpa.scheduler import FlowSchedule, ScheduleStore

    flows_root = tmp_path / "flows"
    (flows_root / "demo").mkdir(parents=True)
    (flows_root / "demo" / "project.json").write_text("{}", encoding="utf-8")

    store = ScheduleStore(flows_root)
    assert store.list_flow_names() == ["demo"]

    schedule = store.get("demo")
    schedule.enabled = True
    schedule.interval_minutes = 120
    store.set(schedule)
    store.save()

    reloaded = ScheduleStore(flows_root)
    reloaded_schedule = reloaded.get("demo")
    assert reloaded_schedule.enabled is True
    assert reloaded_schedule.interval_minutes == 120


def test_schedule_store_removes_missing_flows(tmp_path: Path) -> None:
    from rpa.scheduler import ScheduleStore

    flows_root = tmp_path / "flows"
    (flows_root / "demo").mkdir(parents=True)
    (flows_root / "demo" / "project.json").write_text("{}", encoding="utf-8")

    store = ScheduleStore(flows_root)
    store.get("ghost").enabled = True
    store.set(store.get("ghost"))
    store.save()

    store.remove_missing_flows()
    assert "ghost" not in store._schedules
    assert store.list_flow_names() == ["demo"]


def test_history_persists_and_is_limited_per_flow(tmp_path: Path) -> None:
    from rpa.scheduler import FlowSchedule, RunHistoryEntry, ScheduleStore

    flows_root = tmp_path / "flows"
    (flows_root / "demo").mkdir(parents=True)
    (flows_root / "demo" / "project.json").write_text("{}", encoding="utf-8")
    store = ScheduleStore(flows_root, history_limit=3)
    schedule = FlowSchedule(flow_name="demo")
    schedule.history = [
        RunHistoryEntry(started_at=f"2026-01-01T00:0{index}:00+00:00", status="Success")
        for index in range(5)
    ]
    store.set(schedule)
    store.save()

    reloaded = ScheduleStore(flows_root, history_limit=3)
    assert len(reloaded.get("demo").history) == 3
    assert reloaded.get("demo").history[0].started_at.endswith("02:00+00:00")


def test_legacy_last_run_fields_are_migrated_to_history(tmp_path: Path) -> None:
    import json
    from rpa.scheduler import ScheduleStore

    flows_root = tmp_path / "flows"
    (flows_root / "demo").mkdir(parents=True)
    (flows_root / "demo" / "project.json").write_text("{}", encoding="utf-8")
    legacy = {
        "demo": {
            "enabled": True,
            "last_run_at": "2026-01-01T00:00:00+00:00",
            "last_finished_at": "2026-01-01T00:00:05+00:00",
            "last_duration_seconds": 5,
            "last_status": "Success",
        }
    }
    (flows_root / "schedules.json").write_text(json.dumps(legacy), encoding="utf-8")

    schedule = ScheduleStore(flows_root).get("demo")
    assert len(schedule.history) == 1
    assert schedule.history[0].status == "Success"
    assert schedule.history[0].duration_seconds == 5
