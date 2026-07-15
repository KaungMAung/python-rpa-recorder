from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from .image_matcher import save_click_crop
from .models import ActionType, ProjectSettings, RecorderState, RpaAction
from .timing import runtime_delay
from .utils import foreground_elevation_mismatch, should_ignore_foreground

keyboard = None
mouse = None


SPECIAL_KEYS = {
    "enter": "enter", "tab": "tab", "backspace": "backspace", "delete": "delete",
    "esc": "escape", "escape": "escape", "up": "up", "down": "down", "left": "left",
    "right": "right", "home": "home", "end": "end", "page_up": "pageup",
    "page_down": "pagedown", "space": "space",
}
MODIFIERS = {"ctrl", "ctrl_l", "ctrl_r", "shift", "shift_l", "shift_r", "alt", "alt_l", "alt_r", "cmd", "cmd_l", "cmd_r"}


def normalize_key(key: object) -> str | None:
    char = getattr(key, "char", None)
    if char:
        return str(char)
    name = getattr(key, "name", None) or str(key).replace("Key.", "")
    name = str(name).lower()
    if name.startswith("f") and name[1:].isdigit():
        return name
    return SPECIAL_KEYS.get(name, name.replace("_", ""))


def normalize_modifier(key_name: str) -> str:
    if key_name.startswith("ctrl"):
        return "ctrl"
    if key_name.startswith("shift"):
        return "shift"
    if key_name.startswith("alt"):
        return "alt"
    if key_name.startswith("cmd"):
        return "win"
    return key_name


class TextBuffer:
    def __init__(self, flush_timeout: float = 0.7) -> None:
        self.flush_timeout = flush_timeout
        self.text = ""
        self.last_input = 0.0

    def add(self, char: str, now: float | None = None) -> str | None:
        now = now or time.monotonic()
        if self.text and now - self.last_input > self.flush_timeout:
            flushed = self.flush()
            self.text += char
            self.last_input = now
            return flushed
        self.text += char
        self.last_input = now
        return None

    def flush(self) -> str | None:
        if not self.text:
            return None
        text = self.text
        self.text = ""
        return text


