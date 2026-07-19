"""Persistence and timing logic for automatically running flows on a schedule.

This module is intentionally free of any Qt/UI dependency so the scheduling
math (is a flow due? when does it run next?) can be unit tested directly.

Schedules are stored permanently in ``schedules.json`` inside the flows
directory, so configuration (enabled/paused state, interval, last run
history) survives app restarts.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

STATUS_SUCCESS = "Success"
STATUS_FAILED = "Failed"
STATUS_RUNNING = "Running"
STATUS_STOPPED = "Stopped"
STATUS_SKIPPED_RUNNING = "Skipped (Already Running)"
STATUS_SKIPPED_BUSY = "Skipped (Flow Open In Editor)"
DEFAULT_HISTORY_LIMIT = 100
TASK_REGISTERED = "Registered"
TASK_DISABLED = "Disabled"
TASK_RUNNING = "Running"
TASK_MISSING = "Task missing"
TASK_REGISTRATION_FAILED = "Registration failed"


def legacy_schedule_id(flow_name: str) -> str:
    """Stable ID for the single schedule used by pre-multi-schedule projects."""
    return uuid5(NAMESPACE_URL, f"python-rpa-recorder:{flow_name}").hex[:12]


@dataclass
class RunHistoryEntry:
    """One persisted scheduler run attempt."""

    started_at: str
    finished_at: str | None = None
    duration_seconds: float | None = None
    status: str = STATUS_RUNNING
    failed_step: int | None = None
    error: str | None = None
    attempts: int | None = None
    source: str | None = None
    evidence_path: str | None = None
    run_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunHistoryEntry | None":
        started_at = data.get("started_at")
        if not isinstance(started_at, str) or not started_at:
            return None
        failed_step = data.get("failed_step")
        try:
            failed_step = int(failed_step) if failed_step is not None else None
        except (TypeError, ValueError):
            failed_step = None
        duration = data.get("duration_seconds")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return cls(
            started_at=started_at,
            finished_at=data.get("finished_at"),
            duration_seconds=duration,
            status=str(data.get("status") or STATUS_RUNNING),
            failed_step=failed_step,
            error=data.get("error"),
            attempts=_optional_int(data.get("attempts")),
            source=data.get("source"),
            evidence_path=data.get("evidence_path"),
            run_id=data.get("run_id"),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FlowSchedule:
    flow_name: str
    flow_id: str = ""
    schedule_id: str = ""
    enabled: bool = False
    paused: bool = False
    interval_minutes: int = 60
    last_run_at: str | None = None
    last_finished_at: str | None = None
    last_duration_seconds: float | None = None
    last_status: str | None = None
    last_error: str | None = None
    next_run_at: str | None = None
    runtime_inputs: dict[str, Any] = field(default_factory=dict)
    history: list[RunHistoryEntry] = field(default_factory=list)
    task_status: str = TASK_MISSING
    task_error: str | None = None
    execution_timeout_minutes: int | None = None
    run_with_highest_privileges: bool = False
    windows_task_name: str = ""

    @classmethod
    def from_dict(cls, flow_name: str, data: dict[str, Any]) -> "FlowSchedule":
        history = []
        for raw_entry in data.get("history", []):
            if isinstance(raw_entry, dict):
                entry = RunHistoryEntry.from_dict(raw_entry)
                if entry is not None:
                    history.append(entry)
        # Older schedules remain valid and gain one initial history record from
        # their existing last-run fields when they are next saved.
        if not history and data.get("last_status") and data.get("last_run_at"):
            history.append(RunHistoryEntry(
                started_at=data["last_run_at"],
                finished_at=data.get("last_finished_at"),
                duration_seconds=data.get("last_duration_seconds"),
                status=data["last_status"],
                error=data.get("last_error"),
            ))
        return cls(
            flow_name=flow_name,
            flow_id=str(data.get("flow_id") or ""),
            schedule_id=str(data.get("schedule_id") or legacy_schedule_id(flow_name)),
            enabled=bool(data.get("enabled", False)),
            paused=bool(data.get("paused", False)),
            interval_minutes=int(data.get("interval_minutes") or 60),
            last_run_at=data.get("last_run_at"),
            last_finished_at=data.get("last_finished_at"),
            last_duration_seconds=data.get("last_duration_seconds"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            next_run_at=data.get("next_run_at"),
            runtime_inputs=dict(data.get("runtime_inputs") or {}),
            history=history,
            task_status=str(data.get("task_status") or TASK_MISSING),
            task_error=data.get("task_error"),
            execution_timeout_minutes=_optional_positive_int(data.get("execution_timeout_minutes")),
            run_with_highest_privileges=bool(data.get("run_with_highest_privileges", False)),
            windows_task_name=str(data.get("windows_task_name") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("flow_name")
        return data


def is_due(schedule: FlowSchedule, now: datetime | None = None) -> bool:
    """Return True if an enabled, unpaused schedule's next run time has arrived."""
    if not schedule.enabled or schedule.paused:
        return False
    now = now or utc_now()
    if not schedule.next_run_at:
        return True
    try:
        next_run = datetime.fromisoformat(schedule.next_run_at)
    except ValueError:
        return True
    return now >= next_run


