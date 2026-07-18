from __future__ import annotations

import os
import py_compile
from pathlib import Path
from types import SimpleNamespace

import pytest

from rpa.generator import generate_python
from rpa.models import ActionType, ProjectSettings, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.runner import ReplayRunner
from rpa.validator import validate_project_detailed
from rpa.windowing import (
    AmbiguousWindowError, WindowInfo, WindowNotFoundError, WindowResolver,
    match_windows,
)


WINDOWS = [
    WindowInfo(1, "Quarterly Report - Excel", "EXCEL.EXE", "XLMAIN", 10, -1200, 100, 900, 700),
    WindowInfo(2, "Notes", "notepad.exe", "Notepad", 11, 100, 80, 700, 500),
    WindowInfo(3, "Notes - Copy", "notepad.exe", "Notepad", 12, 850, 80, 700, 500),
]


class FakeBackend:
    def __init__(self, windows=None, active=1) -> None:
        self.windows = list(WINDOWS if windows is None else windows)
        self.active = active

    def enumerate(self):
        return list(self.windows)

    def foreground_handle(self):
        return self.active


class FakeResolver:
    def __init__(self, window: WindowInfo, error: Exception | None = None) -> None:
        self.window = window
        self.error = error
        self.targets = []
        self.operations = []

    def resolve(self, target, _stop=None):
        self.targets.append(dict(target))
        if self.error:
            raise self.error
        return self.window

    def activate(self, window):
        self.operations.append("activate")
        return self.window

    def change_state(self, window, state):
        self.operations.append(state)
        return self.window

    def close(self, window):
        self.operations.append("close")


def target(**overrides):
    value = {
        "process_name": "notepad.exe", "window_title": "Notes", "title_match": "contains",
        "class_name": "Notepad", "timeout": 0, "retry_interval": 0.05,
        "multiple_match": "error",
    }
    value.update(overrides)
    return value


def test_window_matching_supports_process_title_modes_and_class() -> None:
    assert [item.handle for item in match_windows(WINDOWS, target(window_title="Notes", title_match="exact"))] == [2]
    assert [item.handle for item in match_windows(WINDOWS, target(window_title="note", title_match="contains"))] == [2, 3]
    assert [item.handle for item in match_windows(WINDOWS, target(window_title=r"Notes( - Copy)?$", title_match="regex"))] == [2, 3]
    assert [item.handle for item in match_windows(WINDOWS, target(process_name="excel", window_title="Report", class_name="XLMAIN"))] == [1]


def test_resolver_reports_ambiguous_missing_and_active_match() -> None:
    backend = FakeBackend(active=3)
    resolver = WindowResolver(backend=backend, sleep=lambda _seconds: None)
    with pytest.raises(AmbiguousWindowError, match="2 windows matched"):
        resolver.resolve(target(window_title="Notes", multiple_match="error"))
    assert resolver.resolve(target(window_title="Notes", multiple_match="first")).handle == 2
    assert resolver.resolve(target(window_title="Notes", multiple_match="active")).handle == 3
    with pytest.raises(WindowNotFoundError, match="window not found"):
        resolver.resolve(target(window_title="Missing"))


def test_runner_selects_window_and_scales_relative_click(monkeypatch, tmp_path: Path) -> None:
    selected = WindowInfo(20, "Invoice", "billing.exe", "Billing", 20, -1000, 200, 1000, 800)
    resolver = FakeResolver(selected)
    clicked = {}
    monkeypatch.setattr("rpa.runner.foreground_elevation_mismatch", lambda: None)
    monkeypatch.setattr(
        "rpa.runner.pyautogui",
        SimpleNamespace(FAILSAFE=True, click=lambda x, y, button="left": clicked.update(x=x, y=y, button=button)),
    )
    project = RpaProject(actions=[
        RpaAction(ActionType.SELECT_WINDOW.value, {"window": target(process_name="billing.exe", window_title="Invoice")}),
        RpaAction(ActionType.CLICK_WINDOW_RELATIVE.value, {
            "use_selected_window": True, "window": {"timeout": 0, "retry_interval": 0.05, "multiple_match": "error"},
            "relative_x": 250, "relative_y": 200, "scale_with_window": True,
            "original_window_width": 500, "original_window_height": 400,
            "button": "left", "use_absolute_fallback": False,
        }),
    ])
    runner = ReplayRunner(project, tmp_path, lambda _message: None)
    runner.window_resolver = resolver
    runner.run(include_start_delay=False)
    assert clicked == {"x": -500, "y": 600, "button": "left"}
    assert resolver.operations == ["activate"]
    assert runner.step_results[-1]["window_result"]["operation"].startswith("clicked")
    assert runner.step_results[-1]["window_result"]["window"]["title"] == "Invoice"


def test_absolute_fallback_is_used_only_when_explicit(monkeypatch, tmp_path: Path) -> None:
    clicked = []
    monkeypatch.setattr("rpa.runner.pyautogui", SimpleNamespace(click=lambda x, y, button="left": clicked.append((x, y)), FAILSAFE=True))
    base = {
        "window": target(window_title="Missing"), "relative_x": 10, "relative_y": 10,
        "button": "left", "fallback_x": 44, "fallback_y": 55,
    }
    runner = ReplayRunner(RpaProject(), tmp_path, lambda _message: None)
    runner.window_resolver = FakeResolver(WINDOWS[0], WindowNotFoundError("missing"))
    with pytest.raises(WindowNotFoundError):
        runner.run_action(RpaAction(ActionType.CLICK_WINDOW_RELATIVE.value, {**base, "use_absolute_fallback": False}))
    runner.run_action(RpaAction(ActionType.CLICK_WINDOW_RELATIVE.value, {**base, "use_absolute_fallback": True}))
    assert clicked == [(44, 55)]
    assert runner._last_window_result["fallback"] is True