class RpaRecorder:
    def __init__(
        self,
        project_dir: Path,
        settings: ProjectSettings,
        on_action: Callable[[RpaAction], None],
        on_log: Callable[[str], None],
        ignore_app: Callable[[], bool] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.settings = settings
        self.on_action = on_action
        self.on_log = on_log
        self.ignore_app = ignore_app
        self.on_error = on_error or on_log
        self.state = RecorderState.IDLE
        self.text = TextBuffer(settings.text_flush_timeout)
        self._lock = threading.RLock()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._pressed: set[str] = set()
        self._last_action_time = time.monotonic()
        self._last_click: tuple[float, int, int, str] | None = None
        self._screenshot_index = 0
        self._warned_permission_pids: set[int] = set()

    def start(self) -> None:
        with self._lock:
            if self.state == RecorderState.RECORDING:
                raise RuntimeError("Recorder is already active")
            global keyboard, mouse
            if keyboard is None or mouse is None:
                try:
                    from pynput import keyboard as keyboard_module, mouse as mouse_module
                    keyboard = keyboard_module
                    mouse = mouse_module
                except Exception as exc:  # pragma: no cover - depends on desktop session
                    raise RuntimeError(f"pynput is not available: {exc}") from exc
            if keyboard is None or mouse is None:
                raise RuntimeError("pynput is not available")
            self.state = RecorderState.RECORDING
            self._last_action_time = time.monotonic()
            try:
                self._mouse_listener = mouse.Listener(on_click=self._on_click, on_scroll=self._on_scroll)
                self._keyboard_listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
                self._mouse_listener.start()
                self._keyboard_listener.start()
                self._mouse_listener.wait()
                self._keyboard_listener.wait()
                self.on_log("recorder started")
            except Exception as exc:
                self.state = RecorderState.FAILED
                self._stop_listeners(join=False)
                raise RuntimeError(f"Global input hooks could not start: {exc}") from exc

    def pause(self) -> None:
        with self._lock:
            if self.state != RecorderState.RECORDING:
                return
            self._flush_text_locked()
            self.state = RecorderState.PAUSED
            self.on_log("recorder paused")

    def resume(self) -> None:
        with self._lock:
            if self.state != RecorderState.PAUSED:
                return
            self.state = RecorderState.RECORDING
            self._last_action_time = time.monotonic()
            self.on_log("recorder resumed")

    def stop(self, completed: bool = True) -> None:
        with self._lock:
            if self.state not in (RecorderState.RECORDING, RecorderState.PAUSED):
                return
            self.state = RecorderState.STOPPING
            self._flush_text_locked()
            self._stop_listeners(join=True)
            self.state = RecorderState.COMPLETED if completed else RecorderState.IDLE
            self.on_log("recorder stopped" if completed else "recording cancelled")

    def abort(self) -> None:
        with self._lock:
            self._stop_listeners(join=False)
            self.state = RecorderState.FAILED

    def _stop_listeners(self, join: bool) -> None:
        listeners = (self._mouse_listener, self._keyboard_listener)
        for listener in listeners:
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass
        if join:
            for listener in listeners:
                if listener:
                    try:
                        listener.join(timeout=1.0)
                    except (RuntimeError, TypeError):
                        pass
        self._mouse_listener = None
        self._keyboard_listener = None

    def _should_ignore(self) -> bool:
        if not self.settings.ignore_application_window:
            return False
        if self.ignore_app and self.ignore_app():
            return True
        if should_ignore_foreground(os.getpid()):
            return True
        mismatch = foreground_elevation_mismatch()
        if mismatch and mismatch[0] not in self._warned_permission_pids:
            self._warned_permission_pids.add(mismatch[0])
            self.on_log(f"Windows permission warning: {mismatch[1]}")
        return False

    def _fail(self, message: str) -> None:
        if self.state == RecorderState.FAILED:
            return
        self.state = RecorderState.FAILED
        self._stop_listeners(join=False)
        try:
            self.on_error(message)
        except Exception:
            pass

    def _record(self, action: RpaAction) -> None:
        action.recorded_delay = max(0.0, time.monotonic() - self._last_action_time)
        action.delay_before = runtime_delay(action.recorded_delay, self.settings.timing_mode)
        self._last_action_time = time.monotonic()
        try:
            self.on_action(action)
            self.on_log(f"action recorded: {action.action}")
        except Exception as exc:
            self._fail(f"The recorded action could not be added: {exc}")

    def _flush_text_locked(self) -> None:
        text = self.text.flush()
        if text:
            self._record(RpaAction(ActionType.TYPE_TEXT.value, {"text": text, "interval": self.settings.typing_interval}))

    def _on_click(self, x: int, y: int, button: object, pressed: bool) -> None:
        if not pressed:
            return
        with self._lock:
            if self.state != RecorderState.RECORDING or self._should_ignore():
                return
            self._flush_text_locked()
            button_name = str(button).replace("Button.", "")
            now = time.monotonic()
            action_type = ActionType.CLICK_IMAGE.value
            if self._last_click:
                last_t, last_x, last_y, last_button = self._last_click
                if now - last_t <= self.settings.double_click_interval and abs(x - last_x) <= 4 and abs(y - last_y) <= 4 and button_name == last_button:
                    action_type = ActionType.DOUBLE_CLICK_IMAGE.value
            self._last_click = (now, x, y, button_name)
            self._screenshot_index += 1
            rel = Path("screenshots") / f"click_{self._screenshot_index:04d}.png"
            path = self.project_dir / rel
            try:
                offset_x, offset_y, _, _ = save_click_crop(path, x, y, self.settings.crop_width, self.settings.crop_height)
            except Exception as exc:
                self._fail(f"Screenshot capture failed at ({x}, {y}): {exc}")
                return
            self._record(RpaAction(action_type, {
                "image": rel.as_posix(),
                "button": button_name,
                "fallback_x": int(x),
                "fallback_y": int(y),
                "click_offset_x": offset_x,
                "click_offset_y": offset_y,
                "confidence": self.settings.default_confidence,
                "timeout": self.settings.default_timeout,
                "use_coordinate_fallback": self.settings.coordinate_fallback,
            }))

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        with self._lock:
            if self.state != RecorderState.RECORDING or self._should_ignore():
                return
            self._flush_text_locked()
            self._record(RpaAction(ActionType.SCROLL.value, {"amount": int(dy), "x": int(x), "y": int(y), "move_to": True}))

    def _on_press(self, key: object) -> None:
        with self._lock:
            if self.state != RecorderState.RECORDING or self._should_ignore():
                return
            key_name = normalize_key(key)
            if not key_name:
                return
            if key_name in MODIFIERS:
                self._pressed.add(normalize_modifier(key_name))
                return
            if self._pressed:
                keys = list(dict.fromkeys([*sorted(self._pressed), key_name.lower()]))
                self._flush_text_locked()
                self._record(RpaAction(ActionType.HOTKEY.value, {"keys": keys}))
                return
            if len(key_name) == 1 and key_name.isprintable():
                flushed = self.text.add(key_name)
                if flushed:
                    self._record(RpaAction(ActionType.TYPE_TEXT.value, {"text": flushed, "interval": self.settings.typing_interval}))
                return
            self._flush_text_locked()
            self._record(RpaAction(ActionType.PRESS_KEY.value, {"key": key_name, "count": 1, "interval": 0.0}))

    def _on_release(self, key: object) -> None:
        key_name = normalize_key(key)
        if key_name:
            self._pressed.discard(normalize_modifier(key_name))