def schedule_next_run(schedule: FlowSchedule, now: datetime | None = None) -> None:
    """Recompute next_run_at from now, without touching run history (status/duration/error)."""
    now = now or utc_now()
    schedule.next_run_at = (now + timedelta(minutes=max(1, schedule.interval_minutes))).isoformat()


def mark_started(
    schedule: FlowSchedule,
    now: datetime | None = None,
    source: str | None = None,
    evidence_path: str | None = None,
    run_id: str | None = None,
) -> None:
    now = now or utc_now()
    schedule.last_run_at = now.isoformat()
    schedule.last_finished_at = None
    schedule.last_duration_seconds = None
    schedule.last_status = STATUS_RUNNING
    schedule.last_error = None
    schedule.history.append(RunHistoryEntry(
        started_at=now.isoformat(), attempts=0, source=source,
        evidence_path=evidence_path, run_id=run_id,
    ))


def mark_finished(
    schedule: FlowSchedule,
    status: str,
    now: datetime | None = None,
    error: str | None = None,
    failed_step: int | None = None,
    attempts: int | None = None,
) -> None:
    now = now or utc_now()
    was_running = schedule.last_status == STATUS_RUNNING
    schedule.last_finished_at = now.isoformat()
    schedule.last_duration_seconds = _duration_seconds(schedule.last_run_at, now)
    schedule.last_status = status
    schedule.last_error = error
    entry = next(
        (item for item in reversed(schedule.history) if item.status == STATUS_RUNNING and item.finished_at is None),
        None,
    ) if was_running else None
    if entry is None:
        started_at = now.isoformat()
        entry = RunHistoryEntry(started_at=started_at)
        schedule.history.append(entry)
        schedule.last_run_at = started_at
        schedule.last_duration_seconds = 0.0
    entry.finished_at = now.isoformat()
    entry.duration_seconds = schedule.last_duration_seconds
    entry.status = status
    entry.failed_step = failed_step
    entry.error = error
    if attempts is not None:
        entry.attempts = attempts
    schedule_next_run(schedule, now)


def mark_skipped(
    schedule: FlowSchedule,
    status: str,
    now: datetime | None = None,
    source: str | None = None,
    evidence_path: str | None = None,
    run_id: str | None = None,
) -> None:
    """Record a run that never started because of an overlap, without touching duration."""
    now = now or utc_now()
    schedule.last_status = status
    schedule.last_error = None
    schedule.history.append(RunHistoryEntry(
        started_at=now.isoformat(),
        finished_at=now.isoformat(),
        duration_seconds=0.0,
        status=status,
        attempts=0,
        source=source,
        evidence_path=evidence_path,
        run_id=run_id,
    ))
    # Retry soon instead of waiting a full interval, since this attempt never ran.
    schedule.next_run_at = (now + timedelta(minutes=1)).isoformat()


def _duration_seconds(started_at: str | None, finished_at: datetime) -> float | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return max(0.0, (finished_at - started).total_seconds())


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_positive_int(value: Any) -> int | None:
    parsed = _optional_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _trim_history(schedule: FlowSchedule, limit: int) -> None:
    limit = max(1, int(limit))
    if len(schedule.history) > limit:
        del schedule.history[:-limit]


