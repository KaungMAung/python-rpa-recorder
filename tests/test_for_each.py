from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import rpa.runner as runner_module
from rpa.control_flow import parse_control_flow
from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.runner import ReplayRunner


def _runner(tmp_path: Path, actions: list[RpaAction], logs=None) -> ReplayRunner:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    sink = logs.append if logs is not None else (lambda _message: None)
    return ReplayRunner(RpaProject(actions=actions), tmp_path, sink)


def _collect(list_var: str, item_var: str) -> list[RpaAction]:
    return [
        RpaAction(ActionType.PYTHON_CODE.value, {"code": f"variables['{list_var}'] = ['a', 'b', 'c']"}),
        RpaAction(ActionType.FOR_EACH.value, {
            "list_variable": list_var, "item_variable": item_var,
            "max_iterations": 1000, "failure_mode": "stop",
        }),
        RpaAction(ActionType.PYTHON_CODE.value, {
            "code": f"variables['seen'] = variables.get('seen', []) + [variables['{item_var}']]"
        }),
        RpaAction(ActionType.END_LOOP.value, {}),
    ]


def test_for_each_iterates_all_items(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _collect("items", "current_item"))
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["seen"] == ["a", "b", "c"]
    assert runner.runtime_variables["is_last_item"] is True
    assert runner.runtime_variables["loop_number"] == 3


def test_for_each_empty_list_skips_body(tmp_path: Path) -> None:
    logs: list[str] = []
    actions = [
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['items'] = []"}),
        RpaAction(ActionType.FOR_EACH.value, {
            "list_variable": "items", "item_variable": "current_item",
            "max_iterations": 1000, "failure_mode": "stop",
        }),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['ran'] = True"}),
        RpaAction(ActionType.END_LOOP.value, {}),
    ]
    runner = _runner(tmp_path, actions, logs)
    runner.run(include_start_delay=False)
    assert "ran" not in runner.runtime_variables
    assert any("List is empty" in line for line in logs)


def test_for_each_in_loop_types_and_pairs_with_end_loop() -> None:
    actions = _collect("items", "current_item")
    flow = parse_control_flow(actions)
    assert flow.loop_end[1] == 3


def test_for_each_generates_python(tmp_path: Path) -> None:
    project = RpaProject(actions=_collect("items", "current_item"))
    path = generate_python(project, tmp_path)
    code = path.read_text(encoding="utf-8")
    assert "for " in code
    assert "enumerate(" in code
    compile(code, "<generated>", "exec")
