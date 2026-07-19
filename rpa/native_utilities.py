"""Interruptible Windows utility operations used by native action steps."""
from __future__ import annotations

import ctypes
import os
import shlex
import shutil
import subprocess
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from .windowing import NativeWindowBackend, WindowNotFoundError, WindowResolver


class CommandTimeoutError(TimeoutError):
    def __init__(self, message: str, result: dict[str, Any]) -> None:
        self.result = result
        super().__init__(message)


def command_arguments(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return []
    if os.name != "nt":
        return shlex.split(text)
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    pointer = shell32.CommandLineToArgvW(text, ctypes.byref(count))
    if not pointer:
        raise ValueError("arguments use an invalid Windows command line")
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def run_command_interruptible(
    command: list[str], working_directory: str | None, timeout: float,
    cancelled: Callable[[], bool], environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command, cwd=working_directory or None, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=environment, shell=False, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except OSError as exc:
        raise RuntimeError(f"could not start command: {exc}") from exc
    deadline = started + timeout if timeout > 0 else None

    def stop_result() -> dict[str, Any]:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        return {
            "stdout": stdout, "stderr": stderr, "exit_code": int(process.returncode),
            "duration_seconds": max(0.0, time.monotonic() - started),
        }

    while True:
        try:
            was_cancelled = cancelled()
        except BaseException as exc:
            try:
                setattr(exc, "command_result", stop_result())
            except (AttributeError, TypeError):
                stop_result()
            raise
        if was_cancelled:
            result = stop_result()
            raise CommandTimeoutError("command was stopped or timed out", result)
        if deadline is not None and time.monotonic() >= deadline:
            result = stop_result()
            raise CommandTimeoutError(f"command timed out after {timeout:.1f}s", result)
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            duration = max(0.0, time.monotonic() - started)
            return {
                "stdout": stdout, "stderr": stderr, "exit_code": int(process.returncode),
                "duration_seconds": duration,
            }
        time.sleep(0.05)


def enumerate_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        raise RuntimeError("process utility actions are available on Windows only")
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError("could not enumerate Windows processes")

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    results: list[dict[str, Any]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    try:
        success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            results.append({"pid": int(entry.th32ProcessID), "name": entry.szExeFile})
            success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return results


def find_process(process_name: str) -> list[dict[str, Any]]:
    wanted = Path(process_name).name.casefold()
    wanted_stem = Path(wanted).stem
    return [
        item for item in enumerate_processes()
        if item["name"].casefold() == wanted or Path(item["name"]).stem.casefold() == wanted_stem
    ]


def wait_for_process(
    process_name: str, timeout: float, retry_interval: float,
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        matches = find_process(process_name)
        if matches:
            return matches[0]
        if cancelled():
            raise TimeoutError("process wait was stopped or timed out")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"process not found after {timeout:.1f}s: {process_name}")
        time.sleep(min(max(0.05, retry_interval), max(0.0, deadline - time.monotonic())))


def process_window(process_name: str, close: bool = False) -> dict[str, Any]:
    backend = NativeWindowBackend()
    windows = [
        item for item in backend.enumerate()
        if Path(item.process_name).stem.casefold() == Path(process_name).stem.casefold()
    ]
    if not windows:
        raise WindowNotFoundError(f"no visible window belongs to process: {process_name}")
    resolver = WindowResolver(backend)
    if close:
        for window in windows:
            resolver.close(window)
        return {"process_name": process_name, "windows_closed": len(windows)}
    selected = resolver.activate(windows[0])
    return {"process_name": process_name, "pid": selected.process_id, "window_title": selected.title}


def read_clipboard_text() -> str:
    if os.name != "nt":
        raise RuntimeError("clipboard utility actions are available on Windows only")
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = ctypes.c_void_p
    if not user32.OpenClipboard(None):
        raise PermissionError("Windows clipboard is busy; try again")
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("could not read clipboard memory")
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def write_clipboard_text(text: str) -> None:
    if os.name != "nt":
        raise RuntimeError("clipboard utility actions are available on Windows only")
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = ctypes.c_void_p
    user32.SetClipboardData.restype = wintypes.HANDLE
    encoded = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(0x0002, len(encoded))
    if not handle:
        raise OSError("could not allocate clipboard memory")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError("could not lock clipboard memory")
    ctypes.memmove(pointer, encoded, len(encoded))
    kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise PermissionError("Windows clipboard is busy; try again")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(13, handle):
            kernel32.GlobalFree(handle)
            raise OSError("could not write clipboard data")
        handle = None  # Ownership transferred to Windows.
    finally:
        user32.CloseClipboard()


def copy_path(source: Path, destination: Path) -> Path:
    if source.is_dir():
        return Path(shutil.copytree(source, destination, dirs_exist_ok=False))
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy2(source, destination))


def move_path(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(shutil.move(str(source), str(destination)))


def delete_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def show_notification(title: str, message: str) -> None:
    if os.name != "nt":
        raise RuntimeError("desktop notifications are available on Windows only")
    # A short-lived native notification-area icon shows the standard Windows balloon.
    shell32, user32 = ctypes.windll.shell32, ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.LoadIconW.restype = wintypes.HICON

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ]

    data = NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(data)
    data.hWnd = user32.GetForegroundWindow()
    data.uID = int(time.time() * 1000) & 0xFFFFFFFF
    data.hIcon = user32.LoadIconW(None, 32512)  # IDI_APPLICATION
    data.uFlags = 0x17  # NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
    data.szTip = "Python RPA Recorder"
    data.szInfo = str(message)[:255]
    data.szInfoTitle = str(title)[:63]
    data.dwInfoFlags = 0x1  # NIIF_INFO
    data.uTimeout = 5000
    if not shell32.Shell_NotifyIconW(0, ctypes.byref(data)):
        raise OSError("Windows could not display the desktop notification")
    time.sleep(0.5)
    shell32.Shell_NotifyIconW(2, ctypes.byref(data))
