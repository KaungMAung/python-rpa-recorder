from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
import pytest

import rpa.runner as runner_module
from rpa.generator import generate_python
from rpa.models import ActionType, ProjectSettings, RpaAction, RpaProject
from rpa.runner import ReplayRunner, StopReplay
from rpa.validator import LEVEL_ERROR, validate_project_detailed
from ui.dialogs import ManualActionDialog


def _runner(tmp_path: Path, actions: list[RpaAction], logs=None) -> ReplayRunner:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    return ReplayRunner(RpaProject(actions=actions), tmp_path, (logs or []).append)


def test_file_utility_actions_and_wait_path(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")
    actions = [
        RpaAction(ActionType.COPY_PATH.value, {"source": "source.txt", "destination": "copied.txt", "output_variable": "COPIED"}),
        RpaAction(ActionType.MOVE_PATH.value, {"source": "copied.txt", "destination": "moved.txt"}),
        RpaAction(ActionType.RENAME_PATH.value, {"source": "moved.txt", "destination": "renamed.txt"}),
        RpaAction(ActionType.WAIT_PATH.value, {"path": "renamed.txt", "path_type": "file", "timeout": 1}),
        RpaAction(ActionType.DELETE_PATH.value, {"path": "renamed.txt"}),
    ]
    runner = _runner(tmp_path, actions)
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["COPIED"] == str(tmp_path / "copied.txt")
    assert not (tmp_path / "renamed.txt").exists()
    assert all(result["status"] == "Success" for result in runner.step_results)
    assert runner.step_results[0]["utility_result"]["operation"] == ActionType.COPY_PATH.value


def test_python_script_captures_outputs_exit_code_duration_and_stop(tmp_path: Path) -> None:
    script = tmp_path / "utility.py"
    script.write_text("import sys\nprint('hello')\nprint('warning', file=sys.stderr)\n", encoding="utf-8")
    action = RpaAction(ActionType.RUN_PYTHON_SCRIPT.value, {
        "path": "utility.py", "timeout": 5,
        "output_variable": "OUT", "stderr_variable": "ERR", "exit_code_variable": "CODE",
        "sensitive": True,
    })
    runner = _runner(tmp_path, [action])
    runner.run(include_start_delay=False)
    result = runner.step_results[0]["utility_result"]
    assert runner.runtime_variables["OUT"] == "hello"
    assert runner.runtime_variables["ERR"] == "warning"
    assert runner.runtime_variables["CODE"] == 0
    assert result["command"] == "[REDACTED]"
    assert result["stdout"] == "hello\n"
    assert result["stderr"] == "warning\n"
    assert result["duration_seconds"] >= 0

    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    stopped = _runner(tmp_path, [RpaAction(ActionType.RUN_PYTHON_SCRIPT.value, {"path": "slow.py", "timeout": 30})])
    error: list[Exception] = []
    thread = threading.Thread(target=lambda: _run_and_capture(stopped, error))
    thread.start(); time.sleep(0.15); stopped.request_stop(); thread.join(timeout=2)
    assert not thread.is_alive()
    assert error and isinstance(error[0], StopReplay)


@pytest.mark.skipif(os.name != "nt", reason="PowerShell utility is Windows-specific")
def test_powershell_command_captures_streams(tmp_path: Path) -> None:
    action = RpaAction(ActionType.RUN_POWERSHELL.value, {
        "command": "Write-Output 'native-ok'; [Console]::Error.WriteLine('native-warning')",
        "timeout": 10, "output_variable": "OUT", "stderr_variable": "ERR",
        "exit_code_variable": "CODE",
    })
    runner = _runner(tmp_path, [action])
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["OUT"] == "native-ok"
    assert "native-warning" in runner.runtime_variables["ERR"]
    assert runner.runtime_variables["CODE"] == 0


def _run_and_capture(runner: ReplayRunner, errors: list[Exception]) -> None:
    try:
        runner.run(include_start_delay=False)
    except Exception as exc:
        errors.append(exc)


def test_utility_retry_outputs_validation_ui_and_generation(tmp_path: Path, monkeypatch) -> None:
    attempts = {"count": 0}

    def clipboard() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise PermissionError("busy")
        return "ready"

    monkeypatch.setattr(runner_module, "read_clipboard_text", clipboard)
    action = RpaAction(ActionType.READ_CLIPBOARD.value, {
        "output_variable": "CLIP", "retry_count": 1, "retry_delay": 0,
    })
    runner = _runner(tmp_path, [action])
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["CLIP"] == "ready"
    assert runner.step_results[0]["attempts"] == 2

    invalid = RpaProject(actions=[
        RpaAction(ActionType.RUN_PYTHON_SCRIPT.value, {"path": "missing.py", "timeout": 0}),
        RpaAction(ActionType.RUN_POWERSHELL.value, {"command": "", "timeout": 10}),
        RpaAction(ActionType.COPY_PATH.value, {"source": "missing", "destination": "out"}),
    ])
    reasons = [issue.reason for issue in validate_project_detailed(invalid, tmp_path) if issue.level == LEVEL_ERROR]
    assert any("Python script is missing" in reason for reason in reasons)
    assert any("timeout" in reason for reason in reasons)
    assert any("PowerShell command is required" in reason for reason in reasons)
    assert any("source is missing" in reason for reason in reasons)

    app = QApplication.instance() or QApplication([])
    dialog = ManualActionDialog(ProjectSettings(), {"INPUT": "value"}, project_dir=tmp_path)
    dialog.type_box.setCurrentIndex(dialog.type_box.findData(ActionType.RUN_POWERSHELL.value))
    dialog.utility_editor.controls["command"].setPlainText("Write-Output {{INPUT}}")
    built = dialog.action()
    assert built.action == ActionType.RUN_POWERSHELL.value
    assert built.data["command"] == "Write-Output {{INPUT}}"
    generated = generate_python(RpaProject(variables={"INPUT": "value"}, actions=[built]), tmp_path)
    text = generated.read_text(encoding="utf-8")
    compile(text, str(generated), "exec")
    assert "run_utility_action('run_powershell'" in text
    dialog.close(); app.processEvents()
