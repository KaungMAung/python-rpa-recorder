from __future__ import annotations

from pathlib import Path

from rpa.execution import ExecutionContext
from rpa.models import RpaAction, RpaProject
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

