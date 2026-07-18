from __future__ import annotations

import py_compile
import threading
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from rpa.control_flow import parse_control_flow
from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.runner import ReplayActionError, ReplayRunner, StopReplay
from rpa.validator import validate_project_detailed


def action(kind: ActionType, data: dict | None = None) -> RpaAction:
    return RpaAction(kind.value, data or {})


def prepare_runner(monkeypatch, project: RpaProject, tmp_path: Path) -> ReplayRunner:
    monkeypatch.setattr("rpa.runner.foreground_elevation_mismatch", lambda: None)
    monkeypatch.setattr("rpa.runner.pyautogui", SimpleNamespace(FAILSAFE=True))
    return ReplayRunner(project, tmp_path, lambda _message: None)


def test_parser_maps_nested_if_else_and_loop() -> None:
    actions = [
        action(ActionType.REPEAT_COUNT, {"count": 2}),
        action(ActionType.IF_VARIABLE, {"variable": "ready", "operator": "equals", "value": "yes"}),
        action(ActionType.WAIT, {"seconds": 0}),
        action(ActionType.ELSE),
        action(ActionType.BREAK_LOOP),
        action(ActionType.END_IF),
        action(ActionType.END_LOOP),
    ]
    flow = parse_control_flow(actions)
    assert not flow.issues
    assert flow.loop_end[0] == 6
    assert flow.if_else[1] == 3
    assert flow.group_ends[1] == 5
    assert flow.enclosing_loops[4] == [0]
    assert flow.depths == [0, 1, 2, 1, 2, 1, 0]


@pytest.mark.parametrize(
    "actions, expected",
    [
        ([action(ActionType.ELSE)], "Else must be inside"),
        ([action(ActionType.IF_VARIABLE, {"variable": "x"})], "missing End If"),
        ([action(ActionType.BREAK_LOOP)], "Break Loop must be inside"),
        ([action(ActionType.END_LOOP)], "End Loop has no matching"),
    ],
)
def test_validator_rejects_broken_nesting(tmp_path: Path, actions: list[RpaAction], expected: str) -> None:
    issues = validate_project_detailed(RpaProject(actions=actions), tmp_path)
    assert expected in "\n".join(issue.reason for issue in issues)


def test_runner_executes_nested_branches_count_loop_and_break(monkeypatch, tmp_path: Path) -> None:
    project = RpaProject(variables={"ready": "yes", "items": ""}, actions=[
        action(ActionType.REPEAT_COUNT, {"count": 3}),
        action(ActionType.IF_VARIABLE, {"variable": "ready", "operator": "equals", "value": "yes"}),
        action(ActionType.PYTHON_CODE, {"code": "variables['items'] += 'A'"}),
        action(ActionType.ELSE),
        action(ActionType.PYTHON_CODE, {"code": "variables['items'] += 'X'"}),
        action(ActionType.END_IF),
        action(ActionType.BREAK_LOOP),
        action(ActionType.PYTHON_CODE, {"code": "variables['items'] += 'B'"}),
        action(ActionType.END_LOOP),
    ])
    runner = prepare_runner(monkeypatch, project, tmp_path)
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["items"] == "A"
    conditions = [item["control_result"] for item in runner.step_results if item.get("control_result", {}).get("kind") == "condition"]
    assert conditions[0]["evaluated"] is True
    assert conditions[0]["branch"] == "If"
    assert any(item["status"] == "Skipped" for item in runner.step_results)


def test_all_visual_condition_types_are_evaluated(monkeypatch, tmp_path: Path) -> None:
    existing = tmp_path / "ready.txt"
    existing.write_text("ready", encoding="utf-8")
    project = RpaProject(variables={"customer": "Ada Lovelace", "blank": ""})
    runner = prepare_runner(monkeypatch, project, tmp_path)
    monkeypatch.setattr("rpa.runner.find_image", lambda *_args, **_kwargs: SimpleNamespace(found=True, confidence=0.93))
    monkeypatch.setattr("rpa.runner.get_pyautogui", lambda: SimpleNamespace(getAllTitles=lambda: ["Quarterly Report - Excel"]))

    assert runner._evaluate_condition({"image": "target.png", "confidence": 0.8}, "image_exists")[0]
    assert not runner._evaluate_condition({"image": "target.png", "confidence": 0.8}, "image_not_exists")[0]
    assert runner._evaluate_condition({"window_title": "report", "case_sensitive": False}, "window_exists")[0]
    assert runner._evaluate_condition({"path": "ready.txt", "path_type": "file"}, "path_exists")[0]
    assert runner._evaluate_condition({"variable": "customer", "operator": "contains", "value": "love"}, "variable")[0]
    assert runner._evaluate_condition({"variable": "blank", "operator": "is_empty"}, "variable")[0]


def test_repeat_until_evaluates_current_output_variables(monkeypatch, tmp_path: Path) -> None:
    project = RpaProject(variables={"counter": 0}, actions=[
        action(ActionType.REPEAT_UNTIL, {
            "condition_type": "variable", "variable": "counter", "operator": "equals", "value": "3",
            "max_iterations": 10, "iteration_delay": 0,
        }),
        action(ActionType.PYTHON_CODE, {"code": "variables['counter'] += 1"}),
        action(ActionType.END_LOOP),
    ])
    runner = prepare_runner(monkeypatch, project, tmp_path)
    progress: list[str] = []
    runner.run(include_start_delay=False, control_callback=lambda _index, message: progress.append(message))
    assert runner.runtime_variables["counter"] == 3
    assert any("completed after 3 iterations" in message for message in progress)
    end_results = [item["control_result"] for item in runner.step_results if item.get("control_result", {}).get("kind") == "loop_end"]
    assert end_results[-1]["condition"] is True
    assert end_results[-1]["iterations"] == 3


