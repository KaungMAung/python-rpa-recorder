from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .models import RpaAction, RpaProject


COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
FAILED = "FAILED"
STOPPED_BY_USER = "STOPPED_BY_USER"
RECOVERED = "RECOVERED"
REQUIRES_ATTENTION = "REQUIRES_ATTENTION"


@dataclass
class ExecutionContext:
    """Mutable services and state shared by every tool in one flow run."""

    project: RpaProject
    project_dir: Path
    variables: dict[str, Any]
    log: Callable[[str], None]
    current_step: int = 0
    current_action: RpaAction | None = None
    flow_metadata: dict[str, Any] = field(default_factory=dict)
    execution_state: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    helpers: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def helper(self, name: str) -> Callable[..., Any]:
        try:
            return self.helpers[name]
        except KeyError as exc:
            raise RuntimeError(f"Execution helper is unavailable: {name}") from exc

    def log_event(self, event: str, **fields: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in fields.items())
        self.log(f"event={event}" + (f" {details}" if details else ""))

