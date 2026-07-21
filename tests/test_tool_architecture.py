from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rpa.execution import (
    COMPLETED_UNVERIFIED, COMPLETED_VERIFIED, RECOVERED, REQUIRES_ATTENTION,
    ExecutionContext,
)
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.control_flow import CONTROL_TYPES, METADATA_TYPES
from rpa.builtin_tools import create_builtin_registry
from rpa.tools import FunctionTool, ToolRegistry, ToolResult
from rpa.verification import SUPPORTED_VERIFICATIONS, VerificationEngine


def context(tmp_path: Path, variables: dict | None = None) -> ExecutionContext:
    return ExecutionContext(
        project=RpaProject(), project_dir=tmp_path, variables=variables or {},
        log=lambda _message: None,
    )


def test_tool_registry_registers_validates_and_executes() -> None:
    registry = ToolRegistry()
    seen = []
    registry.register("mock", FunctionTool("mock", "A mock tool", lambda inputs, _context: seen.append(inputs["value"])))
    result = registry.execute("mock", {"value": 42}, context(Path.cwd()))
    assert result == ToolResult(value=None)
    assert seen == [42]
    assert registry.action_types() == ("mock",)


def test_builtin_registry_contains_every_executable_action_type() -> None:
    expected = {item.value for item in ActionType} - CONTROL_TYPES - METADATA_TYPES
    assert set(create_builtin_registry().action_types()) == expected


def test_old_action_and_project_json_load_without_phase_one_fields() -> None:
    project = RpaProject(actions=[RpaAction("wait", {"seconds": 1})])
    payload = project.to_dict()
    payload.pop("success_when", None)
    payload["actions"][0].pop("expect", None)
    payload["actions"][0].pop("on_failure", None)
    restored = RpaProject.from_dict(payload)
    assert restored.success_when is None
    assert restored.actions[0].expect is None
    assert restored.actions[0].on_failure is None
    assert restored.actions[0].data == {"seconds": 1}


def test_phase_one_fields_round_trip() -> None:
    action = RpaAction(
        "wait", {"seconds": 0},
        expect={"type": "variable_not_empty", "value": "result"},
        on_failure={"retry_count": 2, "ask_user": True},
    )
    project = RpaProject(
        actions=[action],
        success_when={"mode": "all", "conditions": [{"type": "file_exists", "value": "done.txt"}]},
    )
    restored = RpaProject.from_dict(project.to_dict())
    assert restored.actions[0].expect == action.expect
    assert restored.actions[0].on_failure == action.on_failure
    assert restored.success_when == project.success_when


def test_verification_engine_supports_all_phase_one_types(tmp_path: Path) -> None:
    engine = VerificationEngine()
    probes = {kind: (lambda _condition, _context: True) for kind in SUPPORTED_VERIFICATIONS}
    ctx = context(tmp_path)
    ctx.execution_state["verification_probes"] = probes
    for kind in SUPPORTED_VERIFICATIONS:
        assert engine.verify({"type": kind}, ctx).passed


def test_variable_and_file_verification_and_completion_modes(tmp_path: Path) -> None:
    existing = tmp_path / "result.txt"
    existing.write_text("ready", encoding="utf-8")
    engine = VerificationEngine()
    ctx = context(tmp_path, {"output_file": "result.txt", "report": {"path": "ready"}})
    assert engine.verify({"type": "file_exists", "value": "${output_file}"}, ctx).passed
    assert engine.verify({"type": "variable_equals", "variable": "report.path", "value": "ready"}, ctx).passed
    assert engine.verify({"type": "variable_not_empty", "value": "report.path"}, ctx).passed
    passed, results = engine.verify_completion({
        "mode": "all",
        "conditions": [
            {"type": "file_exists", "value": "${output_file}"},
            {"type": "variable_not_empty", "value": "report.path"},
        ],
    }, ctx)
    assert passed and len(results) == 2
    passed, results = engine.verify_completion({
        "mode": "any",
        "conditions": [
            {"type": "file_exists", "value": "missing.txt"},
            {"type": "variable_not_empty", "value": "report.path"},
        ],
    }, ctx)
    assert passed and len(results) == 2


def _runner_with_registry(tmp_path: Path, project: RpaProject, registry: ToolRegistry, monkeypatch):
    import rpa.runner as runner_module
    from rpa.runner import ReplayRunner

    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    project.settings.start_delay = 0
    return ReplayRunner(project, tmp_path, lambda _message: None, tool_registry=registry)


def test_runner_integration_uses_mock_tool_registry_and_retries(tmp_path: Path, monkeypatch) -> None:
    attempts = []

    def execute(_inputs, _context):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("temporary failure")

    registry = ToolRegistry()
    registry.register("mock", FunctionTool("mock", "Mock integration tool", execute))
    project = RpaProject(actions=[RpaAction(
        "mock", {}, on_failure={"retry_count": 2, "retry_delay_seconds": 0},
    )])
    runner = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    runner.run(include_start_delay=False)
    assert len(attempts) == 3
    assert runner.total_attempts == 3
    assert runner.final_status == COMPLETED_UNVERIFIED


