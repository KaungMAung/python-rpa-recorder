"""Windows desktop preparation shared by recording and replay entry points."""
from __future__ import annotations

import sys


def recorder_window_handles(title: str = "Python RPA Recorder") -> list[int]:
    """Return visible, non-minimized recorder windows eligible for restoration."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == title:
                handles.append(int(hwnd))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return handles


def show_windows_desktop() -> int:
    """Minimize normal top-level windows without using the toggling Win+D shortcut."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    minimized = 0
    gw_owner = 4
    gwl_exstyle = -20
    ws_ex_toolwindow = 0x00000080
    sw_minimize = 6
    shell_classes = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

    def callback(hwnd, _lparam):
        nonlocal minimized
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindow(hwnd, gw_owner):
            return True
        if user32.GetWindowLongW(hwnd, gwl_exstyle) & ws_ex_toolwindow:
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        if class_buffer.value in shell_classes:
            return True
        # Ignore shell/host windows without a user-visible title. This avoids
        # disturbing desktop infrastructure while still handling normal apps.
        if user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        user32.ShowWindow(hwnd, sw_minimize)
        minimized += 1
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return minimized


def restore_recorder_windows(handles: list[int]) -> int:
    """Restore only recorder windows captured before desktop preparation."""
    if sys.platform != "win32" or not handles:
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    restored = 0
    sw_restore = 9
    for hwnd in handles:
        native_handle = wintypes.HWND(hwnd)
        if user32.IsWindow(native_handle):
            user32.ShowWindow(native_handle, sw_restore)
            user32.SetForegroundWindow(native_handle)
            restored += 1
    return restored
