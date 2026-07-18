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
    return MainWindow()


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
    monkeypatch.setattr(main_window_module, "validate_project", lambda project, project_dir: [])

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
