from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import os

import cv2
import numpy as np
from PIL import Image, ImageGrab


@dataclass
class ImageMatch:
    found: bool
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: float = 0.0
    duration: float = 0.0


def screenshot_image() -> Image.Image:
    try:
        return ImageGrab.grab(all_screens=os.name == "nt")
    except TypeError:  # Pillow versions without all_screens support
        return ImageGrab.grab()


def virtual_screen_origin() -> tuple[int, int]:
    if os.name != "nt":
        return 0, 0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
    except Exception:
        return 0, 0


def crop_image_around(
    screen: Image.Image,
    x: int,
    y: int,
    width: int,
    height: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> tuple[Image.Image, int, int]:
    image_x = int(x - origin_x)
    image_y = int(y - origin_y)
    width = max(1, min(int(width), screen.width))
    height = max(1, min(int(height), screen.height))
    left = max(0, int(image_x - width / 2))
    top = max(0, int(image_y - height / 2))
    right = min(screen.width, left + width)
    bottom = min(screen.height, top + height)
    left = max(0, right - width)
    top = max(0, bottom - height)
    return screen.crop((left, top, right, bottom)), left + origin_x, top + origin_y


def crop_around(x: int, y: int, width: int, height: int) -> tuple[Image.Image, int, int]:
    screen = screenshot_image()
    origin_x, origin_y = virtual_screen_origin()
    return crop_image_around(screen, x, y, width, height, origin_x, origin_y)


def save_click_crop(path: Path, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    image, left, top = crop_around(x, y, width, height)
    image.save(path, "PNG")
    return int(x - left), int(y - top), image.width, image.height


def save_crop_from_image(
    path: Path,
    screen: Image.Image,
    x: int,
    y: int,
    width: int,
    height: int,
    origin_x: int = 0,
    origin_y: int = 0,
) -> tuple[int, int, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    image, left, top = crop_image_around(screen, x, y, width, height, origin_x, origin_y)
    image.save(path, "PNG")
    return int(x - left), int(y - top), image.width, image.height


def find_image(
    image_path: Path,
    confidence: float = 0.86,
    excluded_regions: list[tuple[int, int, int, int]] | None = None,
) -> ImageMatch:
    started = time.monotonic()
    image_path = Path(image_path)
    if not image_path.exists():
        return ImageMatch(False, duration=time.monotonic() - started)
    haystack = np.array(screenshot_image().convert("RGB"))
    needle = np.array(Image.open(image_path).convert("RGB"))
    haystack = cv2.cvtColor(haystack, cv2.COLOR_RGB2BGR)
    needle = cv2.cvtColor(needle, cv2.COLOR_RGB2BGR)
    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    origin_x, origin_y = virtual_screen_origin()
    _mask_excluded_matches(result, needle.shape[1], needle.shape[0], excluded_regions or [], origin_x, origin_y)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    duration = time.monotonic() - started
    found = max_val >= confidence
    return ImageMatch(found, max_loc[0] + origin_x, max_loc[1] + origin_y, needle.shape[1], needle.shape[0], float(max_val), duration)


def _mask_excluded_matches(
    result: np.ndarray,
    template_width: int,
    template_height: int,
    excluded_regions: list[tuple[int, int, int, int]],
    origin_x: int,
    origin_y: int,
) -> None:
    result_height, result_width = result.shape[:2]
    for screen_x, screen_y, width, height in excluded_regions:
        if width <= 0 or height <= 0:
            continue
        exclude_x = int(screen_x - origin_x)
        exclude_y = int(screen_y - origin_y)
        left = max(0, exclude_x - template_width + 1)
        top = max(0, exclude_y - template_height + 1)
        right = min(result_width, exclude_x + int(width))
        bottom = min(result_height, exclude_y + int(height))
        if left < right and top < bottom:
            result[top:bottom, left:right] = -1.0


def wait_for_image(
    image_path: Path,
    confidence: float,
    timeout: float,
    stop_requested: Callable[[], bool] | None = None,
    poll_interval: float = 0.1,
    excluded_regions: list[tuple[int, int, int, int]] | None = None,
) -> ImageMatch:
    deadline = time.monotonic() + max(0.0, timeout)
    last = ImageMatch(False)
    while time.monotonic() <= deadline:
        if stop_requested and stop_requested():
            return last
        last = find_image(image_path, confidence, excluded_regions)
        if last.found:
            return last
        time.sleep(poll_interval)
    return last
