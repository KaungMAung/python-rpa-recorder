from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rpa.models import ActionType, RpaAction, RpaProject, RuntimeInputDefinition, VariableDefinition
from rpa.variables import (
    json_compatible_runtime_values, mask_sensitive_text, prepare_runtime_variables,
    validate_variable_configuration,
)


def test_legacy_project_variables_load_without_new_categories() -> None:
    original = RpaProject(variables={"EXISTING": "value"}).to_dict()
    original.pop("runtime_inputs")
    original.pop("output_variables")
    restored = RpaProject.from_dict(original)
    assert restored.variables == {"EXISTING": "value"}
    assert restored.runtime_inputs == {}
    assert restored.output_variables == []


def test_legacy_json_string_variable_map_is_migrated() -> None:
    payload = RpaProject().to_dict()
    payload["variables"] = '{"COUNT": 3, "OPTIONS": ["A", "B"]}'
    restored = RpaProject.from_dict(payload)
    assert restored.variables == {"COUNT": 3, "OPTIONS": ["A", "B"]}
    assert restored.variable_definitions["COUNT"].type == "integer"


def test_runtime_inputs_are_typed_defaulted_and_combined_with_builtins(tmp_path: Path) -> None:
    selected = tmp_path / "input.csv"
    selected.write_text("data", encoding="utf-8")
    project = RpaProject(
        variables={"PROJECT_NAME": "Demo"},
        runtime_inputs={
            "COUNT": RuntimeInputDefinition("number", "2", True),
            "REPORT_DATE": RuntimeInputDefinition("date", "2026-07-19", True),
            "MODE": RuntimeInputDefinition("dropdown", "Daily", True, False, ["Daily", "Weekly"]),
            "INPUT_FILE": RuntimeInputDefinition("file", "", True),
            "NOTE": RuntimeInputDefinition("text", "", False),
        },
    )
    values, errors = prepare_runtime_variables(
        project, {"COUNT": "3.5", "INPUT_FILE": str(selected)}, "clipboard",
        datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    assert errors == []
    assert values["PROJECT_NAME"] == "Demo"
    assert values["COUNT"] == 3.5
    assert values["REPORT_DATE"] == "2026-07-19"
    assert values["CLIPBOARD_TEXT"] == "clipboard"
    assert values["RUN_DATE"] == "2026-07-19"
    assert values["LAST_CLICK_X"] == 0


def test_required_invalid_and_conflicting_variables_are_rejected(tmp_path: Path) -> None:
    project = RpaProject(
        variables={"DUPLICATE": "saved"},
        runtime_inputs={
            "DUPLICATE": RuntimeInputDefinition(),
            "RUN_DATE": RuntimeInputDefinition(),
            "CHOICE": RuntimeInputDefinition("dropdown", "", True, False, []),
            "INPUT_FILE": RuntimeInputDefinition("file", "", True),
        },
    )
    configuration = "\n".join(validate_variable_configuration(project))
    assert "already exists" in configuration
    assert "built-in" in configuration
    assert "dropdown choice" in configuration
    _values, errors = prepare_runtime_variables(project, {"INPUT_FILE": str(tmp_path / "missing")})
    assert any("path does not exist" in error for error in errors)


def test_sensitive_values_are_masked_without_partial_leaks() -> None:
    message = mask_sensitive_text("token=secret-123 and secret-123", {"secret-123"})
    assert "secret-123" not in message
    assert message.count("[REDACTED]") == 2


def test_runner_writes_outputs_and_last_click_coordinates(tmp_path: Path, monkeypatch) -> None:
    import rpa.runner as runner_module
    from rpa.runner import ReplayRunner

    clicks = []
    gui = SimpleNamespace(FAILSAFE=True, click=lambda x, y, **kwargs: clicks.append((x, y)))
    monkeypatch.setattr(runner_module, "pyautogui", gui)
    project = RpaProject(actions=[
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "result = 42", "output_variable": "ANSWER"}),
        RpaAction(ActionType.CLICK_COORDINATE.value, {"x": "{{ANSWER}}", "y": 9, "button": "left"}),
    ])
    project.settings.start_delay = 0
    runner = ReplayRunner(project, tmp_path, lambda _message: None)
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["ANSWER"] == 42
    assert runner.runtime_variables["LAST_CLICK_X"] == 42
    assert runner.runtime_variables["LAST_CLICK_Y"] == 9
    assert clicks == [(42, 9)]


def test_validator_accepts_runtime_inputs_and_prior_step_outputs(tmp_path: Path) -> None:
    from rpa.validator import validate_project

    project = RpaProject(
        runtime_inputs={"INPUT_FILE": RuntimeInputDefinition("text", "", True)},
        actions=[
            RpaAction(ActionType.PYTHON_CODE.value, {"code": "result = 'done'", "output_variable": "RESULT"}),
            RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{INPUT_FILE}} {{RESULT}} {{RUN_DATE}}"}),
        ],
    )
    assert validate_project(project, tmp_path) == []


