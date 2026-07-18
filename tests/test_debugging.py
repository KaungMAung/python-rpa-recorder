from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rpa.runner as runner_module
from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.runner import ReplayRunner, StopReplay


def _action(code: str, breakpoint: bool = False) -> RpaAction:
    return RpaAction(ActionType.PYTHON_CODE.value, {"code": code}, breakpoint=breakpoint)


def _start_debug_runner(tmp_path: Path, actions: list[RpaAction]):
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    pauses: queue.Queue[tuple[int, str, dict]] = queue.Queue()
    errors: list[Exception] = []
    runner = ReplayRunner(RpaProject(actions=actions), tmp_path, lambda _message: None)

    def run() -> None:
        try:
            runner.run(
                include_start_delay=False,
                debug_callback=lambda i, reason, values: pauses.put((i, reason, values)),
                enable_debug=True,
            )
        except Exception as exc:  # asserted by stop tests
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return runner, pauses, errors, thread


def test_multiple_breakpoints_pause_and_resume(tmp_path: Path) -> None:
    actions = [
        _action("variables['order'] = variables.get('order', []) + [1]", True),
        _action("variables['order'] += [2]"),
        _action("variables['order'] += [3]", True),
    ]
    runner, pauses, errors, thread = _start_debug_runner(tmp_path, actions)
    assert pauses.get(timeout=2)[:2] == (0, "breakpoint")
    runner.resume_debug()
    assert pauses.get(timeout=2)[:2] == (2, "breakpoint")
    runner.resume_debug()
    thread.join(2)

    assert not thread.is_alive() and not errors
    assert runner.runtime_variables["order"] == [1, 2, 3]
    assert runner.step_results[0]["debug_events"][0]["event"] == "pause"
    assert any(event["event"] == "resume" for event in runner.step_results[2]["debug_events"])


def test_step_over_and_skip_pause_at_next_executable_step(tmp_path: Path) -> None:
    actions = [
        _action("variables['first'] = True", True),
        _action("variables['second'] = True"),
        _action("variables['third'] = True"),
    ]
    runner, pauses, errors, thread = _start_debug_runner(tmp_path, actions)
    assert pauses.get(timeout=2)[0] == 0
    runner.step_over_debug()
    assert pauses.get(timeout=2)[:2] == (1, "step")
    runner.skip_debug_step()
    assert pauses.get(timeout=2)[:2] == (2, "step")
    runner.resume_debug()
    thread.join(2)

    assert not errors
    assert runner.runtime_variables["first"] is True
    assert "second" not in runner.runtime_variables
    assert runner.runtime_variables["third"] is True
    skipped = next(result for result in runner.step_results if result["step_number"] == 2)
    assert skipped["status"] == "Skipped"
    assert any(event["event"] == "skip" for event in skipped["debug_events"])


def test_stop_interrupts_breakpoint_wait_and_records_pause(tmp_path: Path) -> None:
    runner, pauses, errors, thread = _start_debug_runner(
        tmp_path, [_action("variables['ran'] = True", True)],
    )
    assert pauses.get(timeout=2)[0] == 0
    runner.request_stop()
    thread.join(2)

    assert not thread.is_alive()
    assert errors and isinstance(errors[0], StopReplay)
    assert "ran" not in runner.runtime_variables
    assert runner.step_results[0]["status"] == "Stopped"
    assert runner.step_results[0]["debug_events"][0]["event"] == "pause"


def test_restart_from_selected_step(tmp_path: Path) -> None:
    actions = [
        _action("variables['count'] = variables.get('count', 0) + 1"),
        _action("variables['done'] = True", True),
    ]
    runner, pauses, errors, thread = _start_debug_runner(tmp_path, actions)
    assert pauses.get(timeout=2)[0] == 1
    runner.restart_debug_from(0)
    assert pauses.get(timeout=2)[:2] == (0, "step")
    runner.resume_debug()
    assert pauses.get(timeout=2)[0] == 1
    runner.resume_debug()
    thread.join(2)

    assert not errors and runner.runtime_variables["count"] == 2
    assert runner.runtime_variables["done"] is True
    assert any(
        event["event"] == "restart"
        for result in runner.step_results
        for event in result.get("debug_events", [])
    )


def test_breakpoint_persists_but_generated_python_keeps_normal_execution(tmp_path: Path) -> None:
    project = RpaProject(actions=[_action("variables['ran'] = True", True)])
    ProjectManager().save(project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert loaded.actions[0].breakpoint is True

    generated = generate_python(loaded, tmp_path).read_text(encoding="utf-8")
    assert "variables['ran'] = True" in generated
    assert "breakpoint" not in generated.lower()


def test_breakpoint_is_noninteractive_when_debugging_is_disabled(tmp_path: Path) -> None:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    runner = ReplayRunner(
        RpaProject(actions=[_action("variables['scheduled'] = True", True)]),
        tmp_path, lambda _message: None,
    )
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["scheduled"] is True


def test_action_table_shows_breakpoint_marker_and_toolbar_debug_controls() -> None:
    from PySide6.QtWidgets import QApplication
    from ui.action_table import ActionTable
    from ui.recorder_toolbar import FloatingExecutionToolbar

    app = QApplication.instance() or QApplication([])
    table = ActionTable()
    table.set_actions([_action("pass", True)])
    assert "●" in table.item(0, 0).text()
    assert "Breakpoint" in table.item(0, 0).toolTip()

    toolbar = FloatingExecutionToolbar()
    toolbar.set_debug_paused("Paused before Step 1", "Next: Step 2")
    app.processEvents()
    assert toolbar.debug_controls.isVisibleTo(toolbar)
    assert toolbar.next_step.text() == "Next: Step 2"
    toolbar.close()
    table.close()


def test_debug_variables_masks_sensitive_and_applies_editable_values() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from rpa.models import RuntimeInputDefinition
    from ui.debug_variables_dialog import DebugVariablesDialog

    app = QApplication.instance() or QApplication([])
    project = RpaProject(
        variables={"COUNT": "1"},
        runtime_inputs={
            "PASSWORD": RuntimeInputDefinition(type="password", sensitive=True),
        },
        output_variables=["RESULT"],
    )
    dialog = DebugVariablesDialog(
        project, {"COUNT": 1, "PASSWORD": "secret", "RESULT": "old", "RUN_DATE": "2026-07-19"},
        {"PASSWORD"}, {"RUN_DATE"},
    )
    rows = {dialog.table.item(row, 0).text(): row for row in range(dialog.table.rowCount())}
    assert dialog.table.item(rows["PASSWORD"], 2).text() == "[REDACTED]"
    assert not (dialog.table.item(rows["PASSWORD"], 2).flags() & Qt.ItemIsEditable)
    assert dialog.table.item(rows["RUN_DATE"], 3).text() == "Protected"
    dialog.table.item(rows["COUNT"], 2).setText("7")
    dialog._apply()
    app.processEvents()
    assert dialog.values["COUNT"] == 7
    assert dialog.values["PASSWORD"] == "secret"
    dialog.close()


def test_real_main_window_context_action_toggles_breakpoint(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.project = RpaProject(actions=[_action("pass")])
    window.project_dir = tmp_path
    window._reset_history()
    window.refresh()
    window.table.selectRow(0)
    window.handle_table_context_action("toggle_breakpoint")
    app.processEvents()

    assert window.project.actions[0].breakpoint is True
    assert "●" in window.table.item(0, 0).text()
    window.save_project()
    assert ProjectManager().load(tmp_path / "project.json").actions[0].breakpoint is True
    window.close()
