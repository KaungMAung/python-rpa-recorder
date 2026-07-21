from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .models import ActionType, RpaAction, RpaProject, RuntimeInputDefinition
from .control_flow import CONTROL_TYPES, METADATA_TYPES, parse_control_flow, range_structure_issues
from .utils import MissingPlaceholderError, resolve_placeholders_strict
from .variables import VARIABLE_NAME_PATTERN, built_in_variables
from .windowing import normalize_window_target
from .project_manager import ProjectManager
from .subflows import mapping_dict, resolve_subflow_project, validate_subflow_dependencies

LEVEL_ERROR = "Error"
LEVEL_WARNING = "Warning"
LEVEL_INFO = "Info"

_WINDOW_ACTIONS = {
    ActionType.SELECT_WINDOW.value, ActionType.WAIT_WINDOW.value,
    ActionType.ACTIVATE_WINDOW.value, ActionType.MAXIMIZE_WINDOW.value,
    ActionType.MINIMIZE_WINDOW.value, ActionType.RESTORE_WINDOW.value,
    ActionType.CLOSE_WINDOW.value, ActionType.CLICK_WINDOW_RELATIVE.value,
    ActionType.MOVE_WINDOW_RELATIVE.value,
}


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    step_number: int
    step_name: str
    reason: str

    def message(self) -> str:
        return f"Step {self.step_number} {self.step_name}: {self.reason}"


