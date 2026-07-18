"""Persistence and timing logic for automatically running flows on a schedule.

This module is intentionally free of any Qt/UI dependency so the scheduling
math (is a flow due? when does it run next?) can be unit tested directly.

Schedules are stored permanently in ``schedules.json`` inside the flows
directory, so configuration (enabled/paused state, interval, last run
history) survives app restarts.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATUS_SUCCESS = "Success"
STATUS_FAILED = "Failed"
STATUS_RUNNING = "Running"
STATUS_STOPPED = "Stopped"
STATUS_SKIPPED_RUNNING = "Skipped (Already Running)"
STATUS_SKIPPED_BUSY = "Skipped (Flow Open In Editor)"
DEFAULT_HISTORY_LIMIT = 100


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
    enabled: bool = False
    paused: bool = False
    interval_minutes: int = 60
    last_run_at: str | None = None
    last_finished_at: str | None = None
    last_duration_seconds: float | None = None
    last_status: str | None = None
    last_error: str | None = None
    next_run_at: str | None = None
    history: list[RunHistoryEntry] = field(default_factory=list)

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
            enabled=bool(data.get("enabled", False)),
            paused=bool(data.get("paused", False)),
            interval_minutes=int(data.get("interval_minutes") or 60),
            last_run_at=data.get("last_run_at"),
            last_finished_at=data.get("last_finished_at"),
            last_duration_seconds=data.get("last_duration_seconds"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            next_run_at=data.get("next_run_at"),
            history=history,
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
        self.load()

    def load(self) -> None:
        self._schedules = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        for flow_name, data in raw.items():
            if isinstance(data, dict):
                self._schedules[flow_name] = FlowSchedule.from_dict(flow_name, data)

    def save(self) -> None:
        self.flows_root.mkdir(parents=True, exist_ok=True)
        for schedule in self._schedules.values():
            _trim_history(schedule, self.history_limit)
        payload = {name: schedule.to_dict() for name, schedule in self._schedules.items()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def set_history_limit(self, limit: int) -> None:
        """Change retention and immediately trim persisted in-memory histories."""
        self.history_limit = min(1000, max(1, int(limit)))
        for schedule in self._schedules.values():
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
        return self._schedules.setdefault(flow_name, FlowSchedule(flow_name=flow_name))

    def set(self, schedule: FlowSchedule) -> None:
        self._schedules[schedule.flow_name] = schedule

    def remove_missing_flows(self) -> None:
        existing = set(self.list_flow_names())
        for name in list(self._schedules):
            if name not in existing:
                del self._schedules[name]

    def due_flows(self, now: datetime | None = None) -> list[FlowSchedule]:
        now = now or utc_now()
        return [schedule for schedule in self._schedules.values() if is_due(schedule, now)]
