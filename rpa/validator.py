from __future__ import annotations

from pathlib import Path
import re

from .models import ActionType, RpaProject
from .utils import MissingPlaceholderError, resolve_placeholders_strict


def validate_project(project: RpaProject, project_dir: Path | None = None) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    variables = dict(project.variables)
    for index, action in enumerate(project.actions, start=1):
        name = action.friendly_name()
        if not action.id:
            errors.append(f"Step {index} {name}: id is required")
        elif action.id in seen:
            errors.append(f"Step {index} {name}: id must be unique")
        seen.add(action.id)
        if not action.enabled:
            continue
        data = action.data
        if action.action == ActionType.PYTHON_CODE.value and not str(data.get("code", "")).strip():
            errors.append(f"Step {index} {name}: code is required")
        if action.action == ActionType.TYPE_TEXT.value and "text" not in data:
            errors.append(f"Step {index} {name}: text is required")
        if action.action == ActionType.PRESS_KEY.value and not data.get("key"):
            errors.append(f"Step {index} {name}: key is required")
        if action.action == ActionType.HOTKEY.value and not data.get("keys"):
            errors.append(f"Step {index} {name}: keys are required")
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            image = data.get("image")
            if not image:
                errors.append(f"Step {index} {name}: image is required")
            elif project_dir and not (Path(project_dir) / str(image)).exists():
                errors.append(f"Step {index} {name}: image file is missing: {image}")
        try:
            resolve_placeholders_strict(data, variables)
        except MissingPlaceholderError as exc:
            errors.append(f"Step {index} {name}: missing variable {exc.variable}")
        if action.action == ActionType.PYTHON_CODE.value:
            for match in re.finditer(r"variables\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]\s*=", str(data.get("code", ""))):
                variables.setdefault(match.group(1), "")
    return errors
