from __future__ import annotations

from pathlib import Path

from rpa.models import ActionType, RpaAction, RpaProject
from rpa.validator import LEVEL_ERROR, LEVEL_INFO, LEVEL_WARNING, validate_project, validate_project_detailed


def reasons(project: RpaProject, project_dir: Path) -> list[str]:
    return [issue.reason for issue in validate_project_detailed(project, project_dir)]


def test_validator_reports_missing_files_coordinates_fields_and_variables(tmp_path: Path) -> None:
    project = RpaProject(actions=[
        RpaAction(ActionType.CLICK_IMAGE.value, {
            "image": "screenshots/missing.png", "confidence": 2, "timeout": 0,
            "use_coordinate_fallback": True, "fallback_x": "bad", "fallback_y": 10,
        }, name="Click Submit"),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "{{customer}}"}, name="Enter customer"),
        RpaAction(ActionType.OPEN_FILE.value, {"path": "missing-tool.exe"}, name="Open tool"),
    ])
    issues = validate_project_detailed(project, tmp_path)
    assert all(issue.level == LEVEL_ERROR for issue in issues)
    assert all(issue.step_number >= 1 and issue.step_name and issue.reason for issue in issues)
    text = "\n".join(issue.reason for issue in issues)
    assert "screenshot is missing" in text
    assert "confidence" in text
    assert "timeout" in text
    assert "coordinates" in text
    assert "undefined variable" in text
    assert "application, script, or file is missing" in text


def test_validator_accepts_negative_multimonitor_coordinates(tmp_path: Path) -> None:
    project = RpaProject(actions=[RpaAction(ActionType.DRAG.value, {
        "start_x": -1920, "start_y": -200, "end_x": 300, "end_y": 400,
    })])
    assert validate_project(project, tmp_path) == []


def test_validator_reports_unsupported_and_corrupted_actions(tmp_path: Path) -> None:
    unsupported = RpaAction("future_action", {})
    corrupted = RpaAction(ActionType.WAIT.value, {})
    corrupted.data = "not an object"  # type: ignore[assignment]
    project = RpaProject(actions=[unsupported, corrupted])
    text = "\n".join(reasons(project, tmp_path))
    assert "unsupported action type" in text
    assert "action data is corrupted" in text


def test_validator_emits_warning_and_info_levels(tmp_path: Path) -> None:
    image = tmp_path / "screenshots" / "target.png"
    image.parent.mkdir()
    image.write_bytes(b"image")
    project = RpaProject(actions=[
        RpaAction(ActionType.CLICK_IMAGE.value, {
            "image": "screenshots/target.png", "confidence": 0.3, "timeout": 2,
            "use_coordinate_fallback": False,
        }),
        RpaAction(ActionType.WAIT.value, {"seconds": 1}, enabled=False),
    ])
    issues = validate_project_detailed(project, tmp_path)
    assert any(issue.level == LEVEL_WARNING for issue in issues)
    assert any(issue.level == LEVEL_INFO for issue in issues)


def test_invalid_python_and_empty_required_fields_are_errors(tmp_path: Path) -> None:
    project = RpaProject(actions=[
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "if:"}),
        RpaAction(ActionType.PRESS_KEY.value, {"key": ""}),
        RpaAction(ActionType.HOTKEY.value, {"keys": []}),
        RpaAction(ActionType.WAIT.value, {"seconds": -1}),
    ])
    text = "\n".join(reasons(project, tmp_path))
    assert "Python code is invalid" in text
    assert "key is required" in text
    assert "shortcut key is required" in text
    assert "wait duration" in text


def test_retry_and_failure_settings_are_validated(tmp_path: Path) -> None:
    project = RpaProject(actions=[RpaAction(ActionType.WAIT.value, {
        "seconds": 1,
        "retry_count": -1,
        "retry_delay": -2,
        "step_timeout": -3,
        "failure_action": "jump",
        "failure_jump_step": 9,
        "capture_failure_screenshot": "yes",
    })])
    text = "\n".join(reasons(project, tmp_path))
    assert "retry count" in text
    assert "retry delay" in text
    assert "step timeout" in text
    assert "jump target" in text
    assert "screenshot setting" in text
