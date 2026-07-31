from __future__ import annotations

import json
import math
from typing import Any

import requests

from .utils import resolve_placeholders_strict


SUPPORTED_FIELD_TYPES = {"text", "number", "boolean", "null"}


def builder_rows_to_object(rows: Any, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise ValueError("Webhook payload fields must be a list.")
    should_resolve = variables is not None
    variables = variables or {}
    result: dict[str, Any] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Payload field {index} is invalid.")
        name = str(row.get("name", "")).strip()
        if not name:
            raise ValueError(f"Payload field {index} needs a name.")
        if name in result:
            raise ValueError(f"Payload field name '{name}' is duplicated.")
        field_type = str(row.get("type", "text")).strip().lower()
        if field_type not in SUPPORTED_FIELD_TYPES:
            raise ValueError(f"Payload field '{name}' has an unsupported type.")
        raw = row.get("value", "")
        resolved = resolve_placeholders_strict(raw, variables) if should_resolve else raw
        if field_type == "text":
            value = str(resolved)
        elif field_type == "number":
            value = _number_value(resolved, name)
        elif field_type == "boolean":
            value = _boolean_value(resolved, name)
        else:
            value = None
        result[name] = value
    return result


def plain_json_to_object(text: Any, variables: dict[str, Any] | None = None) -> Any:
    try:
        parsed = json.loads(str(text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    resolved = resolve_placeholders_strict(parsed, variables or {}) if variables is not None else parsed
    try:
        json.dumps(resolved, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Resolved JSON is invalid: {exc}.") from exc
    return resolved


def builder_rows_to_json(rows: Any) -> str:
    if not isinstance(rows, list):
        raise ValueError("Webhook payload fields must be a list.")
    payload: dict[str, Any] = {}
    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "")).strip() if isinstance(row, dict) else ""
        if not name:
            raise ValueError(f"Payload field {index} needs a name.")
        if name in payload:
            raise ValueError(f"Payload field name '{name}' is duplicated.")
        field_type = str(row.get("type", "text")).strip().lower()
        raw = row.get("value", "")
        if field_type == "null":
            value = None
        elif field_type == "number" and "{{" not in str(raw):
            value = _number_value(raw, name)
        elif field_type == "boolean" and "{{" not in str(raw):
            value = _boolean_value(raw, name)
        elif field_type in SUPPORTED_FIELD_TYPES:
            value = str(raw)
        else:
            raise ValueError(f"Payload field '{name}' has an unsupported type.")
        payload[name] = value
    return json.dumps(payload, indent=2, ensure_ascii=False)


def plain_json_to_builder_rows(text: Any) -> list[dict[str, str]]:
    payload = plain_json_to_object(text)
    if not isinstance(payload, dict):
        raise ValueError("Builder mode requires a JSON object, not an array or single value.")
    rows: list[dict[str, str]] = []
    for name, value in payload.items():
        if isinstance(value, (dict, list)):
            raise ValueError("Builder mode supports only a flat object with Text, Number, Boolean, or Null values.")
        if value is None:
            field_type, text_value = "null", ""
        elif isinstance(value, bool):
            field_type, text_value = "boolean", "true" if value else "false"
        elif isinstance(value, (int, float)):
            field_type, text_value = "number", str(value)
        elif isinstance(value, str):
            field_type, text_value = "text", value
        else:
            raise ValueError("Builder mode supports only Text, Number, Boolean, or Null values.")
        rows.append({"name": str(name), "value": text_value, "type": field_type})
    return rows


def build_webhook_request(data: dict[str, Any], variables: dict[str, Any]) -> tuple[str, Any, float]:
    url = str(resolve_placeholders_strict(data.get("url", ""), variables)).strip()
    if not url:
        raise ValueError("Webhook URL is required.")
    timeout = float(data.get("timeout", 60.0))
    if not 10 <= timeout <= 120:
        raise ValueError("Webhook timeout must be between 10 and 120 seconds.")
    mode = str(data.get("payload_mode", "builder")).strip().lower()
    if mode == "builder":
        payload = builder_rows_to_object(data.get("payload_fields", []), variables)
    elif mode == "json":
        payload = plain_json_to_object(data.get("json_payload", "{}"), variables)
    else:
        raise ValueError("Webhook payload mode must be Key/Value Builder or Plain JSON.")
    return url, payload, timeout


def execute_webhook(data: dict[str, Any], variables: dict[str, Any]) -> Any:
    url, payload, timeout = build_webhook_request(data, variables)
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise TimeoutError(f"Power Automate webhook timed out after {timeout:g} seconds.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Power Automate webhook request failed: {exc}") from exc
    if not 200 <= int(response.status_code) < 300:
        raise RuntimeError(f"Power Automate webhook returned HTTP {response.status_code}.")
    try:
        result = response.json()
    except ValueError:
        result = response.text
    output = str(data.get("output_variable", "")).strip()
    if output:
        variables[output] = result
    return result


def _number_value(value: Any, name: str) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"Payload field '{name}' must resolve to a valid number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Payload field '{name}' must resolve to a valid number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"Payload field '{name}' must resolve to a finite number.")
    return int(number) if number.is_integer() else number


def _boolean_value(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"Payload field '{name}' must resolve to true or false.")
