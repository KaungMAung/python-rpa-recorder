from __future__ import annotations

import subprocess
import threading
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from .image_matcher import wait_for_image
from .models import ActionType, RpaAction, RpaProject
from .utils import MissingPlaceholderError, foreground_elevation_mismatch, resolve_placeholders_strict

pyautogui = None


class StopReplay(Exception):
    pass


class ReplayActionError(RuntimeError):
    def __init__(self, index: int, action: RpaAction, cause: Exception) -> None:
        self.index = index
        self.action = action
        self.cause = cause
        super().__init__(f"Step {index + 1} {action.summary()}: {cause}")


class ReplayRunner:
    def __init__(
        self,
        project: RpaProject,
        project_dir: Path,
        log: Callable[[str], None],
        excluded_regions: list[tuple[int, int, int, int]] | None = None,
    ) -> None:
        self.project = project
        self.project_dir = Path(project_dir)
        self.log = log
        self._stop_event = threading.Event()
        self.runtime_variables: dict[str, Any] = dict(project.variables)
        self.excluded_regions = list(excluded_regions or [])

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def sleep_checked(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_requested():
                raise StopReplay()
            time.sleep(min(0.05, deadline - time.monotonic()))

    def run(
        self,
        action_callback: Callable[[int, str], None] | None = None,
        start_index: int = 0,
        end_index: int | None = None,
        include_start_delay: bool = True,
        respect_enabled: bool = True,
    ) -> None:
        gui = get_pyautogui()
        gui.FAILSAFE = self.project.settings.pyautogui_failsafe
        total = len(self.project.actions)
        start_index = max(0, min(start_index, total))
        end_index = total - 1 if end_index is None else max(-1, min(end_index, total - 1))
        self.log("replay started")
        if include_start_delay:
            self.sleep_checked(self.project.settings.start_delay)
        mismatch = foreground_elevation_mismatch()
        if mismatch:
            raise PermissionError(mismatch[1])
        for index in range(start_index, end_index + 1):
            action = self.project.actions[index]
            if self.stop_requested():
                raise StopReplay()
            if respect_enabled and not action.enabled:
                if action_callback:
                    action_callback(index, "skipped")
                continue
            if action_callback:
                action_callback(index, "running")
            self.log(f"action started: {index + 1} {action.action}")
            # Click Image steps rely on continuous polling with their own search
            # timeout (see _click_image/wait_for_image) instead of a fixed
            # pre-wait, so they click as soon as the target appears rather than
            # always waiting out the recorded delay.
            if action.action not in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
                self.sleep_checked(action.delay_before)
            try:
                self.run_action(action, self.runtime_variables, index + 1)
            except MissingPlaceholderError as exc:
                if action_callback:
                    action_callback(index, "failed")
                error = ValueError(f"missing variable '{exc.variable}'")
                raise ReplayActionError(index, action, error) from exc
            except StopReplay:
                raise
            except Exception as exc:
                if action_callback:
                    action_callback(index, "failed")
                friendly = self._friendly_runtime_error(exc)
                raise ReplayActionError(index, action, friendly) from exc
            if action_callback:
                action_callback(index, "completed")
            self.log(f"action completed: {index + 1} {action.action}")
        self.log("replay completed")

    def run_action(self, action: RpaAction, variables: dict[str, Any] | None = None, step_number: int = 1) -> None:
        variables = self.runtime_variables if variables is None else variables
        data = resolve_placeholders_strict(action.data, variables)
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            self._click_image(action, data)
        elif action.action == ActionType.TYPE_TEXT.value:
            gui = get_pyautogui()
            if data.get("clear_first"):
                gui.hotkey("ctrl", "a")
                gui.press("backspace")
            gui.write(str(data.get("text", "")), interval=float(data.get("interval", self.project.settings.typing_interval)))
        elif action.action == ActionType.PRESS_KEY.value:
            gui = get_pyautogui()
            gui.press(str(data.get("key")), presses=int(data.get("count", 1)), interval=float(data.get("interval", 0.0)))
        elif action.action == ActionType.HOTKEY.value:
            gui = get_pyautogui()
            gui.hotkey(*[str(key) for key in data.get("keys", [])])
        elif action.action == ActionType.SCROLL.value:
            gui = get_pyautogui()
            if data.get("move_to"):
                gui.moveTo(int(data.get("x", 0)), int(data.get("y", 0)))
            gui.scroll(int(data.get("amount", 0)))
        elif action.action == ActionType.WAIT.value:
            self.sleep_checked(float(data.get("seconds", action.delay_before)))
        elif action.action == ActionType.CLICK_COORDINATE.value:
            gui = get_pyautogui()
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            gui.click(int(data.get("x", 0)), int(data.get("y", 0)), button=str(data.get("button", "left")))
        elif action.action == ActionType.OPEN_FILE.value:
            subprocess.Popen([str(data.get("path", ""))], shell=True)
            self.sleep_checked(float(data.get("wait_after", 1.0)))
        elif action.action in (ActionType.RUN_PYTHON.value, ActionType.PYTHON_CODE.value):
            self.run_python_code(action, data, variables, step_number)
        else:
            raise ValueError(f"Unsupported action: {action.action}")

    def run_python_code(self, action: RpaAction, data: dict[str, Any], variables: dict[str, Any], step_number: int) -> str:
        if self.stop_requested():
            raise StopReplay()
        code = str(data.get("code", ""))
        if not code.strip():
            raise ValueError(f"Step {step_number} {action.friendly_name()}: code is required")
        output = StringIO()
        env = {
            "variables": variables,
            "project_dir": self.project_dir,
            "action": action,
            "current_action": action,
            "logger": self.log,
            "check_stop": self._check_stop_for_code,
        }
        try:
            with redirect_stdout(output):
                exec(code, env)
        except Exception as exc:
            if data.get("continue_on_error", False):
                self.log(f"Step {step_number} {action.friendly_name()} exception ignored: {exc}")
            else:
                raise RuntimeError(f"Step {step_number} {action.friendly_name()}: {exc}") from exc
        if self.stop_requested():
            raise StopReplay()
        text = output.getvalue().strip()
        if text:
            self.log(text)
        return output.getvalue()

    def _check_stop_for_code(self) -> bool:
        if self.stop_requested():
            raise StopReplay()
        return False

    def _friendly_runtime_error(self, exc: Exception) -> Exception:
        if exc.__class__.__name__ == "FailSafeException":
            return RuntimeError(
                "PyAutoGUI safety stop was triggered because the mouse reached a screen corner. "
                "Move the pointer away from the corners and run again, or disable failsafe in Settings."
            )
        return exc

    def _click_image(self, action: RpaAction, data: dict) -> None:
        image_path = self.project_dir / str(data.get("image", ""))
        required_confidence = float(data.get("confidence", self.project.settings.default_confidence))
        match = wait_for_image(
            image_path,
            required_confidence,
            float(data.get("timeout", self.project.settings.default_timeout)),
            self.stop_requested,
            excluded_regions=self.excluded_regions,
        )
        if self.stop_requested():
            raise StopReplay()
        if match.found:
            self.log(
                f"image match: confidence={match.confidence:.3f} (required {required_confidence:.3f}), "
                f"location=({match.x}, {match.y}), search time={match.duration:.2f}s"
            )
            x = match.x + int(data.get("click_offset_x", match.width / 2))
            y = match.y + int(data.get("click_offset_y", match.height / 2))
            clicks = 2 if action.action == ActionType.DOUBLE_CLICK_IMAGE.value else 1
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            get_pyautogui().click(x, y, clicks=clicks, button=str(data.get("button", "left")))
            return
        self.log(
            f"image match: no match found, best confidence={getattr(match, 'confidence', 0.0):.3f} "
            f"(required {required_confidence:.3f}), search time={getattr(match, 'duration', 0.0):.2f}s"
        )
        if data.get("use_coordinate_fallback", True):
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            get_pyautogui().click(int(data.get("fallback_x", 0)), int(data.get("fallback_y", 0)), button=str(data.get("button", "left")))
            return
        raise FileNotFoundError(f"Image not found: {image_path}")


def get_pyautogui():
    global pyautogui
    if pyautogui is None:
        import pyautogui as module
        pyautogui = module
    return pyautogui
