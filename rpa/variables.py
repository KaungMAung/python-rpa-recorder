"""Variable categories, built-ins, runtime input coercion, and secret masking."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Any

from .models import RpaProject, RuntimeInputDefinition

VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INPUT_TYPES = ("text", "number", "date", "dropdown", "password", "file", "folder")


def built_in_variables(now: datetime | None = None, clipboard_text: str = "") -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    return {
        "RUN_DATE": now.date().isoformat(),
        "CLIPBOARD_TEXT": clipboard_text,
        "LAST_CLICK_X": 0,
        "LAST_CLICK_Y": 0,
    }


def prepare_runtime_variables(
    project: RpaProject,
    supplied: dict[str, Any] | None = None,
    clipboard_text: str = "",
    now: datetime | None = None,
    validate_paths: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    supplied = supplied or {}
    values: dict[str, Any] = dict(getattr(project, "variables", {}) or {})
    values.update(built_in_variables(now, clipboard_text))
    errors: list[str] = []
    for name, raw_definition in (getattr(project, "runtime_inputs", {}) or {}).items():
        definition = (
            raw_definition if isinstance(raw_definition, RuntimeInputDefinition)
            else RuntimeInputDefinition.from_dict(raw_definition)
        )
        raw = supplied[name] if name in supplied else definition.default
        value, error = coerce_runtime_input(name, definition, raw, validate_paths)
        if error:
            errors.append(error)
        else:
            values[name] = value
    return values, errors


def coerce_runtime_input(
    name: str,
    definition: RuntimeInputDefinition,
    raw: Any,
    validate_paths: bool = True,
) -> tuple[Any, str | None]:
    kind = definition.type.casefold()
    if kind not in INPUT_TYPES:
        return raw, f"{name}: unsupported input type '{definition.type}'"
    missing = raw is None or (isinstance(raw, str) and not raw.strip())
    if missing:
        if definition.required:
            return None, f"{name}: a value is required"
        return "", None
    if kind == "number":
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            return raw, f"{name}: enter a valid number"
        return int(number) if number.is_integer() else number, None
    if kind == "date":
        try:
            parsed = raw if isinstance(raw, date) else date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return raw, f"{name}: enter a valid date"
        return parsed.isoformat(), None
    if kind == "dropdown":
        value = str(raw)
        if definition.options and value not in definition.options:
            return value, f"{name}: select one of the configured choices"
        return value, None
    if kind in {"file", "folder"}:
        value = str(raw)
        path = Path(value).expanduser()
        if validate_paths and not path.exists():
            return value, f"{name}: path does not exist"
        if validate_paths and kind == "file" and not path.is_file():
            return value, f"{name}: select a file"
        if validate_paths and kind == "folder" and not path.is_dir():
            return value, f"{name}: select a folder"
        return value, None
    return str(raw), None


def sensitive_variable_names(project: RpaProject) -> set[str]:
    return {
        name for name, raw_definition in (getattr(project, "runtime_inputs", {}) or {}).items()
        for definition in [
            raw_definition if isinstance(raw_definition, RuntimeInputDefinition)
            else RuntimeInputDefinition.from_dict(raw_definition)
        ]
        if definition.sensitive or definition.type.casefold() == "password"
    }


def validate_variable_configuration(project: RpaProject) -> list[str]:
    errors: list[str] = []
    reserved = set(built_in_variables())
    project_variables = getattr(project, "variables", {}) or {}
    runtime_inputs = getattr(project, "runtime_inputs", {}) or {}
    output_variables = getattr(project, "output_variables", []) or []
    seen = set(project_variables)
    for name in project_variables:
        if not VARIABLE_NAME_PATTERN.fullmatch(name):
            errors.append(f"{name or '(blank name)'}: invalid Project Variable name")
        if name in reserved:
            errors.append(f"{name}: built-in variable names cannot be redefined")
    for name, raw_definition in runtime_inputs.items():
        definition = (
            raw_definition if isinstance(raw_definition, RuntimeInputDefinition)
            else RuntimeInputDefinition.from_dict(raw_definition)
        )
        if not VARIABLE_NAME_PATTERN.fullmatch(name):
            errors.append(f"{name or '(blank name)'}: use letters, numbers, and underscores, starting with a letter or underscore")
        if name in reserved:
            errors.append(f"{name}: built-in variable names cannot be redefined")
        if name in seen:
            errors.append(f"{name}: already exists as a Project Variable")
        seen.add(name)
        if definition.type.casefold() not in INPUT_TYPES:
            errors.append(f"{name}: unsupported input type '{definition.type}'")
        if definition.type.casefold() == "dropdown" and not definition.options:
            errors.append(f"{name}: add at least one dropdown choice")
        if definition.default not in (None, ""):
            _value, error = coerce_runtime_input(name, definition, definition.default, validate_paths=False)
            if error:
                errors.append(error)
    for name in output_variables:
        if not VARIABLE_NAME_PATTERN.fullmatch(name):
            errors.append(f"{name or '(blank output name)'}: invalid Output Variable name")
        if name in reserved or name in project_variables or name in runtime_inputs:
            errors.append(f"{name}: Output Variable name conflicts with an existing variable")
    return errors


def mask_sensitive_text(value: Any, secrets: list[Any] | set[Any] | tuple[Any, ...]) -> str:
    text = str(value)
    secret_strings = sorted(
        {str(secret) for secret in secrets if secret is not None and len(str(secret)) >= 1},
        key=len,
        reverse=True,
    )
    for secret in secret_strings:
        text = text.replace(secret, "[REDACTED]")
    return text
