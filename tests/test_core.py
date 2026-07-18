from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_timing_optimization() -> None:
    from rpa.timing import optimize_delay, runtime_delay

    assert optimize_delay(0.05) == 0
    assert optimize_delay(0.2) == 0.2
    assert optimize_delay(1.4) == 0.3
    assert optimize_delay(3.0) == 0
    assert runtime_delay(1.2, "recorded") == 1.2
    assert runtime_delay(1.2, "optimized") == 1.2
    assert runtime_delay(1.2, "none") == 0


def test_text_buffer_groups_and_flushes_after_timeout() -> None:
    from rpa.recorder import TextBuffer

    buffer = TextBuffer(flush_timeout=0.7)
    assert buffer.add("H", now=1.0) is None
    assert buffer.add("i", now=1.2) is None
    assert buffer.add("!", now=2.1) == "Hi"
    assert buffer.flush() == "!"


def test_hotkey_detection_helpers() -> None:
    from rpa.recorder import normalize_modifier

    assert normalize_modifier("ctrl_l") == "ctrl"
    assert normalize_modifier("alt_r") == "alt"


def test_key_mapping_simple_object() -> None:
    from rpa.recorder import normalize_key

    key = type("KeyObj", (), {"name": "page_down"})()
    assert normalize_key(key) == "pagedown"
    char = type("CharObj", (), {"char": "s"})()
    assert normalize_key(char) == "s"


def test_double_click_interval_setting_present() -> None:
    from rpa.models import ProjectSettings

    assert ProjectSettings().double_click_interval > 0
    assert ProjectSettings().pre_click_pause == 0.10
    assert ProjectSettings().show_desktop_before_recording is True


def test_project_save_load(tmp_path: Path) -> None:
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.project_manager import ProjectManager

    manager = ProjectManager()
    project = RpaProject()
    project.actions.append(RpaAction(ActionType.WAIT.value, {"seconds": 1}))
    manager.save(project, tmp_path)
    loaded = manager.load(tmp_path / "project.json")
    assert loaded.actions[0].action == ActionType.WAIT.value
    assert (tmp_path / "screenshots").exists()
    assert (tmp_path / "generated").exists()
    assert (tmp_path / "logs").exists()


def test_placeholder_resolution() -> None:
    from rpa.utils import resolve_placeholders

    data = {"path": "{{INPUT_FILE}}", "items": ["{{PROJECT_NAME}}"]}
    assert resolve_placeholders(data, {"INPUT_FILE": "D:/x.txt", "PROJECT_NAME": "Demo"}) == {"path": "D:/x.txt", "items": ["Demo"]}


def test_python_generation(tmp_path: Path) -> None:
    from rpa.generator import generate_python
    from rpa.models import ActionType, RpaAction, RpaProject

    project = RpaProject()
    project.settings.start_delay = 3.126
    project.variables["PROJECT_NAME"] = "Demo"
    project.actions = [
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "Hello {{PROJECT_NAME}}", "interval": 0.01}, delay_before=1.236),
        RpaAction(ActionType.HOTKEY.value, {"keys": ["ctrl", "s"]}),
    ]
    path = generate_python(project, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "def main():" in text
    assert "pyautogui.write" in text
    assert "pyautogui.hotkey('ctrl', 's')" in text
    assert "project.json" not in text
    assert "time.sleep(3.13)" in text
    assert "time.sleep(1.24)" in text
    assert "PRE_CLICK_PAUSE = 0.10" in text
    assert "run_generated.ps1" in text
    assert (tmp_path / "generated" / "requirements.txt").exists()
    assert (tmp_path / "generated" / "run_generated.ps1").exists()
    run_script = (tmp_path / "generated" / "run_generated.ps1").read_text(encoding="utf-8")
    assert "PythonRPARecorder.exe" in run_script
    assert "--run-generated generated_rpa.py" in run_script
    assert ".venv\\Scripts\\python.exe" in run_script
    assert "pip install" not in run_script


def test_generated_runner_uses_packaged_exe_relative_path(tmp_path: Path, monkeypatch) -> None:
    from rpa.generator import generate_python
    from rpa.models import ActionType, RpaAction, RpaProject
    import rpa.generator as generator

    app_root = tmp_path / "PythonRPARecorder"
    flow_dir = app_root / "flows" / "Test1"
    exe = app_root / "PythonRPARecorder.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(generator, "_app_root", lambda: app_root)
    monkeypatch.setattr(generator.sys, "frozen", True, raising=False)
    monkeypatch.setattr(generator.sys, "executable", str(exe), raising=False)

    project = RpaProject()
    project.actions = [RpaAction(ActionType.TYPE_TEXT.value, {"text": "hello", "interval": 0})]
    generate_python(project, flow_dir)

    run_script = (flow_dir / "generated" / "run_generated.ps1").read_text(encoding="utf-8")
    assert "..\\..\\..\\PythonRPARecorder.exe" in run_script
    assert "--run-generated generated_rpa.py" in run_script


def test_action_ordering() -> None:
    from rpa.models import ActionType, RpaAction

    actions = [RpaAction(ActionType.WAIT.value, {"seconds": 1}), RpaAction(ActionType.TYPE_TEXT.value, {"text": "x"})]
    actions[0], actions[1] = actions[1], actions[0]
    assert actions[0].action == ActionType.TYPE_TEXT.value


def test_coordinate_fallback(monkeypatch, tmp_path: Path) -> None:
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.runner import ReplayRunner

    project = RpaProject()
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/missing.png",
        "fallback_x": 10,
        "fallback_y": 20,
        "button": "left",
        "click_offset_x": 0,
        "click_offset_y": 0,
        "confidence": 0.99,
        "timeout": 0,
        "use_coordinate_fallback": True,
    })
    clicked = {}
    monkeypatch.setattr("rpa.runner.wait_for_image", lambda *args, **kwargs: type("M", (), {"found": False})())
    monkeypatch.setattr("rpa.runner.pyautogui", SimpleNamespace(click=lambda x, y, button="left", clicks=1: clicked.update(x=x, y=y, button=button)))
    ReplayRunner(project, tmp_path, lambda m: None).run_action(action, {})
    assert clicked == {"x": 10, "y": 20, "button": "left"}


