from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFormLayout

from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.runner import ReplayActionError, ReplayRunner
from ui.action_editor import ActionEditor
from rpa.utils import MissingPlaceholderError
from rpa.validator import validate_project
from ui.main_window import MainWindow


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def make_action(text: str) -> RpaAction:
    return RpaAction(ActionType.TYPE_TEXT.value, {"text": text})


def make_window() -> MainWindow:
    app()
    window = MainWindow()
    window.project.actions = [make_action("one"), make_action("two"), make_action("three")]
    window.refresh()
    window.show()
    app().processEvents()
    return window


def action_texts(window: MainWindow) -> list[str]:
    return [action.data["text"] for action in window.project.actions]


def test_insert_before_after_and_append_ordering() -> None:
    window = make_window()
    window.table.selectRow(1)
    window.insert_action(make_action("before"), "before")
    assert action_texts(window) == ["one", "before", "two", "three"]
    assert window.table.selected_index() == 1

    window.insert_action(make_action("after"), "after")
    assert action_texts(window) == ["one", "before", "after", "two", "three"]
    assert window.table.selected_index() == 2

    window.clear_step_selection()
    window.insert_action(make_action("append"))
    assert action_texts(window)[-1] == "append"
    assert window.table.item(window.table.rowCount() - 1, 0).text() == str(len(window.project.actions))


def test_duplicate_delete_move_up_down_and_order_renumbering() -> None:
    window = make_window()
    window.table.selectRow(0)
    window.duplicate_action()
    assert action_texts(window)[:2] == ["one", "one"]
    assert window.project.actions[0].id != window.project.actions[1].id

    window.move_action(1)
    assert window.table.selected_index() == 2
    window.move_action(-1)
    assert window.table.selected_index() == 1

    window.delete_action()
    assert len(window.project.actions) == 3
    assert [window.table.item(row, 0).text() for row in range(window.table.rowCount())] == ["1", "2", "3"]


def test_filter_does_not_change_execution_order() -> None:
    window = make_window()
    original = [action.id for action in window.project.actions]
    window.filter_box.setText("two")
    app().processEvents()
    assert [action.id for action in window.project.actions] == original
    assert window.table.isRowHidden(0)
    assert not window.table.isRowHidden(1)


def test_empty_area_deselection_method() -> None:
    window = make_window()
    window.table.selectRow(0)
    window.clear_step_selection()
    assert window.table.selected_index() == -1
    assert window.editor_scroll.isVisible()
    assert window.editor.action is None


def test_python_code_execution_shared_runtime_variables(tmp_path: Path) -> None:
    project = RpaProject()
    runner = ReplayRunner(project, tmp_path, lambda message: None)
    first = RpaAction(ActionType.PYTHON_CODE.value, {"name": "set result", "code": "variables['result'] = 42", "continue_on_error": False})
    second = RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{result}}", "interval": 0})
    written = {}
    import rpa.runner as runner_module
    runner_module.pyautogui = SimpleNamespace(write=lambda text, interval=0: written.update(text=text))
    runner.run_action(first, runner.runtime_variables, 1)
    runner.run_action(second, runner.runtime_variables, 2)
    assert runner.runtime_variables["result"] == 42
    assert written["text"] == "42"


def test_missing_placeholder_errors(tmp_path: Path) -> None:
    runner = ReplayRunner(RpaProject(), tmp_path, lambda message: None)
    action = RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{missing}}"})
    with pytest.raises(MissingPlaceholderError):
        runner.run_action(action, runner.runtime_variables, 1)


def test_continue_on_error_behavior(tmp_path: Path) -> None:
    logs: list[str] = []
    runner = ReplayRunner(RpaProject(), tmp_path, logs.append)
    action = RpaAction(ActionType.PYTHON_CODE.value, {"code": "raise ValueError('bad')", "continue_on_error": True})
    runner.run_action(action, runner.runtime_variables, 1)
    assert any("exception ignored" in log for log in logs)

    action.data["continue_on_error"] = False
    with pytest.raises(RuntimeError):
        runner.run_action(action, runner.runtime_variables, 1)