def validate_project_detailed(
    project: RpaProject,
    project_dir: Path | None = None,
    start_index: int = 0,
    end_index: int | None = None,
    force_enabled: bool = False,
    runtime_variables: dict[str, Any] | None = None,
) -> list[ValidationIssue]:
    """Return structured validation results using original project step numbers."""
    issues: list[ValidationIssue] = []
    try:
        variables = dict(project.variables)
        variables.update(built_in_variables())
        for input_name, raw_definition in project.runtime_inputs.items():
            definition = (
                raw_definition if isinstance(raw_definition, RuntimeInputDefinition)
                else RuntimeInputDefinition.from_dict(raw_definition)
            )
            if definition.default not in (None, ""):
                variables[input_name] = definition.default
            elif definition.type.casefold() == "number":
                variables[input_name] = 0
            elif definition.type.casefold() == "date":
                variables[input_name] = "2000-01-01"
            elif definition.type.casefold() == "dropdown" and definition.options:
                variables[input_name] = definition.options[0]
            elif definition.type.casefold() == "folder" and project_dir:
                variables[input_name] = str(project_dir)
            elif definition.type.casefold() == "file":
                variables[input_name] = str(Path(__file__))
            else:
                variables[input_name] = "<runtime input>"
        if runtime_variables:
            variables.update(runtime_variables)
    except (TypeError, ValueError):
        variables = {}
    supported = {action.value for action in ActionType}
    end_index = len(project.actions) - 1 if end_index is None else min(end_index, len(project.actions) - 1)
    flow = parse_control_flow(project.actions)
    for issue in flow.issues + range_structure_issues(flow, start_index, end_index):
        action = project.actions[issue.step_number - 1] if 0 < issue.step_number <= len(project.actions) else None
        issues.append(ValidationIssue(
            issue.level, issue.step_number, _step_name(action) if action else "Control Flow", issue.reason,
        ))
    if project_dir:
        for step_number, reason in validate_subflow_dependencies(project, project_dir):
            action = project.actions[step_number - 1] if 0 < step_number <= len(project.actions) else None
            issues.append(ValidationIssue(
                LEVEL_ERROR, step_number, _step_name(action) if action else "Run Subflow", reason,
            ))
    for index, action in enumerate(project.actions):
        if action.action in CONTROL_TYPES and not action.enabled:
            issues.append(ValidationIssue(
                LEVEL_ERROR, index + 1, _step_name(action),
                "control steps cannot be disabled; remove the block or keep its structure enabled",
            ))
        if isinstance(action.data, dict) and str(action.data.get("failure_action", "")).lower() == "jump":
            target = _integer(action.data.get("failure_jump_step"))
            if target and 1 <= target <= len(project.actions):
                target_index = target - 1
                if flow.execution_depths[index] != flow.execution_depths[target_index] or project.actions[target_index].action in CONTROL_TYPES:
                    issues.append(ValidationIssue(
                        LEVEL_ERROR, index + 1, _step_name(action),
                        "failure jump target cannot enter, leave, or land on a control block",
                    ))

    seen: set[str] = set()
    selected_window_available = False
    for index, action in enumerate(project.actions):
        step_number = index + 1
        name = _step_name(action)
        if not isinstance(action.id, str) or not action.id.strip():
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, "step ID is required"))
        elif action.id in seen:
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, "step ID must be unique"))
        else:
            seen.add(action.id)

        in_range = start_index <= index <= end_index
        is_enabled = action.enabled or force_enabled
        if not in_range:
            continue
        if not is_enabled:
            issues.append(ValidationIssue(LEVEL_INFO, step_number, name, "disabled step will be skipped"))
            continue
        if not isinstance(action.action, str) or action.action not in supported:
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, f"unsupported action type: {action.action!r}"))
            continue
        if not isinstance(action.data, dict):
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, "action data is corrupted; expected an object"))
            continue
        if action.action == ActionType.COMMENT.value and not str(action.data.get("text", "")).strip():
            issues.append(ValidationIssue(LEVEL_WARNING, step_number, name, "comment is empty"))
            continue
        if action.action == ActionType.GROUP_START.value and not str(action.data.get("name", "")).strip():
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, "group name is required"))
            continue
        if action.action in METADATA_TYPES:
            continue

        try:
            resolved = resolve_placeholders_strict(action.data, variables)
        except MissingPlaceholderError as exc:
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, f"undefined variable: {exc.variable}"))
            resolved = action.data
        except (TypeError, ValueError) as exc:
            issues.append(ValidationIssue(LEVEL_ERROR, step_number, name, f"action data cannot be resolved: {exc}"))
            resolved = action.data

        if action.action == ActionType.RUN_SUBFLOW.value:
            raw_mappings = action.data.get("input_mappings")
            if isinstance(raw_mappings, dict):
                for parent_name in mapping_dict(raw_mappings).values():
                    if parent_name not in variables:
                        issues.append(ValidationIssue(
                            LEVEL_ERROR, step_number, name,
                            f"subflow input uses undefined parent variable: {parent_name}",
                        ))
        if action.action in {
            ActionType.GET_VARIABLE.value, ActionType.INCREMENT_VARIABLE.value,
            ActionType.APPEND_VARIABLE.value, ActionType.SET_OBJECT_PROPERTY.value,
            ActionType.DELETE_VARIABLE.value,
        }:
            variable_name = str(action.data.get("variable", "")).strip()
            if variable_name and variable_name not in variables:
                issues.append(ValidationIssue(
                    LEVEL_ERROR, step_number, name, f"undefined variable: {variable_name}",
                ))
        _validate_common(
            action, resolved, step_number, name, issues, len(project.actions), start_index + 1, end_index + 1,
        )
        _validate_action(action, resolved, project, project_dir, step_number, name, issues)
        if action.action in _WINDOW_ACTIONS:
            target = normalize_window_target(resolved)
            has_criteria = any(target[key] for key in ("process_name", "window_title", "class_name"))
            if bool(resolved.get("use_selected_window", False)) and not has_criteria and not selected_window_available:
                _add(
                    issues, LEVEL_ERROR, step_number, name,
                    "this step uses the selected window, but no earlier enabled Select / Target Window step defines it",
                )
            if action.action == ActionType.SELECT_WINDOW.value and has_criteria:
                selected_window_available = True
        _collect_created_variables(action, variables)
    return issues


def validate_project(project: RpaProject, project_dir: Path | None = None) -> list[str]:
    """Backward-compatible error-only validation API."""
    return [issue.message() for issue in validate_project_detailed(project, project_dir) if issue.level == LEVEL_ERROR]


def _step_name(action: RpaAction) -> str:
    try:
        return action.name.strip() or action.summary()
    except Exception:
        return str(getattr(action, "action", "Unknown step"))


def _add(issues: list[ValidationIssue], level: str, number: int, name: str, reason: str) -> None:
    issues.append(ValidationIssue(level, number, name, reason))


