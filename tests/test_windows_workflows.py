from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import numpy as np
from PIL import Image
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from rpa.generator import generate_python
from rpa.image_matcher import crop_image_around
from rpa.models import ActionType, ProjectSettings, RecorderState, RpaAction, RpaProject
from rpa.recorder import RpaRecorder
from rpa.runner import ReplayActionError, ReplayRunner, StopReplay
from ui.main_window import MainWindow
from ui.target_capture import TargetCaptureOverlay


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_multi_monitor_crop_preserves_screen_coordinates() -> None:
    desktop = Image.new("RGB", (3840, 1080), "white")
    crop, left, top = crop_image_around(desktop, -1900, 100, 180, 120, -1920, 0)
    assert crop.size == (180, 120)
    assert (left, top) == (-1920, 40)
    assert (-1900 - left, 100 - top) == (20, 60)


def test_image_match_excludes_recorder_preview(tmp_path: Path, monkeypatch) -> None:
    import rpa.image_matcher as matcher

    rng = np.random.default_rng(42)
    needle_array = rng.integers(0, 256, size=(20, 20, 3), dtype=np.uint8)
    real_target = needle_array.copy()
    real_target[0:3, 0:3] = 0
    screen_array = np.full((100, 300, 3), 255, dtype=np.uint8)
    screen_array[30:50, 20:40] = real_target
    screen_array[30:50, 220:240] = needle_array
    needle_path = tmp_path / "target.png"
    Image.fromarray(needle_array).save(needle_path)
    monkeypatch.setattr(matcher, "screenshot_image", lambda: Image.fromarray(screen_array))
    monkeypatch.setattr(matcher, "virtual_screen_origin", lambda: (0, 0))

    preview_match = matcher.find_image(needle_path, 0.8)
    real_match = matcher.find_image(needle_path, 0.8, [(200, 10, 80, 80)])
    assert preview_match.found and preview_match.x == 220
    assert real_match.found and real_match.x == 20


def test_target_capture_overlay_select_resize_and_confirm() -> None:
    app()
    overlay = TargetCaptureOverlay(Image.new("RGB", (800, 600), "white"), 180, 120)
    overlay.show()
    app().processEvents()
    spy = QSignalSpy(overlay.confirmed)
    QTest.mouseClick(overlay, Qt.LeftButton, pos=QPoint(300, 260))
    overlay.width_spin.setValue(220)
    overlay.height_spin.setValue(140)
    QTest.mouseClick(overlay.confirm_button, Qt.LeftButton)
    assert spy.count() == 1
    emitted = spy.at(0)
    assert emitted[2:] == [220, 140]


def test_completed_recapture_updates_action_and_screenshot(tmp_path: Path, monkeypatch) -> None:
    app()
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok)
    window = MainWindow()
    window.project_dir = tmp_path
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/target.png",
        "fallback_x": 1,
        "fallback_y": 2,
        "click_offset_x": 3,
        "click_offset_y": 4,
    })
    window.project.actions = [action]
    window.refresh()
    window.table.selectRow(0)
    overlay = TargetCaptureOverlay(Image.new("RGB", (400, 300), "white"), 120, 80)
    window.target_capture_overlay = overlay
    window.target_capture_action = action
    window.target_capture_origin = (0, 0)
    window._complete_target_capture(200, 150, 120, 80)
    assert (tmp_path / "screenshots" / "target.png").exists()
    assert action.data["fallback_x"] == 200
    assert action.data["fallback_y"] == 150
    assert action.data["click_offset_x"] == 60
    assert action.data["click_offset_y"] == 40
    assert action.data["crop_width"] == 120
    assert action.data["crop_height"] == 80
    assert window.dirty


def test_cancel_recording_discards_session_actions_and_images(tmp_path: Path) -> None:
    app()
    window = MainWindow()
    window.project_dir = tmp_path
    existing = RpaAction(ActionType.WAIT.value, {"seconds": 1})
    recorded = RpaAction(ActionType.CLICK_IMAGE.value, {"image": "screenshots/cancelled.png"})
    screenshot = tmp_path / "screenshots" / "cancelled.png"
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"temporary")
    window.project.actions = [existing, recorded]
    window.recording_start_action_count = 1
    window.recording_was_dirty = False
    window._discard_recording_session()
    assert window.project.actions == [existing]
    assert not screenshot.exists()
    assert not window.dirty


def test_global_listener_start_stop_lifecycle(tmp_path: Path, monkeypatch) -> None:
    import rpa.recorder as recorder_module

    listeners = []

    class FakeListener:
        def __init__(self, **callbacks):
            self.callbacks = callbacks
            self.started = False
            self.stopped = False
            self.joined = False
            listeners.append(self)

        def start(self):
            self.started = True

        def wait(self):
            return None

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            self.joined = True

    monkeypatch.setattr(recorder_module, "keyboard", SimpleNamespace(Listener=FakeListener))
    monkeypatch.setattr(recorder_module, "mouse", SimpleNamespace(Listener=FakeListener))
    recorder = RpaRecorder(tmp_path, ProjectSettings(), lambda action: None, lambda message: None)
    recorder.start()
    assert recorder.state == RecorderState.RECORDING
    assert all(listener.started for listener in listeners)
    recorder.pause()
    assert recorder.state == RecorderState.PAUSED
    recorder.resume()
    recorder.stop()
    assert recorder.state == RecorderState.COMPLETED
    assert all(listener.stopped and listener.joined for listener in listeners)


