"""Windows-native discovery and control used by window-aware actions.

Matching is kept separate from the Win32 backend so validation and tests do not
need a live desktop. Existing coordinate actions do not use this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable


class WindowTargetError(RuntimeError):
    pass


class WindowNotFoundError(WindowTargetError):
    pass


class AmbiguousWindowError(WindowTargetError):
    pass


class WindowPermissionError(WindowTargetError):
    pass


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process_name: str = ""
    class_name: str = ""
    process_id: int = 0
    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0
    minimized: bool = False
    maximized: bool = False

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def target(self) -> dict[str, Any]:
        return {
            "process_name": self.process_name,
            "window_title": self.title,
            "title_match": "exact",
            "class_name": self.class_name,
            "timeout": 10.0,
            "retry_interval": 0.25,
            "multiple_match": "error",
        }

    def evidence(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("handle", None)
        return data


WINDOW_TARGET_KEYS = {
    "process_name", "window_title", "title_match", "class_name",
    "timeout", "retry_interval", "multiple_match",
}


def normalize_window_target(data: dict[str, Any] | None) -> dict[str, Any]:
    data = data or {}
    nested = data.get("window")
    source = nested if isinstance(nested, dict) else data
    return {
        "process_name": str(source.get("process_name", "")).strip(),
        "window_title": str(source.get("window_title", "")).strip(),
        "title_match": str(source.get("title_match", "contains") or "contains").strip().lower(),
        "class_name": str(source.get("class_name", "")).strip(),
        "timeout": source.get("timeout", 10.0),
        "retry_interval": source.get("retry_interval", 0.25),
        "multiple_match": str(source.get("multiple_match", "error") or "error").strip().lower(),
    }


def describe_window_target(target: dict[str, Any]) -> str:
    target = normalize_window_target(target)
    parts = []
    if target["process_name"]:
        parts.append(target["process_name"])
    if target["window_title"]:
        mode = target["title_match"]
        parts.append(f"title {mode} '{target['window_title']}'")
    if target["class_name"]:
        parts.append(f"class '{target['class_name']}'")
    return ", ".join(parts) or "selected window"


def match_windows(windows: Iterable[WindowInfo], target: dict[str, Any]) -> list[WindowInfo]:
    target = normalize_window_target(target)
    process = target["process_name"].casefold()
    process_stem = Path(process).stem
    title = target["window_title"]
    title_mode = target["title_match"]
    class_name = target["class_name"].casefold()
    if title_mode not in {"exact", "contains", "regex"}:
        raise WindowTargetError(f"unsupported window-title match mode: {title_mode}")
    pattern = None
    if title and title_mode == "regex":
        try:
            pattern = re.compile(title, re.IGNORECASE)
        except re.error as exc:
            raise WindowTargetError(f"invalid window-title regular expression: {exc}") from exc
    matches = []
    for window in windows:
        candidate_process = window.process_name.casefold()
        if process and process not in {candidate_process, Path(candidate_process).stem} and process_stem != Path(candidate_process).stem:
            continue
        if class_name and window.class_name.casefold() != class_name:
            continue
        if title:
            if title_mode == "exact" and window.title.casefold() != title.casefold():
                continue
            if title_mode == "contains" and title.casefold() not in window.title.casefold():
                continue
            if pattern is not None and pattern.search(window.title) is None:
                continue
        matches.append(window)
    return matches


class WindowResolver:
    def __init__(self, backend=None, sleep: Callable[[float], None] | None = None) -> None:
        self.backend = backend or NativeWindowBackend()
        self.sleep = sleep or time.sleep

    def resolve(
        self,
        target: dict[str, Any],
        stop_requested: Callable[[], bool] | None = None,
    ) -> WindowInfo:
        normalized = normalize_window_target(target)
        if not any(normalized[key] for key in ("process_name", "window_title", "class_name")):
            raise WindowTargetError("window target needs a process name, title, or class name")
        try:
            timeout = max(0.0, float(normalized["timeout"]))
            retry = max(0.05, float(normalized["retry_interval"]))
        except (TypeError, ValueError) as exc:
            raise WindowTargetError("window timeout and retry interval must be numbers") from exc
        deadline = time.monotonic() + timeout
        while True:
            if stop_requested and stop_requested():
                from .runner import StopReplay  # avoid a module cycle at import time
                raise StopReplay()
            matches = match_windows(self.backend.enumerate(), normalized)
            if matches:
                return self._select_match(matches, normalized["multiple_match"])
            if time.monotonic() >= deadline:
                break
            self.sleep(min(retry, max(0.0, deadline - time.monotonic())))
        raise WindowNotFoundError(
            f"window not found after {timeout:.1f}s: {describe_window_target(normalized)}"
        )

    def _select_match(self, matches: list[WindowInfo], handling: str) -> WindowInfo:
        if len(matches) == 1:
            return matches[0]
        if handling == "first":
            return matches[0]
        if handling == "active":
            active = self.backend.foreground_handle()
            selected = next((item for item in matches if item.handle == active), None)
            if selected:
                return selected
            raise AmbiguousWindowError(
                f"{len(matches)} windows matched, but none is currently active"
            )
        names = ", ".join(repr(item.title or item.process_name) for item in matches[:4])
        raise AmbiguousWindowError(f"{len(matches)} windows matched ({names}); refine the title or class")

    def activate(self, window: WindowInfo) -> WindowInfo:
        if window.minimized:
            self.backend.show(window.handle, "restore")
        if not self.backend.activate(window.handle):
            raise WindowPermissionError(
                "Windows blocked activation of the target window. Run the recorder at the same permission level as the target application."
            )
        return self.backend.info(window.handle)

    def change_state(self, window: WindowInfo, state: str) -> WindowInfo:
        if state not in {"maximize", "minimize", "restore"}:
            raise WindowTargetError(f"unsupported window state: {state}")
        if not self.backend.show(window.handle, state):
            raise WindowPermissionError(f"Windows blocked the request to {state} the target window")
        return self.backend.info(window.handle)

    def close(self, window: WindowInfo) -> None:
        if not self.backend.close(window.handle):
            raise WindowPermissionError("Windows blocked the request to close the target window")


class NativeWindowBackend:
    """Small Win32 backend; importing it remains safe on non-Windows test hosts."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowTargetError("window-aware automation is available on Windows only")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND

    def enumerate(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []
        callback_type = self.ctypes.WINFUNCTYPE(self.wintypes.BOOL, self.wintypes.HWND, self.wintypes.LPARAM)

        @callback_type
        def callback(hwnd, _lparam):
            if self.user32.IsWindowVisible(hwnd) and self.user32.GetWindowTextLengthW(hwnd) > 0:
                try:
                    info = self.info(int(hwnd))
                    if info.width > 0 and info.height > 0:
                        windows.append(info)
                except OSError:
                    pass
            return True

        self.user32.EnumWindows(callback, 0)
        return windows

    def info(self, handle: int) -> WindowInfo:
        hwnd = self.wintypes.HWND(handle)
        if not self.user32.IsWindow(hwnd):
            raise WindowNotFoundError("the selected window no longer exists")
        length = max(1, self.user32.GetWindowTextLengthW(hwnd) + 1)
        title_buffer = self.ctypes.create_unicode_buffer(length)
        self.user32.GetWindowTextW(hwnd, title_buffer, length)
        class_buffer = self.ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        process_id = self.wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(process_id))
        rect = self.wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect)):
            raise OSError("could not read the target window bounds")
        return WindowInfo(
            handle=int(handle), title=title_buffer.value,
            process_name=self._process_name(int(process_id.value)), class_name=class_buffer.value,
            process_id=int(process_id.value), left=int(rect.left), top=int(rect.top),
            width=int(rect.right - rect.left), height=int(rect.bottom - rect.top),
            minimized=bool(self.user32.IsIconic(hwnd)), maximized=bool(self.user32.IsZoomed(hwnd)),
        )

    def _process_name(self, process_id: int) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        process = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not process:
            return ""
        try:
            size = self.wintypes.DWORD(32768)
            buffer = self.ctypes.create_unicode_buffer(size.value)
            if self.kernel32.QueryFullProcessImageNameW(process, 0, buffer, self.ctypes.byref(size)):
                return Path(buffer.value).name
            return ""
        finally:
            self.kernel32.CloseHandle(process)

    def foreground_handle(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def window_at_point(self, x: int, y: int, exclude_process_id: int | None = None) -> WindowInfo:
        # EnumWindows is in top-to-bottom Z order. Excluding our own process
        # avoids selecting the transparent picker or its still-modal parent.
        for window in self.enumerate():
            if exclude_process_id and window.process_id == exclude_process_id:
                continue
            if window.contains(int(x), int(y)):
                return window
        raise WindowNotFoundError(f"no visible application window was found at ({x}, {y})")

    def cursor_position(self) -> tuple[int, int]:
        point = self.wintypes.POINT()
        getter = getattr(self.user32, "GetPhysicalCursorPos", self.user32.GetCursorPos)
        if not getter(self.ctypes.byref(point)):
            raise OSError("could not read the cursor position")
        return int(point.x), int(point.y)

    def activate(self, handle: int) -> bool:
        self.user32.BringWindowToTop(self.wintypes.HWND(handle))
        result = bool(self.user32.SetForegroundWindow(self.wintypes.HWND(handle)))
        return result or self.foreground_handle() == int(handle)

    def show(self, handle: int, state: str) -> bool:
        command = {"restore": 9, "minimize": 6, "maximize": 3}[state]
        self.user32.ShowWindow(self.wintypes.HWND(handle), command)
        return bool(self.user32.IsWindow(self.wintypes.HWND(handle)))

    def close(self, handle: int) -> bool:
        return bool(self.user32.PostMessageW(self.wintypes.HWND(handle), 0x0010, 0, 0))  # WM_CLOSE