def _validate_common(
    action: RpaAction, data: dict[str, Any], number: int, name: str, issues: list[ValidationIssue],
    total_steps: int, run_start_step: int, run_end_step: int,
) -> None:
    for field_name, value in (("wait before", action.delay_before), ("recorded delay", action.recorded_delay)):
        numeric = _finite_number(value)
        if numeric is None or numeric < 0:
            _add(issues, LEVEL_ERROR, number, name, f"{field_name} must be a non-negative number")
    button = data.get("button")
    if button is not None and str(button) not in {"left", "right", "middle"}:
        _add(issues, LEVEL_ERROR, number, name, f"unsupported mouse button: {button!r}")
    retry_count = _integer(data.get("retry_count", 0))
    if retry_count is None or not 0 <= retry_count <= 100:
        _add(issues, LEVEL_ERROR, number, name, "retry count must be a whole number from 0 to 100")
    retry_delay = _finite_number(data.get("retry_delay", 1.0))
    if retry_delay is None or retry_delay < 0:
        _add(issues, LEVEL_ERROR, number, name, "retry delay must be a non-negative number")
    step_timeout = _finite_number(data.get("step_timeout", 0.0))
    if step_timeout is None or step_timeout < 0:
        _add(issues, LEVEL_ERROR, number, name, "step timeout must be a non-negative number")
    failure_action = str(data.get("failure_action", "stop")).strip().lower()
    if failure_action not in {"stop", "continue", "jump"}:
        _add(issues, LEVEL_ERROR, number, name, "failure action must be Stop Flow, Continue, or Jump to Step")
    elif failure_action == "jump":
        jump_step = _integer(data.get("failure_jump_step"))
        if jump_step is None or not 1 <= jump_step <= total_steps:
            _add(issues, LEVEL_ERROR, number, name, f"failure jump target must be between Step 1 and Step {total_steps}")
        elif not run_start_step <= jump_step <= run_end_step:
            _add(
                issues, LEVEL_ERROR, number, name,
                f"failure jump target Step {jump_step} is outside this run range",
            )
        elif jump_step == number:
            _add(issues, LEVEL_WARNING, number, name, "failure jump points back to the same step and may loop")
    capture = data.get("capture_failure_screenshot", False)
    if not isinstance(capture, bool):
        _add(issues, LEVEL_ERROR, number, name, "failure screenshot setting must be true or false")
    for field, label in (("capture_before", "before-step screenshot"), ("capture_after", "after-step screenshot")):
        if field in data and not isinstance(data[field], bool):
            _add(issues, LEVEL_ERROR, number, name, f"{label} setting must be true or false")
    output_name = str(data.get("output_variable", "")).strip()
    if output_name and not VARIABLE_NAME_PATTERN.fullmatch(output_name):
        _add(issues, LEVEL_ERROR, number, name, "output variable name is invalid")


def _validate_window_action(
    action_type: str, data: dict[str, Any], number: int, name: str,
    issues: list[ValidationIssue],
) -> None:
    target = normalize_window_target(data)
    has_criteria = any(target[key] for key in ("process_name", "window_title", "class_name"))
    if not has_criteria and not bool(data.get("use_selected_window", False)):
        _add(issues, LEVEL_ERROR, number, name, "choose a target window or use a previously selected window")
    if target["title_match"] not in {"exact", "contains", "regex"}:
        _add(issues, LEVEL_ERROR, number, name, "window title matching must be Exact, Contains, or Regular Expression")
    elif target["title_match"] == "regex" and target["window_title"]:
        try:
            re.compile(target["window_title"])
        except re.error as exc:
            _add(issues, LEVEL_ERROR, number, name, f"window title regular expression is invalid: {exc}")
    timeout = _finite_number(target["timeout"])
    if timeout is None or timeout < 0:
        _add(issues, LEVEL_ERROR, number, name, "window timeout must be a non-negative number")
    retry = _finite_number(target["retry_interval"])
    if retry is None or retry <= 0:
        _add(issues, LEVEL_ERROR, number, name, "window retry interval must be greater than zero")
    if target["multiple_match"] not in {"error", "first", "active"}:
        _add(issues, LEVEL_ERROR, number, name, "multiple-window handling must be Error, First Match, or Active Match")
    if target["process_name"] and ("/" in target["process_name"] or "\\" in target["process_name"]):
        _add(issues, LEVEL_WARNING, number, name, "use only the process filename, for example notepad.exe")
    if action_type in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value}:
        _validate_coordinates(data, ("relative_x", "relative_y"), number, name, issues, "window-relative position")
        if bool(data.get("scale_with_window", False)):
            width = _finite_number(data.get("original_window_width"))
            height = _finite_number(data.get("original_window_height"))
            if width is None or width <= 0 or height is None or height <= 0:
                _add(issues, LEVEL_ERROR, number, name, "resizing support needs the original window width and height")
        fallback = data.get("use_absolute_fallback", False)
        if not isinstance(fallback, bool):
            _add(issues, LEVEL_ERROR, number, name, "absolute coordinate fallback setting must be true or false")
        elif fallback:
            _validate_coordinates(data, ("fallback_x", "fallback_y"), number, name, issues, "absolute fallback position")
        if action_type == ActionType.CLICK_WINDOW_RELATIVE.value and str(data.get("button", "left")) not in {"left", "right", "middle"}:
            _add(issues, LEVEL_ERROR, number, name, "window-relative click uses an unsupported mouse button")
        if action_type == ActionType.MOVE_WINDOW_RELATIVE.value:
            duration = _finite_number(data.get("duration", 0.2))
            if duration is None or duration < 0:
                _add(issues, LEVEL_ERROR, number, name, "mouse move duration must be non-negative")


