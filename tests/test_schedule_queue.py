from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

import ui.main_window as main_window_module
from ui.main_window import MainWindow


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_window(tmp_path: Path, monkeypatch) -> MainWindow:
    app()
    monkeypatch.setattr(main_window_module, "flows_root", lambda: tmp_path)
    window = MainWindow()
    monkeypatch.setattr(window, "_show_windows_desktop", lambda: None)
    return window


def _make_flow(tmp_path: Path, name: str) -> None:
    flow_dir = tmp_path / name
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "project.json").write_text("{}", encoding="utf-8")


def test_scheduled_run_is_queued_when_a_flow_is_already_running(tmp_path, monkeypatch) -> None:
    window = make_window(tmp_path, monkeypatch)
    _make_flow(tmp_path, "flow_a")
    _make_flow(tmp_path, "flow_b")

    # Simulate flow_a already running.
    fake_thread = QThread()
    window._scheduled_runs["flow_a"] = (fake_thread, object())

    window._run_flow_now("flow_b", scheduled=True)

    assert "flow_b" in window._schedule_queue
    assert "flow_b" not in window._scheduled_runs

    window._scheduled_runs.pop("flow_a", None)
    window.close()


def test_scheduled_run_does_not_duplicate_queue_entries(tmp_path, monkeypatch) -> None:
    window = make_window(tmp_path, monkeypatch)
    _make_flow(tmp_path, "flow_a")
    _make_flow(tmp_path, "flow_b")

    fake_thread = QThread()
    window._scheduled_runs["flow_a"] = (fake_thread, object())

    window._run_flow_now("flow_b", scheduled=True)
    window._run_flow_now("flow_b", scheduled=True)

    assert window._schedule_queue == ["flow_b"]

    window._scheduled_runs.pop("flow_a", None)
    window.close()


def test_finished_run_starts_next_queued_flow(tmp_path, monkeypatch) -> None:
    window = make_window(tmp_path, monkeypatch)
    _make_flow(tmp_path, "flow_a")
    _make_flow(tmp_path, "flow_b")

    started: list[str] = []
    monkeypatch.setattr(window, "_run_flow_now", lambda name, scheduled=False: started.append(name))

    window._schedule_queue = ["flow_b"]
    window._scheduled_run_finished("flow_a", "success")

    assert started == ["flow_b"]

    window.close()


def test_full_scheduled_run_completes_without_thread_self_wait(tmp_path, monkeypatch) -> None:
    """End-to-end regression check: a scheduled run must finish and clean up its
    QThread without the 'QThread::wait: Thread tried to wait on itself' crash."""
    from PySide6.QtCore import QObject, Signal

    window = make_window(tmp_path, monkeypatch)
    _make_flow(tmp_path, "flow_a")

    class FakeWorker(QObject):
        action_status = Signal(int, str)
        log = Signal(str)
        finished = Signal()
        failed = Signal(int, str)
        stopped = Signal()

        def __init__(self, *args, **kwargs) -> None:
            super().__init__()

        def run(self) -> None:
            self.log.emit("hello")
            self.finished.emit()

    class FakeProject:
        actions: list = []

    class FakeProjectManager:
        def load(self, _path):
            return FakeProject()

    monkeypatch.setattr(main_window_module, "ReplayWorker", FakeWorker)
    monkeypatch.setattr(main_window_module, "ProjectManager", FakeProjectManager)
    monkeypatch.setattr(main_window_module, "validate_project_detailed", lambda project, project_dir: [])

    window._run_flow_now("flow_a", scheduled=True)

    for _ in range(200):
        app().processEvents()
        if "flow_a" not in window._scheduled_runs:
            break
        QThread.msleep(5)

    assert "flow_a" not in window._scheduled_runs
    from rpa.scheduler import STATUS_SUCCESS
    assert window.schedule_store.get("flow_a").last_status == STATUS_SUCCESS
    window.close()


def test_scheduled_worker_signal_handlers_are_bound_methods(tmp_path, monkeypatch) -> None:
    """Regression test: connecting cross-thread worker signals to lambdas breaks Qt's
    automatic queued-connection detection and previously crashed with
    'QThread::wait: Thread tried to wait on itself'. Bound methods must be used instead,
    so Qt can detect the receiver's (main) thread and queue the call safely."""
    window = make_window(tmp_path, monkeypatch)
    assert window._scheduled_run_success.__self__ is window
    assert window._scheduled_run_stopped.__self__ is window
    assert window._scheduled_run_failed.__self__ is window
    assert window._scheduled_run_log.__self__ is window
    window.close()


def test_failed_scheduled_run_persists_one_based_failed_step(tmp_path, monkeypatch) -> None:
    from PySide6.QtCore import QObject, Signal

    window = make_window(tmp_path, monkeypatch)
    _make_flow(tmp_path, "flow_a")

    class FailingWorker(QObject):
        action_status = Signal(int, str)
        log = Signal(str)
        finished = Signal()
        failed = Signal(int, str)
        stopped = Signal()

        def __init__(self, *args, **kwargs) -> None:
            super().__init__()

        def run(self) -> None:
            self.failed.emit(2, "image not found")

    class FakeProject:
        actions = [object(), object(), object()]

    class FakeProjectManager:
        def load(self, _path):
            return FakeProject()

    monkeypatch.setattr(main_window_module, "ReplayWorker", FailingWorker)
    monkeypatch.setattr(main_window_module, "ProjectManager", FakeProjectManager)
    monkeypatch.setattr(main_window_module, "validate_project_detailed", lambda project, project_dir: [])
    window._run_flow_now("flow_a", scheduled=True)
    for _ in range(200):
        app().processEvents()
        if "flow_a" not in window._scheduled_runs:
            break
        QThread.msleep(5)

    history = window.schedule_store.get("flow_a").history
    assert history[-1].failed_step == 3
    assert history[-1].error == "image not found"
    window.close()


