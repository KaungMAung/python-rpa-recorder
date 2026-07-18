"""Narrow Windows Task Scheduler integration for saved flow schedules."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable

from .scheduler import (
    FlowSchedule, TASK_DISABLED, TASK_MISSING, TASK_REGISTERED,
    TASK_REGISTRATION_FAILED, TASK_RUNNING,
)

TASK_FOLDER = "RPA Recorder"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class TaskOperationResult:
    ok: bool
    status: str
    task_name: str
    error: str | None = None
    command: list[str] | None = None


def sanitize_task_component(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(value)).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or "Unnamed Flow")[:120]


def task_name(schedule: FlowSchedule) -> str:
    return f"{TASK_FOLDER}\\{sanitize_task_component(schedule.flow_name)} - {schedule.schedule_id}"


def standalone_runner_command(project_json: Path, schedule_id: str) -> list[str]:
    project_json = Path(project_json).resolve()
    arguments = ["--project", str(project_json), "--schedule-id", str(schedule_id), "--scheduled-run"]
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *arguments]
    app_script = Path(__file__).resolve().parents[1] / "app.py"
    return [str(Path(sys.executable).resolve()), str(app_script), *arguments]


class WindowsTaskRegistrar:
    def __init__(
        self, command_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        popen: Callable[..., subprocess.Popen] | None = None,
        allow_elevation: bool = True,
    ) -> None:
        self._run = command_runner or subprocess.run
        self._popen = popen or subprocess.Popen
        self.allow_elevation = allow_elevation

    def sync(self, schedule: FlowSchedule, project_json: Path) -> TaskOperationResult:
        invalid = self._validate(schedule, project_json)
        if invalid:
            return self._failed(schedule, invalid)
        result = self._register(schedule, project_json)
        if not result.ok:
            return result
        if not schedule.enabled or schedule.paused:
            return self.disable(schedule)
        return result

    def delete(self, schedule: FlowSchedule) -> TaskOperationResult:
        result = self._run_schtasks(
            schedule, ["/Delete", "/TN", task_name(schedule), "/F"], missing_ok=True,
        )
        if not result.ok and self.allow_elevation and _needs_elevation(result.error):
            # The helper needs only the task definition and a syntactically valid project path field.
            result = self._run_elevated_helper("delete", schedule, Path.cwd() / "project.json")
        return result

    def enable(self, schedule: FlowSchedule) -> TaskOperationResult:
        result = self._run_schtasks(schedule, ["/Change", "/TN", task_name(schedule), "/ENABLE"])
        if result.ok:
            result.status = TASK_REGISTERED
        return result

    def disable(self, schedule: FlowSchedule) -> TaskOperationResult:
        result = self._run_schtasks(schedule, ["/Change", "/TN", task_name(schedule), "/DISABLE"])
        if result.ok:
            result.status = TASK_DISABLED
        return result

    def query(self, schedule: FlowSchedule) -> TaskOperationResult:
        result = self._run_schtasks(
            schedule, ["/Query", "/TN", task_name(schedule), "/FO", "LIST", "/V"],
            missing_ok=True,
        )
        if not result.ok or result.status == TASK_MISSING:
            return result
        output = (result.error or "").casefold()
        if "running" in output:
            result.status = TASK_RUNNING
        elif not schedule.enabled or schedule.paused:
            result.status = TASK_DISABLED
        else:
            result.status = TASK_REGISTERED
        result.error = None
        return result

    def test_run(self, schedule: FlowSchedule, project_json: Path) -> TaskOperationResult:
        invalid = self._validate(schedule, project_json)
        if invalid:
            return self._failed(schedule, invalid)
        command = standalone_runner_command(project_json, schedule.schedule_id)
        try:
            self._popen(
                command, cwd=str(Path(project_json).resolve().parent),
                creationflags=CREATE_NO_WINDOW,
            )
            return TaskOperationResult(True, schedule.task_status, task_name(schedule), command=command)
        except OSError as exc:
            return self._failed(schedule, f"Runner launch failed: {exc}", command)

    def _register(self, schedule: FlowSchedule, project_json: Path) -> TaskOperationResult:
        command = standalone_runner_command(project_json, schedule.schedule_id)
        folder_result = self._ensure_task_folder(schedule, command)
        if not folder_result.ok:
            if self.allow_elevation and _needs_elevation(folder_result.error):
                return self._run_elevated_helper("sync", schedule, project_json)
            return folder_result
        xml = task_xml(schedule, command)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as handle:
                handle.write(xml)
                temp_path = Path(handle.name)
            result = self._run_schtasks(
                schedule, ["/Create", "/TN", task_name(schedule), "/XML", str(temp_path), "/F"],
                command=command,
            )
            if not result.ok and self.allow_elevation and _needs_elevation(result.error):
                result = self._run_elevated_helper("sync", schedule, project_json)
            return result
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _ensure_task_folder(
        self, schedule: FlowSchedule, command: list[str],
    ) -> TaskOperationResult:
        if os.name != "nt":
            return self._failed(schedule, "Windows Task Scheduler is available only on Windows.", command)
        script = (
            "$service=New-Object -ComObject 'Schedule.Service';$service.Connect();"
            "$root=$service.GetFolder('\\');"
            "try{$null=$root.GetFolder('\\RPA Recorder')}"
            "catch{$null=$root.CreateFolder('RPA Recorder')}"
        )
        try:
            completed = self._run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failed(schedule, f"Could not prepare the Task Scheduler folder: {exc}", command)
        if completed.returncode == 0:
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule), command=command)
        detail = (completed.stderr or completed.stdout or "Task Scheduler folder creation failed.").strip()
        return self._failed(schedule, detail, command)

    def _run_schtasks(
        self, schedule: FlowSchedule, arguments: list[str], missing_ok: bool = False,
        command: list[str] | None = None,
    ) -> TaskOperationResult:
        if os.name != "nt":
            return self._failed(schedule, "Windows Task Scheduler is available only on Windows.", command)
        try:
            completed = self._run(
                ["schtasks.exe", *arguments], capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failed(schedule, f"Task Scheduler command failed: {exc}", command)
        output = "\n".join(value.strip() for value in (completed.stdout, completed.stderr) if value and value.strip())
        if completed.returncode == 0:
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule), output or None, command)
        if missing_ok and _is_missing(output):
            return TaskOperationResult(True, TASK_MISSING, task_name(schedule), None, command)
        return self._failed(
            schedule, output or f"Task Scheduler returned exit code {completed.returncode}.", command,
        )

    def _run_elevated_helper(
        self, operation: str, schedule: FlowSchedule, project_json: Path,
    ) -> TaskOperationResult:
        request_path = result_path = None
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="rpa-task-helper-"))
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"
            request_path.write_text(json.dumps({
                "operation": operation,
                "schedule": schedule.to_dict(),
                "flow_name": schedule.flow_name,
                "project_json": str(Path(project_json).resolve()),
            }), encoding="utf-8")
            helper = _helper_command(request_path, result_path)
            executable, arguments = helper[0], helper[1:]
            ps = (
                f"$p=Start-Process -FilePath '{_ps(executable)}' "
                f"-ArgumentList '{_ps(subprocess.list2cmdline(arguments))}' -Verb RunAs -Wait -PassThru; "
                "exit $p.ExitCode"
            )
            completed = self._run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=90,
            )
            if not result_path.exists():
                detail = (completed.stderr or completed.stdout or "UAC elevation was cancelled or failed.").strip()
                return self._failed(schedule, f"Task registration needs permission: {detail}")
            raw = json.loads(result_path.read_text(encoding="utf-8"))
            return TaskOperationResult(
                bool(raw.get("ok")), str(raw.get("status") or TASK_REGISTRATION_FAILED),
                task_name(schedule), raw.get("error"), standalone_runner_command(project_json, schedule.schedule_id),
            )
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return self._failed(schedule, f"Elevated task-registration helper failed: {exc}")
        finally:
            for path in (request_path, result_path):
                if path:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if request_path:
                try:
                    request_path.parent.rmdir()
                except OSError:
                    pass

    @staticmethod
    def _validate(schedule: FlowSchedule, project_json: Path) -> str | None:
        if not schedule.schedule_id:
            return "The schedule has no ID. Save the schedule and try again."
        path = Path(project_json)
        if not path.is_absolute():
            return "The project path must be absolute before registering a Windows task."
        if not path.is_file():
            return f"Project file does not exist: {path}"
        if schedule.interval_minutes < 1:
            return "The schedule interval must be at least one minute."
        return None

    @staticmethod
    def _failed(
        schedule: FlowSchedule, error: str, command: list[str] | None = None,
    ) -> TaskOperationResult:
        return TaskOperationResult(False, TASK_REGISTRATION_FAILED, task_name(schedule), error, command)


def task_xml(schedule: FlowSchedule, command: list[str]) -> str:
    start = _start_boundary(schedule)
    interval = max(1, int(schedule.interval_minutes))
    # The standalone runner enforces the requested timeout itself. Give its
    # cleanup/history handler a short grace period before Task Scheduler may
    # terminate the process forcibly.
    timeout = f"PT{int(schedule.execution_timeout_minutes)}M30S" if schedule.execution_timeout_minutes else "PT0S"
    run_level = "HighestAvailable" if schedule.run_with_highest_privileges else "LeastPrivilege"
    user = escape(_interactive_user())
    executable = escape(command[0])
    arguments = escape(subprocess.list2cmdline(command[1:]))
    try:
        project_arg = command[command.index("--project") + 1]
        working_path = Path(project_arg).resolve().parent
    except (ValueError, IndexError):
        working_path = Path(command[0]).resolve().parent
    working = escape(str(working_path))
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Python RPA Recorder schedule {escape(schedule.schedule_id)}</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    <Repetition><Interval>PT{interval}M</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
  </CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>{run_level}</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><ExecutionTimeLimit>{timeout}</ExecutionTimeLimit><Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{executable}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{working}</WorkingDirectory></Exec></Actions>
</Task>'''


def run_task_helper(request_path: Path, result_path: Path) -> int:
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        schedule = FlowSchedule.from_dict(str(request["flow_name"]), dict(request["schedule"]))
        registrar = WindowsTaskRegistrar(allow_elevation=False)
        operation = str(request.get("operation"))
        if operation == "sync":
            result = registrar.sync(schedule, Path(request["project_json"]))
        elif operation == "delete":
            result = registrar.delete(schedule)
        else:
            raise ValueError(f"Unsupported helper operation: {operation}")
        Path(result_path).write_text(json.dumps(result.__dict__), encoding="utf-8")
        return 0 if result.ok else 1
    except Exception as exc:
        try:
            Path(result_path).write_text(json.dumps({
                "ok": False, "status": TASK_REGISTRATION_FAILED, "error": str(exc),
            }), encoding="utf-8")
        except OSError:
            pass
        return 1


def _start_boundary(schedule: FlowSchedule) -> str:
    try:
        value = datetime.fromisoformat(schedule.next_run_at) if schedule.next_run_at else None
    except (TypeError, ValueError):
        value = None
    value = value or (datetime.now().astimezone() + timedelta(minutes=max(1, schedule.interval_minutes)))
    return value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def _helper_command(request_path: Path, result_path: Path) -> list[str]:
    args = ["--task-helper", str(request_path), str(result_path)]
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), *args]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve().parents[1] / "app.py"), *args]


def _needs_elevation(error: str | None) -> bool:
    value = str(error or "").casefold()
    return any(token in value for token in ("access is denied", "access denied", "0x80070005", "requires elevation"))


def _is_missing(error: str | None) -> bool:
    value = str(error or "").casefold()
    return any(token in value for token in ("cannot find", "does not exist", "not found"))


def _ps(value: str) -> str:
    return str(value).replace("'", "''")


def _interactive_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    username = os.environ.get("USERNAME", "").strip() or os.environ.get("USER", "").strip()
    return f"{domain}\\{username}" if domain and username else username