def test_generated_python_code_is_explicit_and_ordered(tmp_path: Path) -> None:
    project = RpaProject()
    project.actions = [
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "start", "interval": 0}),
        RpaAction(ActionType.PYTHON_CODE.value, {"name": "calc", "code": "variables['result'] = 5", "continue_on_error": True}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{result}}", "interval": 0}),
    ]
    path = generate_python(project, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "def step_2_calc" in text
    assert "exec(" not in text
    assert text.index("# Action 1") < text.index("# Action 2") < text.index("# Action 3")


def test_validation_allows_runtime_variable_from_previous_python_code(tmp_path: Path) -> None:
    project = RpaProject()
    project.actions = [
        RpaAction(ActionType.PYTHON_CODE.value, {"name": "calc", "code": "variables['result'] = 5", "continue_on_error": False}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{result}}", "interval": 0}),
    ]
    assert validate_project(project, tmp_path) == []


def test_friendly_step_summaries() -> None:
    click = RpaAction(ActionType.CLICK_IMAGE.value, {"image": "screenshots/click.png"})
    typed = RpaAction(ActionType.TYPE_TEXT.value, {"text": "Customer Name"})
    key = RpaAction(ActionType.PRESS_KEY.value, {"key": "enter", "count": 1})
    assert click.friendly_name() == "Click"
    assert click.summary() == "Click screen target"
    assert typed.summary() == 'Type "Customer Name"'
    assert key.summary() == "Press Enter"
    click.name = "Click Save"
    assert click.summary() == "Click Save"


def test_bounded_replay_preserves_original_step_indexes(tmp_path: Path, monkeypatch) -> None:
    import rpa.runner as runner_module

    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    project = RpaProject()
    project.settings.start_delay = 0
    project.actions = [
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables.setdefault('steps', []).append(1)"}),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables.setdefault('steps', []).append(2)"}),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables.setdefault('steps', []).append(3)"}),
    ]
    statuses: list[tuple[int, str]] = []
    runner = ReplayRunner(project, tmp_path, lambda message: None)
    runner.run(lambda index, status: statuses.append((index, status)), 1, 2, False)
    assert runner.runtime_variables["steps"] == [2, 3]
    assert (1, "completed") in statuses
    assert (2, "completed") in statuses
    assert all(index != 0 for index, _ in statuses)


def test_bounded_replay_reports_exact_failed_step(tmp_path: Path, monkeypatch) -> None:
    import rpa.runner as runner_module

    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    project = RpaProject()
    project.actions = [
        RpaAction(ActionType.WAIT.value, {"seconds": 0}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{missing}}"}),
    ]
    runner = ReplayRunner(project, tmp_path, lambda message: None)
    with pytest.raises(ReplayActionError) as exc_info:
        runner.run(start_index=1, end_index=1, include_start_delay=False)
    assert exc_info.value.index == 1
    assert "missing" in str(exc_info.value)


def test_step_details_hide_advanced_fields_by_default() -> None:
    app()
    editor = ActionEditor()
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/click.png",
        "confidence": 0.86,
        "timeout": 10,
    })
    editor.set_action(action, None)
    assert not editor.advanced_button.isChecked()
    assert editor.advanced_widget.isHidden()
    editor.set_advanced_expanded(True)
    assert editor.advanced_widget.isVisibleTo(editor)


def test_step_details_exposes_retry_and_failure_controls() -> None:
    from PySide6.QtWidgets import QComboBox

    app()
    editor = ActionEditor()
    action = RpaAction(ActionType.WAIT.value, {"seconds": 1})
    editor.set_action(action, None)
    labels = [
        editor.advanced_form.itemAt(row, QFormLayout.LabelRole).widget().text()
        for row in range(editor.advanced_form.rowCount())
        if editor.advanced_form.itemAt(row, QFormLayout.LabelRole)
        and editor.advanced_form.itemAt(row, QFormLayout.LabelRole).widget()
    ]
    assert "Retry count" in labels
    assert "Delay between retries" in labels
    assert "Step timeout (0 = off)" in labels
    assert "On final failure" in labels
    failure_combo = editor.advanced_widget.findChild(QComboBox)
    assert failure_combo is not None
    assert [failure_combo.itemData(i) for i in range(failure_combo.count())] == ["stop", "continue", "jump"]


def test_enable_disable_and_range_commands_use_selected_step(monkeypatch) -> None:
    window = make_window()
    window.table.selectRow(1)
    window.toggle_selected_action()
    assert not window.project.actions[1].enabled
    assert window.table.item(1, 5).text() == "Disabled"

    calls: list[tuple] = []
    monkeypatch.setattr(window, "_start_replay", lambda *args, **kwargs: calls.append(args))
    window.run_from_here()
    window.run_until_here()
    assert calls[0][:3] == (1, 2, "from")
    assert calls[1][:3] == (0, 1, "until")


def test_disabled_step_does_not_block_validation(tmp_path: Path) -> None:
    project = RpaProject()
    project.actions = [
        RpaAction(ActionType.CLICK_IMAGE.value, {"image": "screenshots/missing.png"}, enabled=False),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "ready"}),
    ]
    assert validate_project(project, tmp_path) == []


def test_replay_worker_can_continue_with_runtime_variables(tmp_path: Path) -> None:
    from ui.main_window import ReplayWorker

    project = RpaProject()
    worker = ReplayWorker(project, tmp_path, runtime_variables={"result": 42})
    assert worker.runner.runtime_variables == {"result": 42}


def test_replay_worker_receives_image_match_exclusions(tmp_path: Path) -> None:
    from ui.main_window import ReplayWorker

    region = (10, 20, 300, 200)
    worker = ReplayWorker(RpaProject(), tmp_path, excluded_regions=[region])
    assert worker.runner.excluded_regions == [region]


def test_new_run_clears_all_previous_step_statuses() -> None:
    window = make_window()
    window.project.actions[0].status = "completed"
    window.project.actions[1].status = "failed"
    window.project.actions[2].status = "skipped"
    window._reset_action_statuses()
    assert [action.status for action in window.project.actions] == ["pending", "pending", "pending"]
    assert [window.table.item(row, 5).text() for row in range(3)] == ["Pending", "Pending", "Pending"]