def test_generated_python_prompts_inputs_and_resolves_technical_fields(tmp_path: Path) -> None:
    from rpa.generator import generate_python

    project = RpaProject(
        runtime_inputs={
            "TARGET_X": RuntimeInputDefinition("number", 10, True),
            "PASSWORD": RuntimeInputDefinition("password", "", True, True),
        },
        actions=[
            RpaAction(ActionType.CLICK_COORDINATE.value, {"x": "{{TARGET_X}}", "y": 20, "button": "left"}),
            RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{PASSWORD}}", "output_variable": "TYPED"}),
        ],
    )
    text = generate_python(project, tmp_path).read_text(encoding="utf-8")
    compile(text, "generated_rpa.py", "exec")
    assert "RUNTIME_INPUTS =" in text
    assert "getpass.getpass" in text
    assert "RPA_INPUT_" in text
    assert "as_int('{{TARGET_X}}')" in text
    assert "RUNTIME_VARIABLES['TYPED'] = typed_value" in text


def test_schedule_runtime_inputs_round_trip(tmp_path: Path) -> None:
    from rpa.scheduler import ScheduleStore

    flow = tmp_path / "demo"
    flow.mkdir()
    (flow / "project.json").write_text("{}", encoding="utf-8")
    store = ScheduleStore(tmp_path)
    schedule = store.get("demo")
    schedule.runtime_inputs = {"REPORT_DATE": "2026-07-19", "PASSWORD": "secret"}
    store.set(schedule)
    store.save()
    restored = ScheduleStore(tmp_path).get("demo")
    assert restored.runtime_inputs == schedule.runtime_inputs


def test_runtime_input_dialog_uses_masked_and_typed_controls(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QComboBox, QDateEdit, QDialog, QLineEdit
    from ui.runtime_inputs_dialog import RuntimeInputsDialog

    app = QApplication.instance() or QApplication([])
    selected = tmp_path / "input.txt"
    selected.write_text("ok", encoding="utf-8")
    project = RpaProject(runtime_inputs={
        "PASSWORD": RuntimeInputDefinition("password", "", True, True),
        "REPORT_DATE": RuntimeInputDefinition("date", "2026-07-19", True),
        "MODE": RuntimeInputDefinition("dropdown", "Daily", True, False, ["Daily", "Weekly"]),
        "INPUT_FILE": RuntimeInputDefinition("file", str(selected), True),
    })
    dialog = RuntimeInputsDialog(project)
    assert isinstance(dialog.widgets["REPORT_DATE"], QDateEdit)
    assert isinstance(dialog.widgets["MODE"], QComboBox)
    password = dialog.widgets["PASSWORD"]
    assert isinstance(password, QLineEdit)
    assert password.echoMode() == QLineEdit.Password
    password.setText("secret")
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.Accepted
    assert dialog.runtime_variables["PASSWORD"] == "secret"
    app.processEvents()


def test_variables_panel_masks_current_sensitive_values() -> None:
    from PySide6.QtWidgets import QApplication
    from ui.dialogs import VariablesDialog

    app = QApplication.instance() or QApplication([])
    project = RpaProject(
        variables={"PROJECT": "Demo"},
        runtime_inputs={"PASSWORD": RuntimeInputDefinition("password", "", True, True)},
        output_variables=["RESULT"],
    )
    dialog = VariablesDialog(project, {"PROJECT": "Demo", "PASSWORD": "secret", "RESULT": 42})
    displayed = [dialog.current_table.item(row, 2).text() for row in range(dialog.current_table.rowCount())]
    assert "secret" not in displayed
    assert "[REDACTED]" in displayed
    assert dialog.output_list.item(0).text() == "RESULT"
    dialog.close()
    app.processEvents()


def _typed_project() -> RpaProject:
    return RpaProject(
        variables={"quantity": 100, "order": {"product": "ABC", "approved": False}, "api_token": "secret"},
        variable_definitions={
            "quantity": VariableDefinition("integer", 100, "Order quantity"),
            "order": VariableDefinition("object", {"product": "ABC", "approved": False}),
            "api_token": VariableDefinition("secret_text", "secret", secret=True),
        },
    )


def test_integer_default_is_loaded_as_integer_at_run_start() -> None:
    values, errors = prepare_runtime_variables(_typed_project(), validate_paths=False)
    assert errors == []
    assert values["quantity"] == 100
    assert isinstance(values["quantity"], int)


def test_python_steps_share_the_exact_mutable_variable_dictionary(tmp_path: Path) -> None:
    from rpa.runner import ReplayRunner

    project = _typed_project()
    runner = ReplayRunner(project, tmp_path, lambda _message: None)
    shared = runner.runtime_variables
    runner.run_action(RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['quantity'] += 5"}), shared, 1)
    runner.run_action(RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['seen'] = variables['quantity']"}), shared, 2)
    assert runner.runtime_variables is shared
    assert shared["quantity"] == shared["seen"] == 105


def test_nested_object_update_and_nested_template_resolution(tmp_path: Path, monkeypatch) -> None:
    import rpa.runner as runner_module
    from rpa.runner import ReplayRunner

    typed: list[str] = []
    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(
        FAILSAFE=True, write=lambda text, **_kwargs: typed.append(text), hotkey=lambda *_args: None,
        press=lambda *_args, **_kwargs: None,
    ))
    project = _typed_project()
    runner = ReplayRunner(project, tmp_path, lambda _message: None)
    runner.run_action(RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "variables['order']['approved'] = True",
    }), step_number=1)
    runner.run_action(RpaAction(ActionType.TYPE_TEXT.value, {
        "text": "{{order.product}} approved={{order.approved}}", "interval": 0,
    }), step_number=2)
    assert runner.runtime_variables["order"]["approved"] is True
    assert typed == ["ABC approved=True"]