def test_fallback_executes_once_and_recovers_failed_step(tmp_path: Path, monkeypatch) -> None:
    calls = []
    registry = ToolRegistry()
    registry.register("primary", FunctionTool(
        "primary", "Always fails", lambda _inputs, _context: (_ for _ in ()).throw(RuntimeError("failed")),
    ))

    def fallback(_inputs, context):
        calls.append("fallback")
        context.variables["recovered"] = True

    registry.register("fallback", FunctionTool("fallback", "Fallback", fallback))
    project = RpaProject(actions=[RpaAction(
        "primary", {},
        expect={"type": "variable_equals", "variable": "recovered", "value": True},
        on_failure={
            "retry_count": 1, "retry_delay_seconds": 0,
            "fallback_step": {"action": "fallback"},
        },
    )])
    runner = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    runner.run(include_start_delay=False)
    assert calls == ["fallback"]
    assert runner.fallback_count == 1
    assert runner.step_results[0]["fallback_executed"] is True
    assert runner.step_results[0]["status"] == "Recovered"
    assert runner.final_status == RECOVERED


def test_human_skip_is_explicit_and_requires_attention(tmp_path: Path, monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register("fail", FunctionTool(
        "fail", "Always fails", lambda _inputs, _context: (_ for _ in ()).throw(RuntimeError("failed")),
    ))
    project = RpaProject(actions=[RpaAction("fail", {}, on_failure={"ask_user": True})])
    runner = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    runner.set_attention_callback(lambda _payload: runner.submit_attention_decision("skip"))
    runner.run(include_start_delay=False)
    assert runner.final_status == REQUIRES_ATTENTION
    assert runner.user_interventions[0]["decision"] == "skip"
    assert runner.step_results[0]["status"] == "Skipped"


def test_headless_human_escalation_reports_requires_attention(tmp_path: Path, monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register("fail", FunctionTool(
        "fail", "Always fails", lambda _inputs, _context: (_ for _ in ()).throw(RuntimeError("failed")),
    ))
    project = RpaProject(actions=[RpaAction("fail", {}, on_failure={"ask_user": True})])
    runner = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    with pytest.raises(Exception, match="failed"):
        runner.run(include_start_delay=False)
    assert runner.final_status == REQUIRES_ATTENTION
    assert runner.step_results[0]["status"] == "Requires Attention"


def test_completion_criteria_sets_verified_status_or_fails(tmp_path: Path, monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register("set", FunctionTool(
        "set", "Set completion value", lambda _inputs, context: context.variables.update({"done": True}),
    ))
    project = RpaProject(
        actions=[RpaAction("set", {})],
        success_when={
            "mode": "all",
            "conditions": [{"type": "variable_equals", "variable": "done", "value": True}],
        },
    )
    runner = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    runner.run(include_start_delay=False)
    assert runner.final_status == COMPLETED_VERIFIED
    assert runner.completion_result and runner.completion_result["passed"] is True

    project.success_when = {
        "mode": "all",
        "conditions": [{"type": "variable_equals", "variable": "done", "value": False}],
    }
    failed = _runner_with_registry(tmp_path, project, registry, monkeypatch)
    with pytest.raises(Exception, match="completion criteria"):
        failed.run(include_start_delay=False)
    assert failed.completion_result and failed.completion_result["passed"] is False


def test_step_editor_sections_and_table_indicators_are_visible() -> None:
    from PySide6.QtWidgets import QApplication
    from ui.action_editor import ActionEditor
    from ui.action_table import ActionTable

    app = QApplication.instance() or QApplication([])
    action = RpaAction(
        ActionType.WAIT.value, {"seconds": 0},
        expect={"type": "variable_not_empty", "value": "result"},
        on_failure={"retry_count": 2},
    )
    editor = ActionEditor()
    editor.set_action(action, None)
    assert editor.expected_button.text() == "Expected Result"
    assert editor.failure_button.text() == "Failure Handling"
    assert not editor.expected_button.isHidden()
    table = ActionTable()
    table.set_actions([action])
    assert "✓" in table.item(0, 0).text()
    assert "↻" in table.item(0, 0).text()
    editor.close(); table.close(); app.processEvents()


def test_flow_settings_store_completion_criteria() -> None:
    from PySide6.QtWidgets import QApplication, QDialog
    from ui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])
    project = RpaProject()
    dialog = SettingsDialog(project.settings, project=project)
    dialog.completion_enabled.setChecked(True)
    dialog.completion_mode.setCurrentIndex(dialog.completion_mode.findData("any"))
    dialog.completion_conditions.setPlainText('[{"type":"variable_not_empty","value":"result"}]')
    dialog.accept()
    assert dialog.result() == QDialog.Accepted
    assert project.success_when == {
        "mode": "any", "conditions": [{"type": "variable_not_empty", "value": "result"}],
    }
    dialog.close(); app.processEvents()
