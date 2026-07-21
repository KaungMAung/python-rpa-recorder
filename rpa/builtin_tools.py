from __future__ import annotations

import subprocess
from typing import Any, Callable

from .execution import ExecutionContext
from .models import ActionType
from .tools import FunctionTool, ToolRegistry


WINDOW_ACTIONS = {
    ActionType.SELECT_WINDOW.value, ActionType.WAIT_WINDOW.value,
    ActionType.ACTIVATE_WINDOW.value, ActionType.MAXIMIZE_WINDOW.value,
    ActionType.MINIMIZE_WINDOW.value, ActionType.RESTORE_WINDOW.value,
    ActionType.CLOSE_WINDOW.value, ActionType.CLICK_WINDOW_RELATIVE.value,
    ActionType.MOVE_WINDOW_RELATIVE.value,
}

UTILITY_ACTIONS = {
    ActionType.LAUNCH_APPLICATION.value, ActionType.WAIT_PROCESS.value,
    ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value,
    ActionType.READ_CLIPBOARD.value, ActionType.WRITE_CLIPBOARD.value,
    ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value,
    ActionType.DELETE_PATH.value, ActionType.WAIT_PATH.value,
    ActionType.RUN_POWERSHELL.value, ActionType.RUN_PYTHON_SCRIPT.value,
    ActionType.SHOW_NOTIFICATION.value,
}

VARIABLE_ACTIONS = {
    ActionType.SET_VARIABLE.value, ActionType.GET_VARIABLE.value,
    ActionType.INCREMENT_VARIABLE.value, ActionType.APPEND_VARIABLE.value,
    ActionType.SET_OBJECT_PROPERTY.value, ActionType.DELETE_VARIABLE.value,
}


def create_builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def add(action_type: str, description: str, handler: Callable[[dict[str, Any], ExecutionContext], Any]) -> None:
        registry.register(action_type, FunctionTool(action_type, description, handler))

    add(ActionType.CLICK_IMAGE.value, "Click a matched image", _image_click)
    add(ActionType.DOUBLE_CLICK_IMAGE.value, "Double-click a matched image", _image_click)
    add(ActionType.TYPE_TEXT.value, "Type text", _type_text)
    add(ActionType.PRESS_KEY.value, "Press a keyboard key", _press_key)
    add(ActionType.HOTKEY.value, "Press a keyboard shortcut", _hotkey)
    add(ActionType.SCROLL.value, "Scroll the mouse wheel", _scroll)
    add(ActionType.WAIT.value, "Wait for a duration", _wait)
    add(ActionType.CLICK_COORDINATE.value, "Click screen coordinates", _coordinate_click)
    add(ActionType.MOUSE_MOVE.value, "Move the mouse", _mouse_move)
    add(ActionType.DRAG.value, "Drag the mouse", _drag)
    add(ActionType.OPEN_FILE.value, "Open a file or application", _open_file)
    add(ActionType.RUN_PYTHON.value, "Run Python code", _python)
    add(ActionType.PYTHON_CODE.value, "Run Python code", _python)
    add(ActionType.RUN_SUBFLOW.value, "Run another flow", _subflow)
    for action_type in WINDOW_ACTIONS:
        add(action_type, "Perform a window operation", _window)
    for action_type in UTILITY_ACTIONS:
        add(action_type, "Perform a Windows utility operation", _utility)
    for action_type in VARIABLE_ACTIONS:
        add(action_type, "Read or update a runtime variable", _variable)
    return registry


def _action(context: ExecutionContext):
    if context.current_action is None:
        raise RuntimeError("No current action is available in the execution context")
    return context.current_action


def _gui(context: ExecutionContext):
    return context.helper("get_gui")()


def _image_click(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("click_image")(
        _action(context), inputs,
        bool(context.execution_state.get("allow_coordinate_fallback", True)),
    )


def _type_text(inputs: dict[str, Any], context: ExecutionContext) -> str:
    gui = _gui(context)
    if inputs.get("clear_first"):
        gui.hotkey("ctrl", "a")
        gui.press("backspace")
    value = str(inputs.get("text", ""))
    gui.write(value, interval=float(inputs.get("interval", context.project.settings.typing_interval)))
    context.helper("store_output")(inputs, context.variables, value)
    return value


def _press_key(inputs: dict[str, Any], context: ExecutionContext) -> None:
    _gui(context).press(
        str(inputs.get("key")), presses=int(inputs.get("count", 1)),
        interval=float(inputs.get("interval", 0.0)),
    )


def _hotkey(inputs: dict[str, Any], context: ExecutionContext) -> None:
    _gui(context).hotkey(*[str(key) for key in inputs.get("keys", [])])


def _scroll(inputs: dict[str, Any], context: ExecutionContext) -> None:
    gui = _gui(context)
    if inputs.get("move_to"):
        gui.moveTo(int(inputs.get("x", 0)), int(inputs.get("y", 0)))
    gui.scroll(int(inputs.get("amount", 0)))


def _wait(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("sleep")(float(inputs.get("seconds", _action(context).delay_before)))


def _coordinate_click(inputs: dict[str, Any], context: ExecutionContext) -> None:
    x, y = int(inputs.get("x", 0)), int(inputs.get("y", 0))
    context.helper("sleep")(float(inputs.get("pre_click_pause", context.project.settings.pre_click_pause)))
    _gui(context).click(x, y, button=str(inputs.get("button", "left")))
    context.helper("set_last_click")(context.variables, x, y)


def _mouse_move(inputs: dict[str, Any], context: ExecutionContext) -> None:
    _gui(context).moveTo(
        int(inputs.get("x", 0)), int(inputs.get("y", 0)),
        duration=float(inputs.get("duration", 0.2)),
    )


def _drag(inputs: dict[str, Any], context: ExecutionContext) -> None:
    gui = _gui(context)
    gui.moveTo(
        int(inputs.get("start_x", 0)), int(inputs.get("start_y", 0)),
        duration=float(inputs.get("move_duration", 0.2)),
    )
    end_x, end_y = int(inputs.get("end_x", 0)), int(inputs.get("end_y", 0))
    gui.dragTo(
        end_x, end_y, duration=float(inputs.get("duration", 0.5)),
        button=str(inputs.get("button", "left")),
    )
    context.helper("set_last_click")(context.variables, end_x, end_y)


def _window(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("window_action")(_action(context), inputs, context.variables)


def _open_file(inputs: dict[str, Any], context: ExecutionContext) -> str:
    path = str(inputs.get("path", ""))
    subprocess.Popen([path], shell=True)
    context.helper("sleep")(float(inputs.get("wait_after", 1.0)))
    context.helper("store_output")(inputs, context.variables, path)
    return path


def _python(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("python")(_action(context), inputs, context.variables, context.current_step)


def _variable(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("variable_action")(_action(context).action, inputs, context.variables)


def _subflow(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("subflow")(_action(context), inputs, context.variables, context.current_step)


def _utility(inputs: dict[str, Any], context: ExecutionContext) -> None:
    context.helper("native_utility")(_action(context), inputs, context.variables)

