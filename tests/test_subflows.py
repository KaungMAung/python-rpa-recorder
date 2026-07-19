from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

import rpa.runner as runner_module
from rpa.generator import generate_python
from rpa.models import ActionType, ProjectSettings, RpaAction, RpaProject, RuntimeInputDefinition
from rpa.project_manager import ProjectManager
from rpa.runner import ReplayRunner
from rpa.subflows import discover_saved_flows, portable_reference
from rpa.validator import LEVEL_ERROR, validate_project_detailed
from ui.dialogs import ManualActionDialog
from ui.action_editor import ActionEditor


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _save(path: Path, project: RpaProject) -> Path:
    ProjectManager().save(project, path)
    return path / "project.json"


def test_subflow_maps_inputs_outputs_and_records_nested_results(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "Parent", tmp_path / "Child"
    child = RpaProject(
        runtime_inputs={"VALUE": RuntimeInputDefinition(type="number")},
        output_variables=["DOUBLED"],
        actions=[RpaAction(ActionType.PYTHON_CODE.value, {
            "code": "variables['DOUBLED'] = variables['VALUE'] * 2",
        })],
    )
    child.project.name = "Child"
    child_json = _save(child_dir, child)
    action = RpaAction(ActionType.RUN_SUBFLOW.value, {
        "project": portable_reference(parent_dir, child_json),
        "flow_name": "Child",
        "input_mappings": {"VALUE": "SOURCE"},
        "output_mappings": {"DOUBLED": "RESULT"},
    })
    parent = RpaProject(variables={"SOURCE": 21}, output_variables=["RESULT"], actions=[action])
    _save(parent_dir, parent)
    reloaded = ProjectManager().load(parent_dir / "project.json")
    assert reloaded.actions[0].data["project"] == "../Child/project.json"
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    logs: list[str] = []
    runner = ReplayRunner(parent, parent_dir, logs.append)
    runner.run(include_start_delay=False)

    assert runner.runtime_variables["RESULT"] == 42
    assert runner.step_results[0]["subflow"]["status"] == "Success"
    assert runner.step_results[0]["subflow"]["step_results"][0]["status"] == "Success"
    assert any("[Subflow Child]" in line for line in logs)


def test_subflow_uses_common_retry_timeout_and_failure_handling(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "Parent", tmp_path / "Child"
    _save(child_dir, RpaProject(actions=[RpaAction(ActionType.WAIT.value, {"seconds": 1})]))
    action = RpaAction(ActionType.RUN_SUBFLOW.value, {
        "project": "../Child/project.json",
        "retry_count": 1,
        "retry_delay": 0,
        "step_timeout": 0.03,
        "failure_action": "continue",
    })
    project = RpaProject(actions=[action])
    _save(parent_dir, project)
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    runner = ReplayRunner(project, parent_dir, lambda _message: None)
    runner.run(include_start_delay=False)

    result = runner.step_results[0]
    assert result["status"] == "Failed"
    assert result["attempts"] == 2
    assert result["subflow"]["status"] == "Failed"
    assert "timed out" in result["error"]
    assert runner.had_continued_failures


def test_subflow_validation_detects_missing_cycle_and_mapping_errors(tmp_path: Path) -> None:
    parent_dir, child_dir = tmp_path / "Parent", tmp_path / "Child"
    parent = RpaProject(variables={"KNOWN": "x"})
    child = RpaProject(output_variables=["OUT"])
    parent.project.name, child.project.name = "Parent", "Child"
    parent.actions = [RpaAction(ActionType.RUN_SUBFLOW.value, {
        "project": "../Child/project.json",
        "input_mappings": {"NOT_IN_CHILD": "MISSING_PARENT"},
        "output_mappings": {"NOT_AN_OUTPUT": "bad name"},
    })]
    child.actions = [RpaAction(ActionType.RUN_SUBFLOW.value, {"project": "../Parent/project.json"})]
    _save(parent_dir, parent)
    _save(child_dir, child)

    reasons = [
        issue.reason for issue in validate_project_detailed(parent, parent_dir)
        if issue.level == LEVEL_ERROR
    ]
    assert any("circular subflow reference" in reason for reason in reasons)
    assert any("undefined parent variable" in reason for reason in reasons)
    assert any("not defined by the target flow" in reason for reason in reasons)
    assert any("not declared by the target flow" in reason for reason in reasons)
    assert any("invalid parent output variable" in reason for reason in reasons)

    parent.actions[0].data["project"] = "../Missing/project.json"
    reasons = [issue.reason for issue in validate_project_detailed(parent, parent_dir)]
    assert any("subflow project is missing" in reason for reason in reasons)


def test_subflow_picker_uses_saved_flows_and_generated_python_keeps_relative_path(tmp_path: Path) -> None:
    _app()
    parent_dir, child_dir = tmp_path / "Parent", tmp_path / "Child"
    child = RpaProject(
        runtime_inputs={"INPUT": RuntimeInputDefinition()}, output_variables=["OUTPUT"],
    )
    child.project.name = "Child"
    _save(parent_dir, RpaProject(variables={"PARENT": "value"}))
    _save(child_dir, child)
    assert [flow.name for flow in discover_saved_flows(parent_dir)] == ["Child"]

    dialog = ManualActionDialog(ProjectSettings(), {"PARENT": "value"}, project_dir=parent_dir)
    index = dialog.type_box.findData(ActionType.RUN_SUBFLOW.value)
    dialog.type_box.setCurrentIndex(index)
    dialog.subflow_editor.flow.setCurrentIndex(
        dialog.subflow_editor.flow.findData("../Child/project.json")
    )
    input_combo = dialog.subflow_editor.inputs.cellWidget(0, 1)
    assert isinstance(input_combo, QComboBox)
    input_combo.setCurrentIndex(input_combo.findData("PARENT"))
    action = dialog.action()
    assert action.action == ActionType.RUN_SUBFLOW.value
    assert action.data["project"] == "../Child/project.json"
    assert action.data["input_mappings"] == {"INPUT": "PARENT"}

    project = RpaProject(variables={"PARENT": "value"}, actions=[action])
    generated = generate_python(project, parent_dir).read_text(encoding="utf-8")
    compile(generated, "generated_rpa.py", "exec")
    assert "def run_subflow(" in generated
    assert "../Child/project.json" in generated

    editor = ActionEditor()
    editor.set_available_variables(["PARENT"])
    opened: list[str] = []
    editor.open_subflow_requested.connect(opened.append)
    editor.set_action(action, parent_dir)
    subflow_widget = editor.findChild(QComboBox, "subflowPicker").parentWidget()
    open_button = next(
        button for button in subflow_widget.findChildren(type(dialog.confirm_button))
        if button.text() == "Open Flow"
    )
    open_button.click()
    assert opened == ["../Child/project.json"]
    editor.close()
    dialog.close()
