from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

FORMAT_NAME = "python-rpa-recorder"
FORMAT_VERSION = 1


class RecorderState(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"


class TimingMode(str, Enum):
    OPTIMIZED = "optimized"
    RECORDED = "recorded"
    NONE = "none"


class ActionType(str, Enum):
    CLICK_IMAGE = "click_image"
    DOUBLE_CLICK_IMAGE = "double_click_image"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    WAIT = "wait"
    OPEN_FILE = "open_file"
    RUN_PYTHON = "run_python"
    PYTHON_CODE = "python_code"
    CLICK_COORDINATE = "click_coordinate"
    MOUSE_MOVE = "mouse_move"
    DRAG = "drag"
    IF_IMAGE_EXISTS = "if_image_exists"
    IF_IMAGE_NOT_EXISTS = "if_image_not_exists"
    IF_WINDOW_EXISTS = "if_window_exists"
    IF_PATH_EXISTS = "if_path_exists"
    IF_VARIABLE = "if_variable"
    ELSE = "else"
    END_IF = "end_if"
    REPEAT_COUNT = "repeat_count"
    REPEAT_UNTIL = "repeat_until"
    END_LOOP = "end_loop"
    BREAK_LOOP = "break_loop"


class ActionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ProjectSettings:
    timing_mode: str = TimingMode.RECORDED.value
    crop_width: int = 180
    crop_height: int = 120
    default_confidence: float = 0.86
    default_timeout: float = 10.0
    text_flush_timeout: float = 0.7
    double_click_interval: float = 0.35
    coordinate_fallback: bool = True
    typing_interval: float = 0.02
    start_delay: float = 3.0
    pre_click_pause: float = 0.10
    ignore_application_window: bool = True
    pyautogui_failsafe: bool = True
    show_desktop_before_recording: bool = True
    hide_window_during_replay: bool = True
    evidence_retention_runs: int = 100

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProjectSettings":
        if not data:
            return cls()
        fields = cls().__dict__.keys()
        return cls(**{key: data[key] for key in fields if key in data})


@dataclass
class RpaAction:
    action: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    enabled: bool = True
    delay_before: float = 0.0
    recorded_delay: float = 0.0
    status: str = ActionStatus.PENDING.value

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RpaAction":
        return cls(
            id=data.get("id") or str(uuid4()),
            name=data.get("name", ""),
            action=data["action"],
            enabled=bool(data.get("enabled", True)),
            delay_before=float(data.get("delay_before", 0.0) or 0.0),
            recorded_delay=float(data.get("recorded_delay", 0.0) or 0.0),
            status=data.get("status", ActionStatus.PENDING.value),
            data=dict(data.get("data") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self, mask_secrets: bool = True) -> str:
        data = self.data
        if self.name.strip():
            return self.name.strip()
        if self.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            target = data.get("target_name") or "screen target"
            verb = "Double-click" if self.action == ActionType.DOUBLE_CLICK_IMAGE.value else "Click"
            return f"{verb} {target}"
        if self.action == ActionType.TYPE_TEXT.value:
            if data.get("masked") and mask_secrets:
                return "Type protected text"
            text = str(data.get("text", ""))
            text = text.replace("\r", " ").replace("\n", " ").strip()
            if not text:
                return "Type text"
            shortened = text if len(text) <= 48 else text[:45] + "..."
            return f'Type "{shortened}"'
        if self.action == ActionType.PRESS_KEY.value:
            key = str(data.get("key", "")).replace("_", " ").title()
            count = int(data.get("count", 1) or 1)
            return f"Press {key}" if count == 1 else f"Press {key} {count} times"
        if self.action == ActionType.HOTKEY.value:
            keys = "+".join(str(key).title() for key in data.get("keys", []))
            return f"Press {keys}"
        if self.action == ActionType.SCROLL.value:
            amount = int(data.get("amount", 0) or 0)
            return f"Scroll {'up' if amount > 0 else 'down'} {abs(amount)}"
        if self.action == ActionType.WAIT.value:
            return f"Wait {float(data.get('seconds', self.delay_before) or 0):.2f} seconds"
        if self.action == ActionType.OPEN_FILE.value:
            path = str(data.get("path", ""))
            return f"Open {path}" if path else "Open a file"
        if self.action == ActionType.RUN_PYTHON.value:
            return "Run Python code"
        if self.action == ActionType.PYTHON_CODE.value:
            name = data.get("name") or self.name or "Python Code"
            return str(name)
        if self.action == ActionType.CLICK_COORDINATE.value:
            button = str(data.get("button", "left"))
            label = "Right-click" if button == "right" else "Click"
            return f"{label} position ({data.get('x')}, {data.get('y')})"
        if self.action == ActionType.MOUSE_MOVE.value:
            return f"Move mouse to ({data.get('x')}, {data.get('y')})"
        if self.action == ActionType.DRAG.value:
            return f"Drag from ({data.get('start_x')}, {data.get('start_y')}) to ({data.get('end_x')}, {data.get('end_y')})"
        if self.action == ActionType.IF_IMAGE_EXISTS.value:
            return f"If image exists: {data.get('image') or 'choose an image'}"
        if self.action == ActionType.IF_IMAGE_NOT_EXISTS.value:
            return f"If image does not exist: {data.get('image') or 'choose an image'}"
        if self.action == ActionType.IF_WINDOW_EXISTS.value:
            return f"If window exists: {data.get('window_title') or 'enter a title'}"
        if self.action == ActionType.IF_PATH_EXISTS.value:
            return f"If {data.get('path_type', 'file or folder')} exists: {data.get('path') or 'choose a path'}"
        if self.action == ActionType.IF_VARIABLE.value:
            operator = str(data.get("operator", "equals")).replace("_", " ")
            value = "" if operator == "is empty" else f" {data.get('value', '')}"
            return f"If {data.get('variable') or 'variable'} {operator}{value}"
        if self.action == ActionType.ELSE.value:
            return "Else"
        if self.action == ActionType.END_IF.value:
            return "End If"
        if self.action == ActionType.REPEAT_COUNT.value:
            return f"Repeat {data.get('count', 1)} times"
        if self.action == ActionType.REPEAT_UNTIL.value:
            return f"Repeat until {condition_summary(data)}"
        if self.action == ActionType.END_LOOP.value:
            return "End Loop"
        if self.action == ActionType.BREAK_LOOP.value:
            return "Break Loop"
        return self.action

    def friendly_name(self) -> str:
        return FRIENDLY_ACTION_NAMES.get(self.action, self.action.replace("_", " ").title())


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class ProjectMeta:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Untitled Recording"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass
class RuntimeInputDefinition:
    """Saved description of one value requested (or supplied by a schedule) at run time."""

    type: str = "text"
    default: Any = ""
    required: bool = True
    sensitive: bool = False
    options: list[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> "RuntimeInputDefinition":
        if not isinstance(data, dict):
            return cls(default=data if data is not None else "")
        return cls(
            type=str(data.get("type") or "text"),
            default=data.get("default", ""),
            required=bool(data.get("required", True)),
            sensitive=bool(data.get("sensitive", False)),
            options=[str(item) for item in data.get("options", []) if str(item)],
            description=str(data.get("description") or ""),
        )


@dataclass
class RpaProject:
    project: ProjectMeta = field(default_factory=ProjectMeta)
    settings: ProjectSettings = field(default_factory=ProjectSettings)
    variables: dict[str, str] = field(default_factory=dict)
    runtime_inputs: dict[str, RuntimeInputDefinition] = field(default_factory=dict)
    output_variables: list[str] = field(default_factory=list)
    actions: list[RpaAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RpaProject":
        if data.get("format") != FORMAT_NAME:
            raise ValueError("Invalid project format")
        if int(data.get("format_version", 0)) != FORMAT_VERSION:
            raise ValueError("Unsupported project format version")
        raw_runtime_inputs = data.get("runtime_inputs") or {}
        if not isinstance(raw_runtime_inputs, dict):
            raw_runtime_inputs = {}
        raw_output_variables = data.get("output_variables") or []
        if not isinstance(raw_output_variables, list):
            raw_output_variables = []
        return cls(
            project=ProjectMeta(**data.get("project", {})),
            settings=ProjectSettings.from_dict(data.get("settings")),
            variables=dict(data.get("variables") or {}),
            runtime_inputs={
                str(name): RuntimeInputDefinition.from_dict(definition)
                for name, definition in raw_runtime_inputs.items()
            },
            output_variables=[str(name) for name in raw_output_variables],
            actions=[RpaAction.from_dict(item) for item in data.get("actions", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "project": asdict(self.project),
            "settings": asdict(self.settings),
            "variables": self.variables,
            "runtime_inputs": {
                name: asdict(
                    definition if isinstance(definition, RuntimeInputDefinition)
                    else RuntimeInputDefinition.from_dict(definition)
                )
                for name, definition in self.runtime_inputs.items()
            },
            "output_variables": self.output_variables,
            "actions": [action.to_dict() for action in self.actions],
        }


FRIENDLY_ACTION_NAMES = {
    ActionType.CLICK_IMAGE.value: "Click",
    ActionType.DOUBLE_CLICK_IMAGE.value: "Double Click",
    ActionType.TYPE_TEXT.value: "Type Text",
    ActionType.PRESS_KEY.value: "Press Key",
    ActionType.HOTKEY.value: "Keyboard Shortcut",
    ActionType.SCROLL.value: "Scroll",
    ActionType.WAIT.value: "Wait",
    ActionType.OPEN_FILE.value: "Open File",
    ActionType.RUN_PYTHON.value: "Run Python",
    ActionType.PYTHON_CODE.value: "Python Code",
    ActionType.CLICK_COORDINATE.value: "Click Position",
    ActionType.MOUSE_MOVE.value: "Mouse Move",
    ActionType.DRAG.value: "Drag",
    ActionType.IF_IMAGE_EXISTS.value: "If Image Exists",
    ActionType.IF_IMAGE_NOT_EXISTS.value: "If Image Does Not Exist",
    ActionType.IF_WINDOW_EXISTS.value: "If Window Exists",
    ActionType.IF_PATH_EXISTS.value: "If File or Folder Exists",
    ActionType.IF_VARIABLE.value: "If Variable",
    ActionType.ELSE.value: "Else",
    ActionType.END_IF.value: "End If",
    ActionType.REPEAT_COUNT.value: "Repeat N Times",
    ActionType.REPEAT_UNTIL.value: "Repeat Until",
    ActionType.END_LOOP.value: "End Loop",
    ActionType.BREAK_LOOP.value: "Break Loop",
}


def condition_summary(data: dict[str, Any]) -> str:
    kind = str(data.get("condition_type", "variable"))
    if kind in {"image_exists", "image_not_exists"}:
        verb = "does not exist" if kind == "image_not_exists" else "exists"
        return f"image {data.get('image') or '?'} {verb}"
    if kind == "window_exists":
        return f"window '{data.get('window_title') or '?'}' exists"
    if kind == "path_exists":
        return f"{data.get('path') or 'path'} exists"
    operator = str(data.get("operator", "equals")).replace("_", " ")
    suffix = "" if operator == "is empty" else f" {data.get('value', '')}"
    return f"{data.get('variable') or 'variable'} {operator}{suffix}"
