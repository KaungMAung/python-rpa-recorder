from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


class MissingPlaceholderError(ValueError):
    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(f"Missing placeholder variable: {variable}")


def ensure_project_dirs(project_dir: Path) -> None:
    for name in ("screenshots", "generated", "logs"):
        (project_dir / name).mkdir(parents=True, exist_ok=True)


def resolve_placeholders(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(lambda match: str(variables.get(match.group(1), match.group(0))), value)
    if isinstance(value, list):
        return [resolve_placeholders(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_placeholders(item, variables) for key, item in value.items()}
    return value


def resolve_placeholders_strict(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in variables:
                raise MissingPlaceholderError(key)
            return str(variables[key])
        return PLACEHOLDER_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [resolve_placeholders_strict(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: resolve_placeholders_strict(item, variables) for key, item in value.items()}
    return value


def create_file_logger(project_dir: Path, name: str = "python-rpa-recorder") -> tuple[logging.Logger, Path]:
    logs = project_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = logging.getLogger(f"{name}.{path.stem}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, path


def foreground_process_id() -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return None


def should_ignore_foreground(current_pid: int | None = None) -> bool:
    return foreground_process_id() == (current_pid or os.getpid())


def is_windows_admin() -> bool:
    if os.name != "nt":
        return False


def process_is_elevated(process_id: int) -> bool | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, int(process_id))
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
                return None
            elevation = wintypes.DWORD()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token,
                20,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(returned),
            ):
                return None
            return bool(elevation.value)
        finally:
            if token:
                kernel32.CloseHandle(token)
            kernel32.CloseHandle(process)
    except Exception:
        return None


def foreground_elevation_mismatch() -> tuple[int, str] | None:
    target_pid = foreground_process_id()
    if not target_pid or target_pid == os.getpid():
        return None
    recorder_elevated = process_is_elevated(os.getpid())
    target_elevated = process_is_elevated(target_pid)
    if recorder_elevated is None or target_elevated is None or recorder_elevated == target_elevated:
        return None
    if target_elevated:
        message = (
            "The target application is running as administrator, but Python RPA Recorder is not. "
            "Windows may block recording or replay. Run both applications at the same permission level."
        )
    else:
        message = (
            "Python RPA Recorder is running as administrator, but the target application is not. "
            "For predictable Windows input handling, run both applications at the same permission level."
        )
    return target_pid, message
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def logger_from_callback(callback: Callable[[str], None] | None) -> Callable[[str], None]:
    if callback:
        return callback
    return lambda message: print(message, file=sys.stderr)