def test_repeat_until_safety_limit_fails_with_evidence(monkeypatch, tmp_path: Path) -> None:
    project = RpaProject(variables={"done": "no"}, actions=[
        action(ActionType.REPEAT_UNTIL, {
            "condition_type": "variable", "variable": "done", "operator": "equals", "value": "yes",
            "max_iterations": 2, "iteration_delay": 0,
        }),
        action(ActionType.END_LOOP),
    ])
    runner = prepare_runner(monkeypatch, project, tmp_path)
    with pytest.raises(ReplayActionError, match="safety limit"):
        runner.run(include_start_delay=False)
    failed = [item for item in runner.step_results if item["status"] == "Failed"]
    assert failed
    assert "safety limit" in failed[-1]["error"]


def test_stop_interrupts_repeat_until_delay(monkeypatch, tmp_path: Path) -> None:
    project = RpaProject(variables={"done": "no"}, actions=[
        action(ActionType.REPEAT_UNTIL, {
            "condition_type": "variable", "variable": "done", "operator": "equals", "value": "yes",
            "max_iterations": 1000, "iteration_delay": 10,
        }),
        action(ActionType.END_LOOP),
    ])
    runner = prepare_runner(monkeypatch, project, tmp_path)
    reached_delay = threading.Event()
    original_sleep = runner.sleep_checked

    def sleep_checked(seconds: float) -> None:
        if seconds == 10:
            reached_delay.set()
        original_sleep(seconds)

    runner.sleep_checked = sleep_checked
    caught: list[BaseException] = []
    thread = threading.Thread(target=lambda: _run_and_capture(runner, caught), daemon=True)
    thread.start()
    assert reached_delay.wait(1)
    runner.request_stop()
    thread.join(1)
    assert not thread.is_alive()
    assert caught and isinstance(caught[0], StopReplay)


def _run_and_capture(runner: ReplayRunner, caught: list[BaseException]) -> None:
    try:
        runner.run(include_start_delay=False)
    except BaseException as exc:  # test helper must preserve the exact stop exception
        caught.append(exc)


def test_control_steps_save_reload_and_generate_valid_python(tmp_path: Path) -> None:
    project = RpaProject(variables={"name": "Ada"}, actions=[
        action(ActionType.IF_VARIABLE, {"variable": "name", "operator": "contains", "value": "A"}),
        action(ActionType.TYPE_TEXT, {"text": "Hello", "interval": 0}),
        action(ActionType.ELSE),
        action(ActionType.TYPE_TEXT, {"text": "Unknown", "interval": 0}),
        action(ActionType.END_IF),
        action(ActionType.REPEAT_COUNT, {"count": 2}),
        action(ActionType.WAIT, {"seconds": 0}),
        action(ActionType.END_LOOP),
    ])
    manager = ProjectManager()
    manager.save(project, tmp_path)
    loaded = manager.load(tmp_path / "project.json")
    assert [item.action for item in loaded.actions] == [item.action for item in project.actions]
    generated = generate_python(loaded, tmp_path)
    py_compile.compile(str(generated), doraise=True)
    text = generated.read_text(encoding="utf-8")
    assert "if __condition_step_1:" in text
    assert "else:" in text
    assert "for __loop_step_6 in range(__loop_count_6):" in text


def test_generation_rejects_invalid_structure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid control-flow structure"):
        generate_python(RpaProject(actions=[action(ActionType.ELSE)]), tmp_path)


def test_action_table_indents_and_collapses_nested_blocks() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.action_table import ActionTable

    app = QApplication.instance() or QApplication([])
    actions = [
        action(ActionType.IF_VARIABLE, {"variable": "ready", "operator": "is_empty"}),
        action(ActionType.REPEAT_COUNT, {"count": 2}),
        action(ActionType.WAIT, {"seconds": 0}),
        action(ActionType.END_LOOP),
        action(ActionType.END_IF),
    ]
    table = ActionTable()
    table.set_actions(actions)
    table.apply_filter("")
    assert table.item(2, 1).text().startswith("        ")
    table._cell_clicked(0, 0)
    assert table.isRowHidden(1)
    assert table.isRowHidden(4)
    table._cell_clicked(0, 0)
    assert not table.isRowHidden(1)
    table.deleteLater()
    app.processEvents()


def test_main_window_inserts_matching_closer_and_undoes_as_one_change(monkeypatch) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.project = RpaProject()
    window.refresh()
    window._reset_history()
    assert window.insert_action(action(ActionType.REPEAT_COUNT, {"count": 2}))
    assert [item.action for item in window.project.actions] == [
        ActionType.REPEAT_COUNT.value, ActionType.END_LOOP.value,
    ]
    window.undo()
    assert window.project.actions == []
    window.redo()
    assert len(window.project.actions) == 2
    window.table.selectRow(0)
    window.duplicate_action()
    assert len(window.project.actions) == 4
    assert not parse_control_flow(window.project.actions).issues
    assert len({item.id for item in window.project.actions}) == 4
    window.table.selectRow(0)
    window.delete_action()
    assert len(window.project.actions) == 2
    assert not parse_control_flow(window.project.actions).issues
    errors: list[str] = []
    monkeypatch.setattr("ui.main_window.show_error", lambda _parent, _title, message: errors.append(message))
    window.table.clearSelection()
    assert not window.insert_action(action(ActionType.ELSE))
    assert errors and "invalid block" in errors[0]
    window.close()
    app.processEvents()