def _validate_action(
    action: RpaAction,
    data: dict[str, Any],
    project: RpaProject,
    project_dir: Path | None,
    number: int,
    name: str,
    issues: list[ValidationIssue],
) -> None:
    action_type = action.action
    if action_type in {
        ActionType.LAUNCH_APPLICATION.value, ActionType.WAIT_PROCESS.value,
        ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value,
        ActionType.READ_CLIPBOARD.value, ActionType.WRITE_CLIPBOARD.value,
        ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value,
        ActionType.DELETE_PATH.value, ActionType.WAIT_PATH.value,
        ActionType.RUN_POWERSHELL.value, ActionType.RUN_PYTHON_SCRIPT.value,
        ActionType.SHOW_NOTIFICATION.value,
    }:
        _validate_utility_action(action_type, data, project_dir, number, name, issues)
        return
    if action_type == ActionType.RUN_SUBFLOW.value:
        reference = str(data.get("project", "")).strip()
        if not reference:
            _add(issues, LEVEL_ERROR, number, name, "choose a target flow")
            return
        if Path(reference).is_absolute():
            _add(issues, LEVEL_ERROR, number, name, "subflow reference must be relative so the project remains portable")
            return
        for key, label in (("input_mappings", "input mappings"), ("output_mappings", "output mappings")):
            if not isinstance(data.get(key, {}), dict):
                _add(issues, LEVEL_ERROR, number, name, f"subflow {label} must be an object")
        if not project_dir:
            return
        try:
            target = resolve_subflow_project(project_dir, reference)
            child = ProjectManager().load(target)
        except (OSError, ValueError, TypeError):
            return  # Dependency validation reports the precise load error.
        known_inputs = set(child.variables) | set(child.runtime_inputs)
        for child_name in mapping_dict(data.get("input_mappings")).keys():
            if child_name not in known_inputs:
                _add(issues, LEVEL_ERROR, number, name, f"subflow input is not defined by the target flow: {child_name}")
        known_outputs = set(child.output_variables)
        for child_name, parent_name in mapping_dict(data.get("output_mappings")).items():
            if child_name not in known_outputs:
                _add(issues, LEVEL_ERROR, number, name, f"subflow output is not declared by the target flow: {child_name}")
            if not VARIABLE_NAME_PATTERN.fullmatch(parent_name):
                _add(issues, LEVEL_ERROR, number, name, f"invalid parent output variable name: {parent_name}")
        return
    if action_type in _WINDOW_ACTIONS:
        _validate_window_action(action_type, data, number, name, issues)
        return
    fixed_conditions = {
        ActionType.IF_IMAGE_EXISTS.value: "image_exists",
        ActionType.IF_IMAGE_NOT_EXISTS.value: "image_not_exists",
        ActionType.IF_WINDOW_EXISTS.value: "window_exists",
        ActionType.IF_PATH_EXISTS.value: "path_exists",
        ActionType.IF_VARIABLE.value: "variable",
    }
    if action_type in fixed_conditions:
        _validate_condition_data(data, project_dir, number, name, issues, fixed_conditions[action_type])
        return
    if action_type == ActionType.REPEAT_COUNT.value:
        count = _integer(data.get("count"))
        if count is None or count < 0:
            _add(issues, LEVEL_ERROR, number, name, "repeat count must be a non-negative whole number")
        elif count > 10000:
            _add(issues, LEVEL_WARNING, number, name, "repeat count is very high and may take a long time")
        return
    if action_type == ActionType.REPEAT_UNTIL.value:
        _validate_condition_data(data, project_dir, number, name, issues, str(data.get("condition_type", "variable")))
        maximum = _integer(data.get("max_iterations", 1000))
        if maximum is None or maximum < 1:
            _add(issues, LEVEL_ERROR, number, name, "Repeat Until needs a maximum iteration limit of at least 1")
        elif maximum > 10000:
            _add(issues, LEVEL_WARNING, number, name, "maximum iterations is very high and risks a long-running loop")
        delay = _finite_number(data.get("iteration_delay", 0.0))
        if delay is None or delay < 0:
            _add(issues, LEVEL_ERROR, number, name, "loop iteration delay must be non-negative")
        _add(issues, LEVEL_INFO, number, name, f"loop safety limit: {maximum or '?'} iterations")
        return
    if action_type in {
        ActionType.ELSE.value, ActionType.END_IF.value,
        ActionType.END_LOOP.value, ActionType.BREAK_LOOP.value,
    }:
        return
    image_actions = {ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value}
    if action_type in image_actions:
        image = str(data.get("image") or "").strip()
        references = data.get("reference_images", [])
        if references is None:
            references = []
        if not isinstance(references, list) or any(not isinstance(value, str) or not value.strip() for value in references):
            _add(issues, LEVEL_ERROR, number, name, "reference images must be an ordered list of image paths")
            references = []
        ordered_images = [image] + [str(value).strip() for value in references if str(value).strip() != image]
        if not image:
            _add(issues, LEVEL_ERROR, number, name, "target screenshot is required")
        for reference_index, reference in enumerate(ordered_images):
            if reference and project_dir and not _resolve_path(reference, project_dir).is_file():
                label = "target screenshot" if reference_index == 0 else f"reference image {reference_index + 1}"
                _add(issues, LEVEL_ERROR, number, name, f"{label} is missing: {reference}")
        if len(set(ordered_images)) != len(ordered_images):
            _add(issues, LEVEL_WARNING, number, name, "duplicate reference images will be ignored")
        confidence = _finite_number(data.get("confidence", project.settings.default_confidence))
        if confidence is None or not 0 < confidence <= 1:
            _add(issues, LEVEL_ERROR, number, name, "image confidence must be greater than 0 and at most 1")
        elif confidence < 0.5:
            _add(issues, LEVEL_WARNING, number, name, "image confidence is very low and may match the wrong target")
        timeout = _finite_number(data.get("timeout", project.settings.default_timeout))
        if timeout is None or timeout <= 0:
            _add(issues, LEVEL_ERROR, number, name, "image timeout must be greater than 0 seconds")
        if data.get("use_coordinate_fallback", True):
            _validate_coordinates(data, ("fallback_x", "fallback_y"), number, name, issues, "fallback position")
        if "grayscale" in data and not isinstance(data.get("grayscale"), bool):
            _add(issues, LEVEL_ERROR, number, name, "grayscale matching setting must be true or false")
        priority = str(data.get("match_priority", "highest_confidence"))
        if priority not in {"highest_confidence", "leftmost", "rightmost", "topmost", "bottommost", "match_index"}:
            _add(issues, LEVEL_ERROR, number, name, "image match priority is unsupported")
        match_index = _integer(data.get("match_index", 1))
        if match_index is None or match_index < 1:
            _add(issues, LEVEL_ERROR, number, name, "selected match number must be at least 1")
        region = data.get("search_region")
        if region not in (None, {}) and not isinstance(region, dict):
            _add(issues, LEVEL_ERROR, number, name, "search region must contain X, Y, width, and height")
        elif isinstance(region, dict) and region:
            values = [_finite_number(region.get(key)) for key in ("x", "y", "width", "height")]
            if any(value is None for value in values) or values[2] <= 0 or values[3] <= 0:
                _add(issues, LEVEL_ERROR, number, name, "search region needs valid coordinates and a positive width and height")
        return

    if action_type in {ActionType.CLICK_COORDINATE.value, ActionType.MOUSE_MOVE.value}:
        _validate_coordinates(data, ("x", "y"), number, name, issues, "position")
    elif action_type == ActionType.DRAG.value:
        _validate_coordinates(data, ("start_x", "start_y"), number, name, issues, "start position")
        _validate_coordinates(data, ("end_x", "end_y"), number, name, issues, "end position")
    elif action_type == ActionType.SCROLL.value:
        amount = _integer(data.get("amount"))
        if amount is None:
            _add(issues, LEVEL_ERROR, number, name, "scroll amount must be a whole number")
        elif amount == 0:
            _add(issues, LEVEL_WARNING, number, name, "scroll amount is zero, so this step will do nothing")
        if data.get("move_to"):
            _validate_coordinates(data, ("x", "y"), number, name, issues, "scroll position")
    elif action_type == ActionType.TYPE_TEXT.value:
        if "text" not in data or not str(data.get("text", "")).strip():
            _add(issues, LEVEL_ERROR, number, name, "text is required")
        interval = _finite_number(data.get("interval", project.settings.typing_interval))
        if interval is None or interval < 0:
            _add(issues, LEVEL_ERROR, number, name, "typing interval must be a non-negative number")
    elif action_type == ActionType.PRESS_KEY.value:
        if not str(data.get("key") or "").strip():
            _add(issues, LEVEL_ERROR, number, name, "key is required")
        count = _integer(data.get("count", 1))
        if count is None or count < 1:
            _add(issues, LEVEL_ERROR, number, name, "key press count must be at least 1")
    elif action_type == ActionType.HOTKEY.value:
        keys = data.get("keys")
        if not isinstance(keys, (list, tuple)) or not keys or any(not str(key).strip() for key in keys):
            _add(issues, LEVEL_ERROR, number, name, "at least one shortcut key is required")
    elif action_type == ActionType.WAIT.value:
        seconds = _finite_number(data.get("seconds", action.delay_before))
        if seconds is None or seconds < 0:
            _add(issues, LEVEL_ERROR, number, name, "wait duration must be a non-negative number")
    elif action_type == ActionType.OPEN_FILE.value:
        path = str(data.get("path") or "").strip()
        if not path:
            _add(issues, LEVEL_ERROR, number, name, "application or file path is required")
        elif project_dir and not _path_exists(path, project_dir):
            _add(issues, LEVEL_ERROR, number, name, f"application, script, or file is missing: {path}")
    elif action_type in {ActionType.RUN_PYTHON.value, ActionType.PYTHON_CODE.value}:
        code = str(data.get("code") or "")
        if not code.strip():
            _add(issues, LEVEL_ERROR, number, name, "Python code is required")
        else:
            try:
                compile(code, f"step_{number}", "exec")
            except (SyntaxError, ValueError) as exc:
                reason = getattr(exc, "msg", str(exc))
                _add(issues, LEVEL_ERROR, number, name, f"Python code is invalid: {reason}")
    elif action_type in {
        ActionType.SET_VARIABLE.value, ActionType.GET_VARIABLE.value,
        ActionType.INCREMENT_VARIABLE.value, ActionType.APPEND_VARIABLE.value,
        ActionType.SET_OBJECT_PROPERTY.value, ActionType.DELETE_VARIABLE.value,
    }:
        variable = str(data.get("variable", "")).strip()
        if not VARIABLE_NAME_PATTERN.fullmatch(variable):
            _add(issues, LEVEL_ERROR, number, name, "a valid variable name is required")
        if action_type == ActionType.SET_OBJECT_PROPERTY.value and not str(data.get("property", "")).strip():
            _add(issues, LEVEL_ERROR, number, name, "object property is required")


