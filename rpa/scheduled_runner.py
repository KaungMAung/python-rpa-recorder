"""Standalone scheduled execution invoked by Windows Task Scheduler."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .evidence import RunEvidenceSession
from .desktop_lifecycle import (
    recorder_window_handles, restore_recorder_windows, show_windows_desktop,
)
from .models import RpaProject
from .project_manager import ProjectManager
from .runner import ReplayActionError, ReplayRunner, StopReplay
from .scheduler import (
    STATUS_FAILED, STATUS_STOPPED, STATUS_SUCCESS, ScheduleStore,
    mark_finished, mark_started,
)
from .validator import LEVEL_ERROR, validate_project_detailed
from .variables import (
    mask_sensitive_text, prepare_runtime_variables, sensitive_variable_names,
    validate_variable_configuration,
)
from ui.recorder_toolbar import FloatingExecutionToolbar


class ScheduledRunController(QObject):
    completed = Signal(object)
    action_progress = Signal(int, str)

    def __init__(
        self, app: QApplication, project_json: Path, schedule_id: str,
    ) -> None:
        super().__init__()
        self.app = app
        self.project_json = Path(project_json).resolve()
        self.flow_dir = self.project_json.parent
        self.store = ScheduleStore(self.flow_dir.parent)
        self.schedule = self.store.get_by_id(schedule_id)
        self.project: RpaProject | None = None
        self.runner: ReplayRunner | None = None
        self.evidence: RunEvidenceSession | None = None
        self.toolbar: FloatingExecutionToolbar | None = None
        self.secret_values: set[str] = set()
        self._timeout_timer: threading.Timer | None = None
        self._recorder_windows: list[int] = []
        self.exit_code = 1
        self.completed.connect(self._finish)
        self.action_progress.connect(self._update_toolbar)

    def start(self) -> None:
        error = self._prepare()
        if error:
            self._finish({"status": STATUS_FAILED, "error": error, "failed_step": None, "attempts": 0, "steps": []})
            return
        if self.project is not None and self.project.settings.hide_window_during_replay:
            try:
                self._prepare_desktop()
            except Exception as exc:
                self._finish({
                    "status": STATUS_FAILED,
                    "error": f"Could not prepare the Windows desktop: {exc}",
                    "failed_step": None,
                    "attempts": 0,
                    "steps": [],
                })
                return
        self.toolbar = FloatingExecutionToolbar()
        self.toolbar.stop_requested.connect(self.stop)
        self.toolbar.set_status(f"Scheduled: {self.schedule.flow_name}")
        self.toolbar.show()
        self._position_toolbar()
        self.toolbar.position_changed.connect(self._toolbar_moved)
        QTimer.singleShot(300, self._start_thread)

    def _prepare(self) -> str | None:
        if not self.project_json.is_file():
            return f"Project file does not exist: {self.project_json}"
        if self.schedule is None:
            return "The requested schedule ID does not exist in schedules.json."
        try:
            self.project = ProjectManager().load(self.project_json)
        except Exception as exc:
            return f"Could not load project: {exc}"
        try:
            self.evidence = RunEvidenceSession(
                self.flow_dir, self.project.project.name or self.schedule.flow_name,
                "Scheduled", self.project.settings.evidence_retention_runs,
            )
        except OSError as exc:
            return f"Could not create run evidence: {exc}"
        sensitive = sensitive_variable_names(self.project)
        runtime_variables, input_errors = prepare_runtime_variables(
            self.project, self.schedule.runtime_inputs,
            clipboard_text=self.app.clipboard().text(),
        )
        self.secret_values = {
            str(runtime_variables[name]) for name in sensitive
            if name in runtime_variables and runtime_variables[name] not in (None, "")
        }
        self.evidence.set_runtime_inputs(self.schedule.runtime_inputs, sensitive)
        configuration_errors = validate_variable_configuration(self.project)
        issues = validate_project_detailed(
            self.project, self.flow_dir, runtime_variables=runtime_variables,
        )
        self.evidence.set_validation(issues)
        all_errors = [*configuration_errors, *[issue.message() for issue in issues if issue.level == LEVEL_ERROR]]
        mark_started(
            self.schedule, source="Scheduled", evidence_path=self.evidence.relative_folder,
            run_id=self.evidence.run_id,
        )
        self.store.set(self.schedule)
        self.store.save()
        if all_errors:
            return all_errors[0]
        self.runner = ReplayRunner(
            self.project, self.flow_dir, self._log,
            evidence_dir=self.evidence.folder,
        )
        self.runner.runtime_variables = runtime_variables
        return None

    def _start_thread(self) -> None:
        if self.runner is None or self.project is None:
            return
        timeout = self.schedule.execution_timeout_minutes if self.schedule else None
        if timeout:
            self._timeout_timer = threading.Timer(timeout * 60, self._timeout)
            self._timeout_timer.daemon = True
            self._timeout_timer.start()
        threading.Thread(target=self._run, name="scheduled-rpa-run", daemon=True).start()

    def _run(self) -> None:
        status, error, failed_step = STATUS_SUCCESS, None, None
        try:
            self.runner.run(
                action_callback=self._action_status,
                include_start_delay=True,
                enable_debug=False,
            )
            if self.runner.had_continued_failures:
                status = STATUS_FAILED
                failed_step = (self.runner.first_failed_index + 1) if self.runner.first_failed_index is not None else None
                error = self.runner.first_failure_error
        except StopReplay:
            status, error = STATUS_STOPPED, "Stopped by user or execution timeout"
            failed_step = (self.runner.current_index + 1) if self.runner.current_index is not None else None
        except ReplayActionError as exc:
            status, error, failed_step = STATUS_FAILED, str(exc), exc.index + 1
        except Exception as exc:
            status, error = STATUS_FAILED, str(exc)
        self.completed.emit({
            "status": status, "error": error, "failed_step": failed_step,
            "attempts": self.runner.total_attempts,
            "steps": self.runner.step_results,
        })

    def stop(self) -> None:
        if self.runner:
            self.runner.request_stop()
            self._log("stop scheduled run requested")

    def _timeout(self) -> None:
        if self.runner:
            self._log("scheduled execution timeout reached; stopping")
            self.runner.request_stop()

    def _action_status(self, index: int, status: str) -> None:
        self.action_progress.emit(index, status)

    def _update_toolbar(self, index: int, status: str) -> None:
        if self.toolbar:
            self.toolbar.set_status(f"Scheduled: {self.schedule.flow_name} | Step {index + 1} | {status.title()}")

    def _finish(self, result: dict[str, Any]) -> None:
        if self._timeout_timer:
            self._timeout_timer.cancel()
            self._timeout_timer = None
        status = str(result.get("status") or STATUS_FAILED)
        error = str(result.get("error") or "") or None
        failed_step = result.get("failed_step")
        attempts = int(result.get("attempts") or 0)
        safe_error = mask_sensitive_text(error, self.secret_values) if error else None
        if self.schedule is not None:
            # A different schedule for the same flow may have completed while
            # this process was running. Reload before updating our own record
            # so its history and any UI edits are not overwritten by a stale copy.
            schedule_id = self.schedule.schedule_id
            self.store.load()
            latest = self.store.get_by_id(schedule_id)
            if latest is not None:
                self.schedule = latest
            mark_finished(
                self.schedule, status, error=safe_error,
                failed_step=failed_step, attempts=attempts,
            )
            self.store.set(self.schedule)
            self.store.save()
        if (
            status == STATUS_SUCCESS and self.project is not None
            and self.project.settings.persist_variable_values
        ):
            try:
                ProjectManager().save(self.project, self.flow_dir)
            except (OSError, TypeError, ValueError) as exc:
                self._log(f"Variable persistence warning: {exc}")
        if self.toolbar:
            self.toolbar.close()
            self.toolbar = None
        self._restore_desktop()
        if self.evidence is not None:
            try:
                self.evidence.finalize(
                    status, _mask(result.get("steps") or [], self.secret_values),
                    attempts, failed_step, safe_error,
                )
            except Exception:
                self.evidence.close()
        self.exit_code = 0 if status == STATUS_SUCCESS else 2
        self.app.exit(self.exit_code)

    def _log(self, message: str) -> None:
        safe = mask_sensitive_text(str(message), self.secret_values)
        if self.evidence:
            self.evidence.logger.info(safe)

    def _prepare_desktop(self) -> None:
        self._log("scheduled desktop preparation started")
        self._recorder_windows = recorder_window_handles()
        minimized = show_windows_desktop()
        self._log(
            f"scheduled desktop prepared: minimized {minimized} window(s); "
            f"recorder windows to restore: {len(self._recorder_windows)}"
        )

    def _restore_desktop(self) -> None:
        handles = self._recorder_windows
        self._recorder_windows = []
        try:
            restored = restore_recorder_windows(handles)
        except Exception as exc:
            self._log(f"scheduled cleanup could not restore the recorder window: {exc}")
            return
        self._log(f"scheduled cleanup restored {restored} recorder window(s)")

    def _position_toolbar(self) -> None:
        if not self.toolbar:
            return
        settings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        self.toolbar.adjustSize()
        saved = settings.value("execution_toolbar_position")
        if isinstance(saved, QPoint):
            screen = QApplication.screenAt(QRect(saved, self.toolbar.size()).center())
            if screen and screen.availableGeometry().contains(QRect(saved, self.toolbar.size())):
                self.toolbar.move(saved)
                return
        screen = QApplication.primaryScreen()
        if screen:
            bounds = screen.availableGeometry()
            self.toolbar.move(
                bounds.right() - self.toolbar.width() - 31,
                bounds.bottom() - self.toolbar.height() - 31,
            )

    def _toolbar_moved(self, position: QPoint) -> None:
        QSettings("PythonRPARecorder", "PythonRPARecorder").setValue(
            "execution_toolbar_position", position,
        )


def scheduled_run_main(app: QApplication, project_json: Path, schedule_id: str) -> tuple[ScheduledRunController, int]:
    controller = ScheduledRunController(app, project_json, schedule_id)
    QTimer.singleShot(0, controller.start)
    return controller, app.exec()


def _mask(value: Any, secrets: set[str]) -> Any:
    if isinstance(value, str):
        return mask_sensitive_text(value, secrets)
    if isinstance(value, list):
        return [_mask(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _mask(item, secrets) for key, item in value.items()}
    return value
