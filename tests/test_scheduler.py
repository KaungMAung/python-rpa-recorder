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