def test_scheduled_execution_is_blocked_by_validation_errors(tmp_path, monkeypatch) -> None:
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.project_manager import ProjectManager
    from rpa.scheduler import STATUS_FAILED

    window = make_window(tmp_path, monkeypatch)
    flow_dir = tmp_path / "invalid_flow"
    project = RpaProject(actions=[RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{missing}}"})])
    ProjectManager().save(project, flow_dir)
    prepared: list[str] = []
    monkeypatch.setattr(window, "_prepare_run_environment", lambda settings, label: prepared.append(label))

    window._run_flow_now("invalid_flow", scheduled=True)
    assert "invalid_flow" not in window._scheduled_runs
    schedule = window.schedule_store.get("invalid_flow")
    assert schedule.last_status == STATUS_FAILED
    assert schedule.history[-1].failed_step == 1
    assert schedule.history[-1].attempts == 0
    assert "undefined variable" in (schedule.history[-1].error or "")
    assert prepared == []
    window.close()


def test_real_scheduled_run_retries_and_records_success_attempts(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    import rpa.runner as runner_module
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.project_manager import ProjectManager
    from rpa.scheduler import STATUS_SUCCESS

    window = make_window(tmp_path, monkeypatch)
    flow_dir = tmp_path / "retry_flow"
    project = RpaProject(actions=[RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "variables['tries'] = variables.get('tries', 0) + 1\nif variables['tries'] < 2: raise RuntimeError('again')",
        "retry_count": 1,
        "retry_delay": 0,
    })])
    project.settings.start_delay = 0
    ProjectManager().save(project, flow_dir)
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    lifecycle: list[str] = []
    monkeypatch.setattr(window, "_prepare_run_environment", lambda settings, label: lifecycle.append("prepare"))
    monkeypatch.setattr(window, "_restore_run_environment", lambda: lifecycle.append("restore"))

    window._run_flow_now("retry_flow", scheduled=True)
    for _ in range(300):
        app().processEvents()
        if "retry_flow" not in window._scheduled_runs:
            break
        QThread.msleep(5)
    entry = window.schedule_store.get("retry_flow").history[-1]
    assert entry.status == STATUS_SUCCESS
    assert entry.attempts == 2
    assert lifecycle == ["prepare", "restore"]
    window.close()


def test_real_scheduled_run_failure_records_step_error_and_cleanup(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    import rpa.runner as runner_module
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.project_manager import ProjectManager
    from rpa.scheduler import STATUS_FAILED

    window = make_window(tmp_path, monkeypatch)
    flow_dir = tmp_path / "failed_flow"
    project = RpaProject(actions=[RpaAction(ActionType.PYTHON_CODE.value, {"code": "raise RuntimeError('boom')"})])
    project.settings.start_delay = 0
    ProjectManager().save(project, flow_dir)
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    lifecycle: list[str] = []
    monkeypatch.setattr(window, "_prepare_run_environment", lambda settings, label: lifecycle.append("prepare"))
    monkeypatch.setattr(window, "_restore_run_environment", lambda: lifecycle.append("restore"))

    window._run_flow_now("failed_flow", scheduled=True)
    for _ in range(300):
        app().processEvents()
        if "failed_flow" not in window._scheduled_runs:
            break
        QThread.msleep(5)
    entry = window.schedule_store.get("failed_flow").history[-1]
    assert entry.status == STATUS_FAILED
    assert entry.failed_step == 1
    assert entry.attempts == 1
    assert "boom" in (entry.error or "")
    assert lifecycle == ["prepare", "restore"]
    window.close()


def test_real_scheduled_run_stop_interrupts_wait_and_restores(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    import rpa.runner as runner_module
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.project_manager import ProjectManager
    from rpa.scheduler import STATUS_STOPPED

    window = make_window(tmp_path, monkeypatch)
    flow_dir = tmp_path / "stopped_flow"
    project = RpaProject(actions=[RpaAction(ActionType.WAIT.value, {"seconds": 5})])
    project.settings.start_delay = 0
    ProjectManager().save(project, flow_dir)
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    lifecycle: list[str] = []
    monkeypatch.setattr(window, "_prepare_run_environment", lambda settings, label: lifecycle.append("prepare"))
    monkeypatch.setattr(window, "_restore_run_environment", lambda: lifecycle.append("restore"))

    window._run_flow_now("stopped_flow", scheduled=True)
    for _ in range(100):
        app().processEvents()
        entry = window._scheduled_runs.get("stopped_flow")
        if entry and entry[1].runner.current_index == 0:
            break
        QThread.msleep(5)
    window.stop_run()
    for _ in range(200):
        app().processEvents()
        if "stopped_flow" not in window._scheduled_runs:
            break
        QThread.msleep(5)
    entry = window.schedule_store.get("stopped_flow").history[-1]
    assert entry.status == STATUS_STOPPED
    assert entry.failed_step == 1
    assert entry.attempts == 1
    assert lifecycle == ["prepare", "restore"]
    window.close()