@pytest.mark.parametrize(
    "kind, operation",
    [
        (ActionType.ACTIVATE_WINDOW, "activate"),
        (ActionType.MAXIMIZE_WINDOW, "maximize"),
        (ActionType.MINIMIZE_WINDOW, "minimize"),
        (ActionType.RESTORE_WINDOW, "restore"),
        (ActionType.CLOSE_WINDOW, "close"),
    ],
)
def test_runner_window_management_actions(kind: ActionType, operation: str, tmp_path: Path) -> None:
    resolver = FakeResolver(WINDOWS[1])
    runner = ReplayRunner(RpaProject(), tmp_path, lambda _message: None)
    runner.window_resolver = resolver
    runner.run_action(RpaAction(kind.value, {"window": target(window_title="Notes", title_match="exact")}))
    assert resolver.operations == [operation]
    assert runner._last_window_result["window"]["process_name"] == "notepad.exe"


def test_runner_moves_relative_to_negative_monitor_window(monkeypatch, tmp_path: Path) -> None:
    moved = {}
    monkeypatch.setattr(
        "rpa.runner.pyautogui",
        SimpleNamespace(moveTo=lambda x, y, duration=0: moved.update(x=x, y=y, duration=duration), FAILSAFE=True),
    )
    runner = ReplayRunner(RpaProject(), tmp_path, lambda _message: None)
    runner.window_resolver = FakeResolver(WINDOWS[0])
    runner.run_action(RpaAction(ActionType.MOVE_WINDOW_RELATIVE.value, {
        "window": target(process_name="excel.exe", window_title="Report", class_name="XLMAIN"),
        "relative_x": 125, "relative_y": 90, "duration": 0.4,
        "scale_with_window": False, "use_absolute_fallback": False,
    }))
    assert moved == {"x": -1075, "y": 190, "duration": 0.4}


def test_window_validation_catches_targets_regex_selected_order_and_fallback(tmp_path: Path) -> None:
    project = RpaProject(actions=[
        RpaAction(ActionType.ACTIVATE_WINDOW.value, {"use_selected_window": True, "window": {}}),
        RpaAction(ActionType.SELECT_WINDOW.value, {"window": target(window_title="[")}),
        RpaAction(ActionType.CLICK_WINDOW_RELATIVE.value, {
            "window": target(), "relative_x": "bad", "relative_y": 2,
            "scale_with_window": True, "original_window_width": 0, "original_window_height": 0,
            "use_absolute_fallback": True, "fallback_x": "bad", "fallback_y": 1,
        }),
    ])
    project.actions[1].data["window"]["title_match"] = "regex"
    reasons = "\n".join(issue.reason for issue in validate_project_detailed(project, tmp_path))
    assert "no earlier enabled Select / Target Window" in reasons
    assert "regular expression is invalid" in reasons
    assert "window-relative position requires valid X and Y coordinates" in reasons
    assert "original window width and height" in reasons
    assert "absolute fallback position requires valid X and Y coordinates" in reasons


def test_window_steps_save_reload_and_generate_standalone_python(tmp_path: Path) -> None:
    project = RpaProject(actions=[
        RpaAction(ActionType.SELECT_WINDOW.value, {"window": target()}),
        RpaAction(ActionType.WAIT_WINDOW.value, {"use_selected_window": True, "window": {"timeout": 2, "retry_interval": 0.2}}),
        RpaAction(ActionType.ACTIVATE_WINDOW.value, {"use_selected_window": True, "window": {}}),
        RpaAction(ActionType.MAXIMIZE_WINDOW.value, {"use_selected_window": True, "window": {}}),
        RpaAction(ActionType.CLICK_WINDOW_RELATIVE.value, {
            "use_selected_window": True, "window": {}, "relative_x": 100, "relative_y": 80,
            "scale_with_window": False, "button": "left", "use_absolute_fallback": False,
        }),
        RpaAction(ActionType.CLOSE_WINDOW.value, {"use_selected_window": True, "window": {}}),
    ])
    ProjectManager().save(project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert [item.action for item in loaded.actions] == [item.action for item in project.actions]
    generated = generate_python(loaded, tmp_path)
    py_compile.compile(str(generated), doraise=True)
    text = generated.read_text(encoding="utf-8")
    assert "def resolve_window(target):" in text
    assert "SELECTED_WINDOW_TARGET = dict(__window_target_1)" in text
    assert "relative_window_action(__window_target_5" in text
    assert "close_window(__window_6)" in text


def test_manual_relative_window_form_uses_captured_window_details() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import ManualActionDialog

    app = QApplication.instance() or QApplication([])
    dialog = ManualActionDialog(ProjectSettings(), {})
    dialog.type_box.setCurrentIndex(dialog.type_box.findData(ActionType.CLICK_WINDOW_RELATIVE.value))
    requested = []
    dialog.screen_pick_requested.connect(requested.append)
    dialog.window_editor.pick_button.click()
    assert requested == ["window_target"]
    dialog.set_window_target(
        target(process_name="billing.exe", window_title="Invoice", title_match="exact"),
        {"process_name": "billing.exe", "title": "Invoice", "left": -900, "top": 100, "width": 800, "height": 600},
        (-500, 350),
    )
    result = dialog.action()
    assert result.action == ActionType.CLICK_WINDOW_RELATIVE.value
    assert result.data["relative_x"] == 400
    assert result.data["relative_y"] == 250
    assert result.data["fallback_x"] == -500
    assert result.data["use_absolute_fallback"] is False
    assert result.data["window"]["process_name"] == "billing.exe"
    dialog.deleteLater(); app.processEvents()