def test_image_not_found_without_fallback(monkeypatch, tmp_path: Path) -> None:
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.runner import ReplayRunner

    project = RpaProject()
    action = RpaAction(ActionType.CLICK_IMAGE.value, {"image": "missing.png", "use_coordinate_fallback": False, "timeout": 0})
    monkeypatch.setattr("rpa.runner.wait_for_image", lambda *args, **kwargs: type("M", (), {"found": False})())
    with pytest.raises(FileNotFoundError):
        ReplayRunner(project, tmp_path, lambda m: None).run_action(action, {})


def test_run_skips_fixed_delay_for_click_image_steps(monkeypatch, tmp_path: Path) -> None:
    from rpa.models import ActionType, RpaAction, RpaProject
    from rpa.runner import ReplayActionError, ReplayRunner

    project = RpaProject()
    click_action = RpaAction(ActionType.CLICK_IMAGE.value, {"image": "missing.png", "use_coordinate_fallback": False, "timeout": 0}, delay_before=5.0)
    wait_action = RpaAction(ActionType.WAIT.value, {"seconds": 0}, delay_before=2.0)
    project.actions = [wait_action, click_action]
    monkeypatch.setattr("rpa.runner.wait_for_image", lambda *args, **kwargs: type("M", (), {"found": False})())
    monkeypatch.setattr("rpa.runner.foreground_elevation_mismatch", lambda: None)
    monkeypatch.setattr("rpa.runner.pyautogui", SimpleNamespace(click=lambda *a, **k: None, FAILSAFE=None))
    runner = ReplayRunner(project, tmp_path, lambda m: None)
    sleeps: list[float] = []
    monkeypatch.setattr(runner, "sleep_checked", lambda seconds: sleeps.append(seconds))
    with pytest.raises(ReplayActionError):
        runner.run(include_start_delay=False)
    # Only the WAIT step's delay_before (2.0) should be slept; the click image
    # step's delay_before (5.0) is skipped in favor of continuous polling.
    assert 5.0 not in sleeps
    assert 2.0 in sleeps


def test_pause_resume_state_without_hooks(tmp_path: Path) -> None:
    from rpa.recorder import RpaRecorder
    from rpa.models import ProjectSettings, RecorderState

    recorder = RpaRecorder(tmp_path, ProjectSettings(), lambda a: None, lambda m: None)
    recorder.state = RecorderState.RECORDING
    recorder.pause()
    assert recorder.state == RecorderState.PAUSED
    recorder.resume()
    assert recorder.state == RecorderState.RECORDING


def test_new_recorder_continues_screenshot_numbering_from_existing_files(tmp_path: Path) -> None:
    from rpa.recorder import RpaRecorder
    from rpa.models import ProjectSettings

    screenshots_dir = tmp_path / "screenshots"
    screenshots_dir.mkdir()
    (screenshots_dir / "click_0001.png").write_bytes(b"fake")
    (screenshots_dir / "click_0012.png").write_bytes(b"fake")
    (screenshots_dir / "click_0007.png").write_bytes(b"fake")

    recorder = RpaRecorder(tmp_path, ProjectSettings(), lambda a: None, lambda m: None)
    assert recorder._screenshot_index == 12


def test_new_recorder_starts_at_zero_when_no_screenshots_exist(tmp_path: Path) -> None:
    from rpa.recorder import RpaRecorder
    from rpa.models import ProjectSettings

    recorder = RpaRecorder(tmp_path, ProjectSettings(), lambda a: None, lambda m: None)
    assert recorder._screenshot_index == 0


def test_stop_replay_state() -> None:
    from rpa.models import RpaProject
    from rpa.runner import ReplayRunner

    runner = ReplayRunner(RpaProject(), Path("."), lambda m: None)
    runner.request_stop()
    assert runner.stop_requested()