def test_recorder_ignores_current_process_foreground(tmp_path: Path, monkeypatch) -> None:
    import rpa.recorder as recorder_module

    actions: list[RpaAction] = []
    recorder = RpaRecorder(tmp_path, ProjectSettings(), actions.append, lambda message: None)
    recorder.state = RecorderState.RECORDING
    monkeypatch.setattr(recorder_module, "should_ignore_foreground", lambda process_id: True)
    recorder._on_press(SimpleNamespace(char="x"))
    recorder._on_scroll(10, 20, 0, -1)
    assert actions == []


def test_requested_replay_action_types(monkeypatch, tmp_path: Path) -> None:
    import rpa.runner as runner_module

    calls: list[tuple] = []
    gui = SimpleNamespace(
        FAILSAFE=True,
        write=lambda text, interval=0: calls.append(("write", text, interval)),
        press=lambda key, presses=1, interval=0: calls.append(("press", key, presses, interval)),
        hotkey=lambda *keys: calls.append(("hotkey", *keys)),
        moveTo=lambda x, y: calls.append(("move", x, y)),
        scroll=lambda amount: calls.append(("scroll", amount)),
    )
    monkeypatch.setattr(runner_module, "pyautogui", gui)
    runner = ReplayRunner(RpaProject(), tmp_path, lambda message: None)
    runner.run_action(RpaAction(ActionType.TYPE_TEXT.value, {"text": "Hello", "interval": 0.01}))
    runner.run_action(RpaAction(ActionType.PRESS_KEY.value, {"key": "enter", "count": 2, "interval": 0.1}))
    runner.run_action(RpaAction(ActionType.HOTKEY.value, {"keys": ["ctrl", "s"]}))
    runner.run_action(RpaAction(ActionType.SCROLL.value, {"amount": -3, "x": 20, "y": 30, "move_to": True}))
    assert calls == [
        ("write", "Hello", 0.01),
        ("press", "enter", 2, 0.1),
        ("hotkey", "ctrl", "s"),
        ("move", 20, 30),
        ("scroll", -3),
    ]


def test_type_text_clear_first(monkeypatch, tmp_path: Path) -> None:
    import rpa.runner as runner_module

    calls: list[tuple] = []
    gui = SimpleNamespace(
        hotkey=lambda *keys: calls.append(("hotkey", *keys)),
        press=lambda key, presses=1, interval=0: calls.append(("press", key)),
        write=lambda text, interval=0: calls.append(("write", text)),
    )
    monkeypatch.setattr(runner_module, "pyautogui", gui)
    action = RpaAction(ActionType.TYPE_TEXT.value, {"text": "Replacement", "clear_first": True})
    ReplayRunner(RpaProject(), tmp_path, lambda message: None).run_action(action)
    assert calls == [("hotkey", "ctrl", "a"), ("press", "backspace"), ("write", "Replacement")]


def test_stop_run_interrupts_checked_wait(tmp_path: Path) -> None:
    runner = ReplayRunner(RpaProject(), tmp_path, lambda message: None)
    runner.request_stop()
    with pytest.raises(StopReplay):
        runner.sleep_checked(10)


def test_pyautogui_failsafe_has_friendly_error(monkeypatch, tmp_path: Path) -> None:
    import rpa.runner as runner_module

    class FailSafeException(Exception):
        pass

    def fail_write(*args, **kwargs):
        raise FailSafeException("corner")

    monkeypatch.setattr(runner_module, "foreground_elevation_mismatch", lambda: None)
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True, write=fail_write))
    project = RpaProject()
    project.settings.start_delay = 0
    project.actions = [RpaAction(ActionType.TYPE_TEXT.value, {"text": "Hello"})]
    with pytest.raises(ReplayActionError, match="safety stop"):
        ReplayRunner(project, tmp_path, lambda message: None).run()


def test_replay_reports_elevation_mismatch(monkeypatch, tmp_path: Path) -> None:
    import rpa.runner as runner_module

    monkeypatch.setattr(runner_module, "foreground_elevation_mismatch", lambda: (123, "Run both applications at the same permission level."))
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    project = RpaProject()
    project.settings.start_delay = 0
    with pytest.raises(PermissionError, match="same permission level"):
        ReplayRunner(project, tmp_path, lambda message: None).run()


def test_generated_python_windows_compatibility(tmp_path: Path) -> None:
    project = RpaProject()
    project.settings.pyautogui_failsafe = False
    project.actions = [RpaAction(ActionType.TYPE_TEXT.value, {"text": "Hello"})]
    text = generate_python(project, tmp_path).read_text(encoding="utf-8")
    compile(text, "generated_rpa.py", "exec")
    assert "pyautogui.FAILSAFE = False" in text
    assert "all_screens=os.name == 'nt'" in text
    assert "virtual_screen_origin" in text
    assert text.index("        main()") < text.index("Flow completed")
    assert "except pyautogui.FailSafeException" in text