class ScheduleStore:
    """Persists per-flow schedule configuration to schedules.json inside flows_root."""

    def __init__(self, flows_root: Path, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.flows_root = Path(flows_root)
        self.path = self.flows_root / "schedules.json"
        self.history_limit = min(1000, max(1, int(history_limit)))
        self._schedules: dict[str, FlowSchedule] = {}
        self._additional_schedules: dict[str, FlowSchedule] = {}
        self._task_registration_migrations: set[str] = set()
        self.load()

    def load(self) -> None:
        self._schedules = {}
        self._additional_schedules = {}
        self._task_registration_migrations = set()
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        for flow_name, data in raw.items():
            if isinstance(data, dict):
                primary = FlowSchedule.from_dict(flow_name, data)
                self._ensure_flow_identity(primary)
                self._schedules[flow_name] = primary
                if "task_status" not in data:
                    self._task_registration_migrations.add(primary.schedule_id)
                for extra_data in data.get("additional_schedules", []):
                    if not isinstance(extra_data, dict):
                        continue
                    extra = FlowSchedule.from_dict(flow_name, extra_data)
                    self._ensure_flow_identity(extra)
                    if extra.schedule_id and extra.schedule_id != primary.schedule_id:
                        self._additional_schedules[extra.schedule_id] = extra
                        if "task_status" not in extra_data:
                            self._task_registration_migrations.add(extra.schedule_id)

    def save(self) -> None:
        self.flows_root.mkdir(parents=True, exist_ok=True)
        for schedule in [*self._schedules.values(), *self._additional_schedules.values()]:
            _trim_history(schedule, self.history_limit)
        payload = {}
        for name, schedule in self._schedules.items():
            data = schedule.to_dict()
            data["additional_schedules"] = [
                extra.to_dict() for extra in self._additional_schedules.values()
                if extra.flow_name == name
            ]
            payload[name] = data
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            for attempt in range(5):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    # Windows can briefly deny replacement while a background
                    # refresh is opening the previous JSON file.
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if temporary.exists():
                temporary.unlink()

    def set_history_limit(self, limit: int) -> None:
        """Change retention and immediately trim persisted in-memory histories."""
        self.history_limit = min(1000, max(1, int(limit)))
        for schedule in [*self._schedules.values(), *self._additional_schedules.values()]:
            _trim_history(schedule, self.history_limit)

    def list_flow_names(self) -> list[str]:
        if not self.flows_root.exists():
            return []
        return sorted(
            child.name
            for child in self.flows_root.iterdir()
            if child.is_dir() and (child / "project.json").exists()
        )

    def get(self, flow_name: str) -> FlowSchedule:
        if flow_name in self._additional_schedules:
            return self._additional_schedules[flow_name]
        schedule = self._schedules.setdefault(
            flow_name, FlowSchedule(
                flow_name=flow_name, flow_id=self._flow_id(flow_name),
                schedule_id=legacy_schedule_id(flow_name),
            ),
        )
        self._ensure_flow_identity(schedule)
        return schedule

    def get_by_id(self, schedule_id: str) -> FlowSchedule | None:
        if schedule_id in self._additional_schedules:
            return self._additional_schedules[schedule_id]
        return next(
            (schedule for schedule in self._schedules.values() if schedule.schedule_id == schedule_id), None,
        )

    def needs_task_registration_migration(self, schedule_id: str) -> bool:
        """True only for schedules saved before Windows task status existed."""
        return schedule_id in self._task_registration_migrations

    def mark_task_registration_attempted(self, schedule_id: str) -> None:
        self._task_registration_migrations.discard(schedule_id)

    def list_schedules(self) -> list[FlowSchedule]:
        for flow_name in self.list_flow_names():
            self.get(flow_name)
        existing = set(self.list_flow_names())
        schedules = [schedule for name, schedule in self._schedules.items() if name in existing]
        schedules.extend(
            schedule for schedule in self._additional_schedules.values()
            if schedule.flow_name in existing
        )
        return schedules

    def cached_schedules(self) -> list[FlowSchedule]:
        """Return the loaded model without scanning project directories.

        UI refreshes use this for immediate redraws while filesystem discovery
        happens in a background ScheduleStore instance.
        """
        return [*self._schedules.values(), *self._additional_schedules.values()]

    def adopt_loaded_state(self, other: "ScheduleStore") -> None:
        """Adopt a completed background load without reading disk again."""
        if self.flows_root.resolve() != other.flows_root.resolve():
            raise ValueError("Cannot adopt schedules loaded from a different flows root")
        self._schedules = other._schedules
        self._additional_schedules = other._additional_schedules
        self._task_registration_migrations = other._task_registration_migrations

    def create_schedule(self, flow_name: str) -> FlowSchedule:
        self.get(flow_name)
        schedule = FlowSchedule(
            flow_name=flow_name, flow_id=self._flow_id(flow_name), schedule_id=uuid4().hex[:12],
        )
        self._additional_schedules[schedule.schedule_id] = schedule
        return schedule

    def remove_schedule(self, schedule_id: str) -> FlowSchedule | None:
        extra = self._additional_schedules.pop(schedule_id, None)
        if extra is not None:
            return extra
        for flow_name, schedule in list(self._schedules.items()):
            if schedule.schedule_id == schedule_id:
                removed = schedule
                self._schedules[flow_name] = FlowSchedule(
                    flow_name=flow_name, flow_id=self._flow_id(flow_name),
                    schedule_id=legacy_schedule_id(flow_name),
                )
                return removed
        return None

    def set(self, schedule: FlowSchedule) -> None:
        self._ensure_flow_identity(schedule)
        primary = self._schedules.get(schedule.flow_name)
        if primary is None or primary.schedule_id == schedule.schedule_id:
            self._schedules[schedule.flow_name] = schedule
        else:
            self._additional_schedules[schedule.schedule_id] = schedule

    def remove_missing_flows(self) -> None:
        existing = set(self.list_flow_names())
        for name in list(self._schedules):
            if name not in existing:
                del self._schedules[name]
        for schedule_id, schedule in list(self._additional_schedules.items()):
            if schedule.flow_name not in existing:
                del self._additional_schedules[schedule_id]

    def due_flows(self, now: datetime | None = None) -> list[FlowSchedule]:
        now = now or utc_now()
        return [schedule for schedule in self.list_schedules() if is_due(schedule, now)]

    def _ensure_flow_identity(self, schedule: FlowSchedule) -> None:
        if not schedule.flow_id:
            schedule.flow_id = self._flow_id(schedule.flow_name)

    def _flow_id(self, flow_name: str) -> str:
        project_json = self.flows_root / flow_name / "project.json"
        try:
            raw = json.loads(project_json.read_text(encoding="utf-8"))
            value = str((raw.get("project") or {}).get("id") or "").strip()
            if value:
                return value
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return uuid5(NAMESPACE_URL, f"python-rpa-recorder-flow:{flow_name}").hex