def test_new_run_resets_defaults_when_persistence_is_disabled(tmp_path: Path) -> None:
    from rpa.runner import ReplayRunner

    project = _typed_project()
    project.persisted_variable_values = {"quantity": 999}
    project.settings.persist_variable_values = False
    first = ReplayRunner(project, tmp_path, lambda _message: None)
    first.runtime_variables["quantity"] = 7
    second = ReplayRunner(project, tmp_path, lambda _message: None)
    assert second.runtime_variables["quantity"] == 100


def test_successful_run_updates_json_compatible_persisted_values(tmp_path: Path) -> None:
    from rpa.runner import ReplayRunner

    project = _typed_project()
    project.settings.start_delay = 0
    project.settings.persist_variable_values = True
    project.actions = [RpaAction(ActionType.INCREMENT_VARIABLE.value, {"variable": "quantity", "amount": 2})]
    ReplayRunner(project, tmp_path, lambda _message: None).run(include_start_delay=False)
    assert project.persisted_variable_values["quantity"] == 102
    next_values, errors = prepare_runtime_variables(project, validate_paths=False)
    assert errors == []
    assert next_values["quantity"] == 102


def test_secret_variable_updates_are_masked_in_runner_logs(tmp_path: Path) -> None:
    from rpa.runner import ReplayRunner

    messages: list[str] = []
    project = _typed_project()
    project.settings.start_delay = 0
    project.actions = [RpaAction(ActionType.SET_VARIABLE.value, {"variable": "api_token", "value": "new-secret"})]
    ReplayRunner(project, tmp_path, messages.append).run(include_start_delay=False)
    text = "\n".join(messages)
    assert "new-secret" not in text
    assert "Variable updated: api_token = ********" in text


def test_secret_printed_by_python_step_is_masked(tmp_path: Path) -> None:
    from rpa.runner import ReplayRunner

    messages: list[str] = []
    project = _typed_project()
    runner = ReplayRunner(project, tmp_path, messages.append)
    runner.run_action(RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "print(variables['api_token'])",
    }), step_number=1)
    assert "secret" not in "\n".join(messages)
    assert "[REDACTED]" in "\n".join(messages)


def test_invalid_json_in_variable_editor_shows_validation_error(monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
    from ui.dialogs import VariableEditorDialog

    app = QApplication.instance() or QApplication([])
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, message: shown.append(str(message)))
    dialog = VariableEditorDialog()
    dialog.name_edit.setText("items")
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData("list"))
    dialog.json_edit.setPlainText("[not valid JSON]")
    dialog._accept_if_valid()
    assert dialog.result() == QDialog.Rejected
    assert shown and "invalid list" in shown[0]
    dialog.close()
    app.processEvents()


def test_unsupported_runtime_object_has_clear_persistence_warning() -> None:
    compatible, warnings = json_compatible_runtime_values({"good": 1, "bad": object()})
    assert compatible == {"good": 1}
    assert warnings == ["bad: runtime value of type object cannot be persisted"]


def test_typed_definitions_round_trip_in_project_json_structure() -> None:
    project = _typed_project()
    payload = project.to_dict()
    assert payload["variables"]["definitions"]["quantity"]["type"] == "integer"
    restored = RpaProject.from_dict(payload)
    assert restored.variables["quantity"] == 100
    assert restored.variable_definitions["order"].default["product"] == "ABC"


def test_generated_python_supports_variable_steps_and_omits_secret_defaults(tmp_path: Path) -> None:
    from rpa.generator import generate_python

    project = _typed_project()
    project.actions = [
        RpaAction(ActionType.INCREMENT_VARIABLE.value, {"variable": "quantity", "amount": 2}),
        RpaAction(ActionType.SET_OBJECT_PROPERTY.value, {
            "variable": "order", "property": "approved", "value": True,
        }),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{order.product}}", "interval": 0}),
    ]
    source = generate_python(project, tmp_path).read_text(encoding="utf-8")
    compile(source, "generated_rpa.py", "exec")
    assert "RUNTIME_VARIABLES['quantity'] += resolve(2)" in source
    assert "RUNTIME_VARIABLES['order']['approved'] = resolve(True)" in source
    assert '"secret"' not in source
