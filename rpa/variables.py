"""Variable categories, built-ins, runtime input coercion, and secret masking."""
from __future__ import annotations

from datetime import date, datetime
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from .models import RpaProject, RuntimeInputDefinition, VariableDefinition

VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INPUT_TYPES = ("text", "number", "date", "dropdown", "password", "file", "folder")
VARIABLE_TYPES = ("text", "integer", "decimal", "boolean", "list", "object", "null", "secret_text")


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
    definitions = getattr(project, "variable_definitions", {}) or {}
    values: dict[str, Any] = {
        name: deepcopy(
            definition.default if isinstance(definition, VariableDefinition)
            else VariableDefinition.from_dict(definition).default
        )
        for name, definition in definitions.items()
    }
    values.update(deepcopy(dict(getattr(project, "variables", {}) or {})))
    if getattr(project.settings, "persist_variable_values", False):
        known = set(values)
        values.update({
            name: deepcopy(value)
            for name, value in (getattr(project, "persisted_variable_values", {}) or {}).items()
            if name in known
        })
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
    sensitive = {
        name for name, raw_definition in (getattr(project, "runtime_inputs", {}) or {}).items()
        for definition in [
            raw_definition if isinstance(raw_definition, RuntimeInputDefinition)
            else RuntimeInputDefinition.from_dict(raw_definition)
        ]
        if definition.sensitive or definition.type.casefold() == "password"
    }
    sensitive.update(
        name for name, raw_definition in (getattr(project, "variable_definitions", {}) or {}).items()
        for definition in [
            raw_definition if isinstance(raw_definition, VariableDefinition)
            else VariableDefinition.from_dict(raw_definition)
        ]
        if definition.secret or definition.type == "secret_text"
    )
    return sensitive


def coerce_variable_value(name: str, kind: str, raw: Any) -> tuple[Any, str | None]:
    """Convert a dialog/import value to its declared JSON-friendly type."""
    kind = str(kind).casefold()
    try:
        if kind in {"text", "secret_text"}:
            return str(raw if raw is not None else ""), None
        if kind == "integer":
            if isinstance(raw, bool):
                raise ValueError
            return int(str(raw).strip()), None
        if kind == "decimal":
            if isinstance(raw, bool):
                raise ValueError
            return float(str(raw).strip()), None
        if kind == "boolean":
            if isinstance(raw, bool):
                return raw, None
            normalized = str(raw).strip().casefold()
            if normalized in {"true", "1", "yes", "on"}:
                return True, None
            if normalized in {"false", "0", "no", "off"}:
                return False, None
            raise ValueError
        if kind in {"list", "object"}:
            value = raw if isinstance(raw, (list, dict)) else json.loads(str(raw))
            expected = list if kind == "list" else dict
            if not isinstance(value, expected):
                return raw, f"{name}: enter a JSON {kind}"
            return value, None
        if kind == "null":
            return None, None
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw, f"{name}: invalid {kind.replace('_', ' ')} value"
    return raw, f"{name}: unsupported variable type '{kind}'"


def json_compatible_runtime_values(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    compatible: dict[str, Any] = {}
    warnings: list[str] = []
    for name, value in values.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError, OverflowError):
            warnings.append(f"{name}: runtime value of type {type(value).__name__} cannot be persisted")
        else:
            compatible[name] = deepcopy(value)
    return compatible, warnings


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
        raw_definition = (getattr(project, "variable_definitions", {}) or {}).get(name)
        definition = (
            raw_definition if isinstance(raw_definition, VariableDefinition)
            else VariableDefinition.from_dict(raw_definition if raw_definition is not None else project_variables[name])
        )
        if definition.type not in VARIABLE_TYPES:
            errors.append(f"{name}: unsupported variable type '{definition.type}'")
        _value, error = coerce_variable_value(name, definition.type, definition.default)
        if error:
            errors.append(error)
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