def _validate_condition_data(
    data: dict[str, Any], project_dir: Path | None, number: int, name: str,
    issues: list[ValidationIssue], condition_type: str,
) -> None:
    if condition_type in {"image_exists", "image_not_exists"}:
        image = str(data.get("image") or "").strip()
        if not image:
            _add(issues, LEVEL_ERROR, number, name, "condition image is required")
        elif project_dir and not _resolve_path(image, project_dir).is_file():
            _add(issues, LEVEL_ERROR, number, name, f"condition image is missing: {image}")
        confidence = _finite_number(data.get("confidence", 0.86))
        if confidence is None or not 0 < confidence <= 1:
            _add(issues, LEVEL_ERROR, number, name, "image confidence must be greater than 0 and at most 1")
    elif condition_type == "window_exists":
        if not str(data.get("window_title") or "").strip():
            _add(issues, LEVEL_ERROR, number, name, "window title is required")
    elif condition_type == "path_exists":
        if not str(data.get("path") or "").strip():
            _add(issues, LEVEL_ERROR, number, name, "file or folder path is required")
        if str(data.get("path_type", "either")) not in {"either", "file", "folder"}:
            _add(issues, LEVEL_ERROR, number, name, "path type must be File, Folder, or Either")
    elif condition_type == "variable":
        if not str(data.get("variable") or "").strip():
            _add(issues, LEVEL_ERROR, number, name, "variable name is required")
        operator = str(data.get("operator", "equals"))
        if operator not in {"equals", "contains", "is_empty"}:
            _add(issues, LEVEL_ERROR, number, name, "variable comparison must be Equals, Contains, or Is Empty")
    else:
        _add(issues, LEVEL_ERROR, number, name, f"unsupported condition type: {condition_type}")


