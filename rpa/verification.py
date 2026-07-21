from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import time
from typing import Any, Callable

from .execution import ExecutionContext
from .image_matcher import find_image
from .native_utilities import find_process


SUPPORTED_VERIFICATIONS = {
    "image_visible", "image_not_visible", "file_exists", "file_not_exists",
    "variable_equals", "variable_not_empty", "window_title_contains", "process_running",
}

_DOLLAR_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\}")


@dataclass
class VerificationResult:
    passed: bool
    condition_type: str
    message: str
    observed: Any = None
    attempts: int = 1
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerificationEngine:
    def verify(self, condition: dict[str, Any], context: ExecutionContext) -> VerificationResult:
        if not isinstance(condition, dict):
            raise ValueError("verification condition must be an object")
        kind = str(condition.get("type") or "").strip().casefold()
        if kind not in SUPPORTED_VERIFICATIONS:
            raise ValueError(f"unsupported verification type: {kind or '(blank)'}")
        timeout = max(0.0, float(condition.get("timeout_seconds", 0.0) or 0.0))
        interval = max(0.05, float(condition.get("poll_interval_seconds", 0.5) or 0.5))
        started = time.monotonic()
        attempts = 0
        observed: Any = None
        deadline = started + timeout
        while True:
            attempts += 1
            check_stop = context.helpers.get("check_stop")
            if check_stop:
                check_stop()
            passed, observed = self._probe(kind, condition, context)
            if passed:
                duration = time.monotonic() - started
                return VerificationResult(
                    True, kind, f"{kind} verification passed", observed, attempts, duration,
                )
            if time.monotonic() >= deadline:
                duration = time.monotonic() - started
                return VerificationResult(
                    False, kind, f"{kind} verification failed", observed, attempts, duration,
                )
            sleep = context.helpers.get("sleep")
            if sleep:
                sleep(min(interval, max(0.0, deadline - time.monotonic())))
            else:
                time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def verify_completion(
        self, criteria: dict[str, Any], context: ExecutionContext,
    ) -> tuple[bool, list[VerificationResult]]:
        mode = str(criteria.get("mode") or "all").casefold()
        if mode not in {"all", "any"}:
            raise ValueError("completion criteria mode must be 'all' or 'any'")
        conditions = criteria.get("conditions") or []
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("completion criteria must contain at least one condition")
        results: list[VerificationResult] = []
        for condition in conditions:
            result = self.verify(condition, context)
            results.append(result)
            if mode == "any" and result.passed:
                return True, results
            if mode == "all" and not result.passed:
                return False, results
        return (all(item.passed for item in results) if mode == "all" else any(item.passed for item in results)), results

    def _probe(
        self, kind: str, condition: dict[str, Any], context: ExecutionContext,
    ) -> tuple[bool, Any]:
        custom = context.execution_state.get("verification_probes", {}).get(kind)
        if custom:
            value = custom(condition, context)
            return value if isinstance(value, tuple) else (bool(value), value)
        if kind in {"image_visible", "image_not_visible"}:
            value = str(self._resolve(condition.get("value", ""), context.variables))
            path = Path(value).expanduser()
            path = path if path.is_absolute() else context.project_dir / path
            confidence = float(condition.get("confidence", context.project.settings.default_confidence))
            match = find_image(path, confidence)
            visible = bool(match.found)
            return (visible if kind == "image_visible" else not visible), {
                "image": value, "visible": visible, "confidence": float(match.confidence),
            }
        if kind in {"file_exists", "file_not_exists"}:
            value = str(self._resolve(condition.get("value", ""), context.variables))
            path = Path(value).expanduser()
            path = path if path.is_absolute() else context.project_dir / path
            exists = path.exists()
            return (exists if kind == "file_exists" else not exists), str(path)
        if kind == "variable_equals":
            name = str(condition.get("variable") or condition.get("name") or "")
            actual = self._lookup(context.variables, name)
            expected = self._resolve(condition.get("value"), context.variables)
            return actual == expected, {"variable": name, "actual": actual, "expected": expected}
        if kind == "variable_not_empty":
            name = str(condition.get("variable") or condition.get("value") or "")
            actual = self._lookup(context.variables, name)
            return actual is not None and (not isinstance(actual, str) or bool(actual.strip())), {
                "variable": name, "actual": actual,
            }
        if kind == "window_title_contains":
            wanted = str(self._resolve(condition.get("value", ""), context.variables)).casefold()
            titles = context.helper("window_titles")()
            matched = next((title for title in titles if wanted in str(title).casefold()), None)
            return matched is not None, matched
        if kind == "process_running":
            process = str(self._resolve(condition.get("value", ""), context.variables))
            matches = find_process(process)
            return bool(matches), matches[0] if matches else None
        raise ValueError(f"unsupported verification type: {kind}")

    def _resolve(self, value: Any, variables: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value
        exact = _DOLLAR_VARIABLE.fullmatch(value)
        if exact:
            return self._lookup(variables, exact.group(1))
        return _DOLLAR_VARIABLE.sub(lambda match: str(self._lookup(variables, match.group(1))), value)

    @staticmethod
    def _lookup(variables: dict[str, Any], path: str) -> Any:
        parts = path.split(".") if path else []
        if not parts or parts[0] not in variables:
            raise KeyError(f"undefined variable: {path or '(blank)'}")
        value: Any = variables[parts[0]]
        for part in parts[1:]:
            if not isinstance(value, dict) or part not in value:
                raise KeyError(f"undefined variable: {path}")
            value = value[part]
        return value

