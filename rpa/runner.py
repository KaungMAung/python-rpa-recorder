from __future__ import annotations

import subprocess
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from .image_matcher import screenshot_image, wait_for_image
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
        evidence_dir: Path | None = None,
    ) -> None:
        self.project = project
        self.project_dir = Path(project_dir)
        self.log = log
        self._stop_event = threading.Event()
        self.runtime_variables: dict[str, Any] = dict(project.variables)
        self.excluded_regions = list(excluded_regions or [])
        self.total_attempts = 0
        self.current_index: int | None = None
        self.had_continued_failures = False
        self.first_failed_index: int | None = None
        self.first_failure_error: str | None = None
        self._step_deadline: float | None = None
        self._best_image_confidence = 0.0
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.step_results: list[dict[str, Any]] = []

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def sleep_checked(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_requested():
                raise StopReplay()
            if self._step_timed_out():
                raise TimeoutError("step timed out")
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _step_timed_out(self) -> bool:
        return self._step_deadline is not None and time.monotonic() >= self._step_deadline

    def _poll_cancelled(self) -> bool:
        return self.stop_requested() or self._step_timed_out()

    def run(
        self,
        action_callback: Callable[[int, str], None] | None = None,
        start_index: int = 0,
        end_index: int | None = None,
        include_start_delay: bool = True,
        respect_enabled: bool = True,
        retry_callback: Callable[[int, int, int, str], None] | None = None,
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
        index = start_index
        transitions = 0
        while index <= end_index:
            transitions += 1
            if transitions > max(1000, total * 100):
                action = self.project.actions[index]
                raise ReplayActionError(index, action, RuntimeError("failure jumps exceeded the safety limit"))
            action = self.project.actions[index]
            self.current_index = index
            if self.stop_requested():
                raise StopReplay()
            if respect_enabled and not action.enabled:
                record = self._start_step_record(action, index)
                self._finish_step_record(record, "Skipped")
                if action_callback:
                    action_callback(index, "skipped")
                index += 1
                continue
            if action_callback:
                action_callback(index, "running")
            record = self._start_step_record(action, index)
            self._capture_step_screenshot(record, index, "before", bool(action.data.get("capture_before")))
            self.log(f"action started: {index + 1} {action.action}")
            # Click Image steps rely on continuous polling with their own search
            # timeout (see _click_image/wait_for_image) instead of a fixed
            # pre-wait, so they click as soon as the target appears rather than
            # always waiting out the recorded delay.
            if action.action not in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
                self.sleep_checked(action.delay_before)
            try:
                control_data = resolve_placeholders_strict(action.data, self.runtime_variables)
            except MissingPlaceholderError:
                control_data = action.data
            retry_count = max(0, self._safe_int(control_data.get("retry_count", 0), 0))
            retry_delay = max(0.0, self._safe_float(control_data.get("retry_delay", 1.0), 1.0))
            step_timeout = max(0.0, self._safe_float(control_data.get("step_timeout", 0.0), 0.0))
            max_attempts = retry_count + 1
            final_error: Exception | None = None
            self._best_image_confidence = 0.0
            for attempt in range(1, max_attempts + 1):
                self.total_attempts += 1
                record["attempts"] = attempt
                self.log(f"[Step {index + 1}] Attempt {attempt}/{max_attempts}")
                self._step_deadline = time.monotonic() + step_timeout if step_timeout > 0 else None
                try:
                    self.run_action(
                        action, self.runtime_variables, index + 1,
                        allow_coordinate_fallback=(attempt == max_attempts),
                    )
                    if self._step_timed_out():
                        raise TimeoutError("step timed out")
                    final_error = None
                    break
                except MissingPlaceholderError as exc:
                    final_error = ValueError(f"missing variable '{exc.variable}'")
                except StopReplay:
                    raise
                except Exception as exc:
                    final_error = self._friendly_runtime_error(exc)
                finally:
                    self._step_deadline = None
                if attempt < max_attempts:
                    reason = str(final_error)
                    record["retry_attempts"].append({"attempt": attempt + 1, "reason": reason})
                    self.log(
                        f"[Step {index + 1}] Retry {attempt + 1}/{max_attempts} in {retry_delay:.2f}s: {reason}"
                    )
                    if retry_callback:
                        retry_callback(index, attempt + 1, max_attempts, reason)
                    self.sleep_checked(retry_delay)
            if final_error is not None:
                if action_callback:
                    action_callback(index, "failed")
                failure_message = str(final_error)
                screenshot_path = self._capture_failure_screenshot(action, index)
                record["error"] = failure_message
                if screenshot_path:
                    record["screenshots"]["failure"] = screenshot_path
                self._finish_step_record(record, "Failed", failure_message)
                if screenshot_path:
                    failure_message = f"{failure_message} (failure screenshot: {screenshot_path})"
                failure_action = str(control_data.get("failure_action", "stop")).strip().lower()
                if failure_action in {"continue", "jump"}:
                    self.had_continued_failures = True
                    if self.first_failed_index is None:
                        self.first_failed_index = index
                        self.first_failure_error = failure_message
                    self.log(f"[Step {index + 1}] Final failure: {failure_message}")
                    if failure_action == "jump":
                        jump_step = self._safe_int(control_data.get("failure_jump_step", 0), 0)
                        jump_index = jump_step - 1
                        if not start_index <= jump_index <= end_index:
                            raise ReplayActionError(
                                index, action, ValueError(f"jump target Step {jump_step} is outside this run range")
                            )
                        self.log(f"[Step {index + 1}] Jumping to Step {jump_step}")
                        index = jump_index
                        continue
                    index += 1
                    continue
                raise ReplayActionError(index, action, RuntimeError(failure_message))
            if action_callback:
                action_callback(index, "completed")
            self._capture_step_screenshot(record, index, "after", bool(action.data.get("capture_after")))
            self._finish_step_record(record, "Success")
            self.log(f"action completed: {index + 1} {action.action}")
            index += 1
        self.log("replay completed")

    def run_action(
        self,
        action: RpaAction,
        variables: dict[str, Any] | None = None,
        step_number: int = 1,
        allow_coordinate_fallback: bool = True,
    ) -> None:
        variables = self.runtime_variables if variables is None else variables
        data = resolve_placeholders_strict(action.data, variables)
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            self._click_image(action, data, allow_coordinate_fallback)
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
        elif action.action == ActionType.MOUSE_MOVE.value:
            get_pyautogui().moveTo(int(data.get("x", 0)), int(data.get("y", 0)), duration=float(data.get("duration", 0.2)))
        elif action.action == ActionType.DRAG.value:
            gui = get_pyautogui()
            gui.moveTo(int(data.get("start_x", 0)), int(data.get("start_y", 0)), duration=float(data.get("move_duration", 0.2)))
            gui.dragTo(int(data.get("end_x", 0)), int(data.get("end_y", 0)), duration=float(data.get("duration", 0.5)), button=str(data.get("button", "left")))
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
        if self._step_timed_out():
            raise TimeoutError("step timed out")
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
        if self._step_timed_out():
            raise TimeoutError("step timed out")
        text = output.getvalue().strip()
        if text:
            self.log(text)
        return output.getvalue()

    def _check_stop_for_code(self) -> bool:
        if self.stop_requested():
            raise StopReplay()
        if self._step_timed_out():
            raise TimeoutError("step timed out")
        return False

    def _friendly_runtime_error(self, exc: Exception) -> Exception:
        if exc.__class__.__name__ == "FailSafeException":
            return RuntimeError(
                "PyAutoGUI safety stop was triggered because the mouse reached a screen corner. "
                "Move the pointer away from the corners and run again, or disable failsafe in Settings."
            )
        return exc

    def _click_image(self, action: RpaAction, data: dict, allow_coordinate_fallback: bool = True) -> None:
        image_path = self.project_dir / str(data.get("image", ""))
        required_confidence = float(data.get("confidence", self.project.settings.default_confidence))
        match = wait_for_image(
            image_path,
            required_confidence,
            float(data.get("timeout", self.project.settings.default_timeout)),
            self._poll_cancelled,
            excluded_regions=self.excluded_regions,
        )
        if self.stop_requested():
            raise StopReplay()
        self._best_image_confidence = max(
            self._best_image_confidence, float(getattr(match, "confidence", 0.0) or 0.0),
        )
        if self._step_timed_out():
            raise TimeoutError(
                "step timed out while searching for the image target; "
                f"best confidence={self._best_image_confidence:.3f}, required={required_confidence:.3f}"
            )
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
        if allow_coordinate_fallback and data.get("use_coordinate_fallback", True):
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            get_pyautogui().click(int(data.get("fallback_x", 0)), int(data.get("fallback_y", 0)), button=str(data.get("button", "left")))
            return
        raise FileNotFoundError(
            f"Image not found: {image_path}; best confidence={self._best_image_confidence:.3f}, "
            f"required={required_confidence:.3f}"
        )

    def _capture_failure_screenshot(self, action: RpaAction, index: int) -> str | None:
        if self.evidence_dir is None and not action.data.get("capture_failure_screenshot", False):
            return None
        try:
            target_dir = (
                self.evidence_dir / "screenshots" if self.evidence_dir is not None
                else self.project_dir / "logs" / "failures"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"step_{index + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
            screenshot_image().save(path, "PNG")
            self.log(f"[Step {index + 1}] Failure screenshot saved: {path}")
            return self._evidence_relative_path(path)
        except Exception as exc:
            self.log(f"[Step {index + 1}] Could not save failure screenshot: {exc}")
            return None

    def _start_step_record(self, action: RpaAction, index: int) -> dict[str, Any]:
        record = {
            "step_number": index + 1,
            "step_name": action.summary(),
            "action": action.action,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None,
            "duration_seconds": None,
            "status": "Running",
            "attempts": 0,
            "retry_attempts": [],
            "error": None,
            "screenshots": {},
        }
        self.step_results.append(record)
        return record

    def _finish_step_record(self, record: dict[str, Any], status: str, error: str | None = None) -> None:
        ended = datetime.now(timezone.utc)
        record["ended_at"] = ended.isoformat()
        try:
            started = datetime.fromisoformat(str(record["started_at"]))
            record["duration_seconds"] = max(0.0, (ended - started).total_seconds())
        except (TypeError, ValueError):
            record["duration_seconds"] = None
        record["status"] = status
        record["error"] = error

    def _capture_step_screenshot(
        self, record: dict[str, Any], index: int, kind: str, enabled: bool,
    ) -> str | None:
        if not enabled or self.evidence_dir is None:
            return None
        try:
            target_dir = self.evidence_dir / "screenshots"
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"step_{index + 1}_{kind}.png"
            screenshot_image().save(path, "PNG")
            relative = self._evidence_relative_path(path)
            record["screenshots"][kind] = relative
            self.log(f"[Step {index + 1}] {kind.title()} screenshot saved: {relative}")
            return relative
        except Exception as exc:
            self.log(f"[Step {index + 1}] Could not save {kind} screenshot: {exc}")
            return None

    def _evidence_relative_path(self, path: Path) -> str:
        if self.evidence_dir is not None:
            try:
                return path.relative_to(self.evidence_dir).as_posix()
            except ValueError:
                pass
        return str(path)

    def _safe_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def _safe_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default


def get_pyautogui():
    global pyautogui
    if pyautogui is None:
        import pyautogui as module
        pyautogui = module
    return pyautogui