def _validate_utility_action(
    action_type: str, data: dict[str, Any], project_dir: Path | None,
    number: int, name: str, issues: list[ValidationIssue],
) -> None:
    def path_value(key: str) -> tuple[str, Path | None]:
        raw = str(data.get(key, "")).strip()
        return raw, (_resolve_path(raw, project_dir) if raw and project_dir else None)

    if action_type == ActionType.LAUNCH_APPLICATION.value:
        raw, path = path_value("path")
        if not raw:
            _add(issues, LEVEL_ERROR, number, name, "choose an application to launch")
        elif path and not path.is_file() and shutil.which(raw) is None:
            _add(issues, LEVEL_ERROR, number, name, f"application is missing: {raw}")
    elif action_type in {ActionType.WAIT_PROCESS.value, ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value}:
        process = str(data.get("process_name", "")).strip()
        if not process:
            _add(issues, LEVEL_ERROR, number, name, "process name is required")
        elif "/" in process or "\\" in process:
            _add(issues, LEVEL_WARNING, number, name, "use the process filename, for example notepad.exe")
    elif action_type == ActionType.READ_CLIPBOARD.value:
        if not str(data.get("output_variable", "")).strip():
            _add(issues, LEVEL_ERROR, number, name, "choose an output variable for the clipboard text")
    elif action_type == ActionType.WRITE_CLIPBOARD.value:
        if "text" not in data:
            _add(issues, LEVEL_ERROR, number, name, "clipboard text is required")
    elif action_type in {ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value}:
        source_raw, source = path_value("source")
        destination_raw, destination = path_value("destination")
        if not source_raw:
            _add(issues, LEVEL_ERROR, number, name, "choose the source file or folder")
        elif source and not source.exists():
            _add(issues, LEVEL_ERROR, number, name, f"source is missing: {source_raw}")
        if not destination_raw:
            _add(issues, LEVEL_ERROR, number, name, "choose the destination path")
        elif destination and destination.exists():
            _add(issues, LEVEL_WARNING, number, name, f"destination already exists: {destination_raw}")
        if destination and destination.parent.exists() and not os.access(destination.parent, os.W_OK):
            _add(issues, LEVEL_ERROR, number, name, f"destination folder is not writable: {destination.parent}")
    elif action_type == ActionType.DELETE_PATH.value:
        raw, path = path_value("path")
        if not raw:
            _add(issues, LEVEL_ERROR, number, name, "choose the file or folder to delete")
        elif path and not path.exists():
            _add(issues, LEVEL_ERROR, number, name, f"delete target is missing: {raw}")
        elif path and not os.access(path.parent, os.W_OK):
            _add(issues, LEVEL_ERROR, number, name, f"delete target cannot be modified with current permissions: {raw}")
    elif action_type == ActionType.WAIT_PATH.value:
        raw, _path = path_value("path")
        if not raw:
            _add(issues, LEVEL_ERROR, number, name, "enter the file or folder to wait for")
        if str(data.get("path_type", "either")) not in {"file", "folder", "either"}:
            _add(issues, LEVEL_ERROR, number, name, "path type must be File, Folder, or Either")
    elif action_type == ActionType.RUN_POWERSHELL.value:
        if not str(data.get("command", "")).strip():
            _add(issues, LEVEL_ERROR, number, name, "PowerShell command is required")
        if os.name == "nt" and shutil.which("powershell.exe") is None and shutil.which("powershell") is None:
            _add(issues, LEVEL_ERROR, number, name, "PowerShell executable was not found")
    elif action_type == ActionType.RUN_PYTHON_SCRIPT.value:
        raw, path = path_value("path")
        if not raw:
            _add(issues, LEVEL_ERROR, number, name, "choose a Python script")
        elif path and (not path.is_file() or path.suffix.casefold() not in {".py", ".pyw"}):
            _add(issues, LEVEL_ERROR, number, name, f"Python script is missing or invalid: {raw}")
    elif action_type == ActionType.SHOW_NOTIFICATION.value:
        if not str(data.get("message", "")).strip():
            _add(issues, LEVEL_ERROR, number, name, "notification message is required")

    if action_type in {ActionType.WAIT_PROCESS.value, ActionType.WAIT_PATH.value, ActionType.RUN_POWERSHELL.value, ActionType.RUN_PYTHON_SCRIPT.value}:
        timeout = _finite_number(data.get("timeout", 30.0))
        if timeout is None or timeout <= 0:
            _add(issues, LEVEL_ERROR, number, name, "operation timeout must be greater than zero")
    if "working_directory" in data and str(data.get("working_directory", "")).strip() and project_dir:
        working = _resolve_path(str(data["working_directory"]), project_dir)
        if not working.is_dir():
            _add(issues, LEVEL_ERROR, number, name, f"working directory is missing: {data['working_directory']}")
    for field, label in (("stderr_variable", "stderr variable"), ("exit_code_variable", "exit-code variable")):
        value = str(data.get(field, "")).strip()
        if value and not VARIABLE_NAME_PATTERN.fullmatch(value):
            _add(issues, LEVEL_ERROR, number, name, f"{label} name is invalid")


