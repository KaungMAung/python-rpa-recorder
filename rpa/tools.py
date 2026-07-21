from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .execution import ExecutionContext


@dataclass
class ToolResult:
    success: bool = True
    value: Any = None
    details: dict[str, Any] = field(default_factory=dict)


class RpaTool(ABC):
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}

    def validate(self, inputs: dict[str, Any], context: ExecutionContext) -> list[str]:
        return []

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> ToolResult:
        raise NotImplementedError

    def verify(self, result: ToolResult, context: ExecutionContext) -> bool:
        return result.success

    def recover(self, error: Exception, context: ExecutionContext) -> ToolResult | None:
        return None


class FunctionTool(RpaTool):
    def __init__(
        self,
        name: str,
        description: str,
        executor: Callable[[dict[str, Any], ExecutionContext], Any],
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = dict(input_schema or {})
        self._executor = executor

    def execute(self, inputs: dict[str, Any], context: ExecutionContext) -> ToolResult:
        value = self._executor(inputs, context)
        return value if isinstance(value, ToolResult) else ToolResult(value=value)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RpaTool] = {}

    def register(self, action_type: str, tool: RpaTool, *, replace: bool = False) -> None:
        key = str(action_type).strip()
        if not key:
            raise ValueError("action type is required")
        if key in self._tools and not replace:
            raise ValueError(f"tool already registered for action type: {key}")
        self._tools[key] = tool

    def register_many(self, action_types: Iterable[str], tool: RpaTool) -> None:
        for action_type in action_types:
            self.register(action_type, tool)

    def get(self, action_type: str) -> RpaTool:
        try:
            return self._tools[action_type]
        except KeyError as exc:
            raise KeyError(f"No RPA tool is registered for action type: {action_type}") from exc

    def contains(self, action_type: str) -> bool:
        return action_type in self._tools

    def action_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def execute(
        self, action_type: str, inputs: dict[str, Any], context: ExecutionContext,
    ) -> ToolResult:
        tool = self.get(action_type)
        errors = tool.validate(inputs, context)
        if errors:
            raise ValueError("; ".join(errors))
        result = tool.execute(inputs, context)
        if not tool.verify(result, context):
            raise RuntimeError(f"Tool verification failed: {tool.name or action_type}")
        return result