def _validate_coordinates(
    data: dict[str, Any], keys: tuple[str, str], number: int, name: str,
    issues: list[ValidationIssue], label: str,
) -> None:
    for key in keys:
        value = _integer(data.get(key))
        if value is None:
            _add(issues, LEVEL_ERROR, number, name, f"{label} requires valid X and Y coordinates")
            return
        # Negative values are valid on monitors positioned left/above the primary display.
        if abs(value) > 1_000_000:
            _add(issues, LEVEL_ERROR, number, name, f"{label} coordinate is outside the supported range")
            return


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or not numeric.is_integer():
        return None
    return int(numeric)


def _resolve_path(value: str, project_dir: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else Path(project_dir) / path


def _path_exists(value: str, project_dir: Path) -> bool:
    return _resolve_path(value, project_dir).exists() or shutil.which(value) is not None


def _collect_created_variables(action: RpaAction, variables: dict[str, Any]) -> None:
    output_name = str(action.data.get("output_variable", "")).strip() if isinstance(action.data, dict) else ""
    if output_name:
        variables.setdefault(output_name, 0)
    if action.action == ActionType.SET_VARIABLE.value and isinstance(action.data, dict):
        name = str(action.data.get("variable", "")).strip()
        if name:
            variables[name] = action.data.get("value")
    if action.action == ActionType.RUN_SUBFLOW.value and isinstance(action.data, dict):
        for parent_name in mapping_dict(action.data.get("output_mappings")).values():
            variables.setdefault(parent_name, 0)
    if isinstance(action.data, dict):
        for field in ("stderr_variable", "exit_code_variable"):
            name = str(action.data.get(field, "")).strip()
            if name:
                variables.setdefault(name, 0)
    if action.action not in {ActionType.RUN_PYTHON.value, ActionType.PYTHON_CODE.value}:
        return
    code = str(action.data.get("code", "")) if isinstance(action.data, dict) else ""
    for match in re.finditer(r"variables\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]\s*=", code):
        # The exact runtime value is unknowable during static validation, but
        # it is defined for following steps if this assignment executes.
        variables.setdefault(match.group(1), 0)
