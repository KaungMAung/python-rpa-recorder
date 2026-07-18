from __future__ import annotations

import time
from dataclasses import dataclass, field
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
    reference_image: str = ""
    reference_index: int = 0
    match_index: int = 0


@dataclass
class ImageDiagnostic:
    matches: list[ImageMatch] = field(default_factory=list)
    selected: ImageMatch = field(default_factory=lambda: ImageMatch(False))
    duration: float = 0.0
    warnings: list[str] = field(default_factory=list)


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
    diagnostic = find_reference_matches(
        [image_path], confidence, excluded_regions=excluded_regions,
        diagnostic_min_confidence=confidence,
    )
    return diagnostic.selected


def find_reference_matches(
    reference_paths: list[Path] | tuple[Path, ...],
    confidence: float = 0.86,
    excluded_regions: list[tuple[int, int, int, int]] | None = None,
    search_region: dict | tuple[int, int, int, int] | None = None,
    grayscale: bool = False,
    match_priority: str = "highest_confidence",
    match_index: int = 1,
    diagnostic_min_confidence: float = 0.5,
    max_matches: int = 50,
    screen: Image.Image | None = None,
) -> ImageDiagnostic:
    """Match ordered references against one screenshot and retain diagnostics."""
    started = time.monotonic()
    screen = screen or screenshot_image()
    origin_x, origin_y = virtual_screen_origin()
    region = _normalize_search_region(search_region, screen.width, screen.height, origin_x, origin_y)
    region_x, region_y, region_width, region_height = region
    image_left, image_top = region_x - origin_x, region_y - origin_y
    cropped = screen.crop((image_left, image_top, image_left + region_width, image_top + region_height)).convert("RGB")
    haystack_rgb = np.array(cropped)
    haystack = cv2.cvtColor(haystack_rgb, cv2.COLOR_RGB2GRAY if grayscale else cv2.COLOR_RGB2BGR)
    all_matches: list[ImageMatch] = []
    by_reference: dict[int, list[ImageMatch]] = {}
    warnings: list[str] = []
    for reference_index, raw_path in enumerate(reference_paths):
        path = Path(raw_path)
        if not path.is_file():
            warnings.append(f"Reference image is missing: {path}")
            continue
        try:
            with Image.open(path) as opened:
                needle_rgb = np.array(opened.convert("RGB"))
        except (OSError, ValueError) as exc:
            warnings.append(f"Reference image could not be read: {path} ({exc})")
            continue
        needle = cv2.cvtColor(needle_rgb, cv2.COLOR_RGB2GRAY if grayscale else cv2.COLOR_RGB2BGR)
        height, width = needle.shape[:2]
        if width > haystack.shape[1] or height > haystack.shape[0]:
            warnings.append(
                f"Reference image {path.name} ({width}x{height}) is larger than the search area "
                f"({haystack.shape[1]}x{haystack.shape[0]}); check display scaling or resolution."
            )
            continue
        result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
        result = np.nan_to_num(result, nan=-1.0, posinf=1.0, neginf=-1.0)
        _mask_excluded_matches(
            result, width, height, excluded_regions or [], region_x, region_y,
        )
        candidates = _extract_candidates(
            result, width, height, region_x, region_y,
            max(0.0, min(1.0, float(diagnostic_min_confidence))), max_matches,
            str(path), reference_index,
        )
        by_reference[reference_index] = candidates
        all_matches.extend(candidates)

    qualifying_reference = next(
        (index for index in range(len(reference_paths)) if any(item.confidence >= confidence for item in by_reference.get(index, []))),
        None,
    )
    if qualifying_reference is not None:
        selected = _select_match(by_reference[qualifying_reference], match_priority, match_index)
        selected.found = selected.confidence >= confidence
    elif all_matches:
        selected = max(all_matches, key=lambda item: item.confidence)
        selected.found = False
    else:
        selected = ImageMatch(False)
    duration = time.monotonic() - started
    selected.duration = duration
    all_matches.sort(key=lambda item: (item.reference_index, -item.confidence, item.y, item.x))
    for item in all_matches:
        item.duration = duration
    return ImageDiagnostic(all_matches, selected, duration, warnings)


def _normalize_search_region(
    region: dict | tuple[int, int, int, int] | None,
    screen_width: int, screen_height: int, origin_x: int, origin_y: int,
) -> tuple[int, int, int, int]:
    if isinstance(region, dict):
        values = (region.get("x", origin_x), region.get("y", origin_y), region.get("width", screen_width), region.get("height", screen_height))
    elif isinstance(region, (tuple, list)) and len(region) == 4:
        values = tuple(region)
    else:
        return origin_x, origin_y, screen_width, screen_height
    try:
        x, y, width, height = (int(float(value)) for value in values)
    except (TypeError, ValueError):
        return origin_x, origin_y, screen_width, screen_height
    left = max(origin_x, x)
    top = max(origin_y, y)
    right = min(origin_x + screen_width, x + max(1, width))
    bottom = min(origin_y + screen_height, y + max(1, height))
    if right <= left or bottom <= top:
        return origin_x, origin_y, screen_width, screen_height
    return left, top, right - left, bottom - top


def _extract_candidates(
    result: np.ndarray, template_width: int, template_height: int,
    origin_x: int, origin_y: int, minimum: float, maximum: int,
    reference_image: str, reference_index: int,
) -> list[ImageMatch]:
    work = result.copy()
    matches: list[ImageMatch] = []
    best_added = False
    for _ in range(max(1, maximum)):
        _, max_value, _, location = cv2.minMaxLoc(work)
        if max_value < minimum and (best_added or matches):
            break
        if max_value < -0.5:
            break
        matches.append(ImageMatch(
            False, location[0] + origin_x, location[1] + origin_y,
            template_width, template_height, float(max_value),
            reference_image=reference_image, reference_index=reference_index,
        ))
        best_added = True
        left = max(0, location[0] - template_width + 1)
        top = max(0, location[1] - template_height + 1)
        right = min(work.shape[1], location[0] + template_width)
        bottom = min(work.shape[0], location[1] + template_height)
        work[top:bottom, left:right] = -1.0
        if max_value < minimum:
            break
    for index, item in enumerate(matches, start=1):
        item.match_index = index
    return matches


def _select_match(matches: list[ImageMatch], priority: str, index: int) -> ImageMatch:
    if not matches:
        return ImageMatch(False)
    priority = str(priority or "highest_confidence")
    if priority == "leftmost":
        return min(matches, key=lambda item: (item.x, -item.confidence))
    if priority == "rightmost":
        return max(matches, key=lambda item: (item.x, item.confidence))
    if priority == "topmost":
        return min(matches, key=lambda item: (item.y, -item.confidence))
    if priority == "bottommost":
        return max(matches, key=lambda item: (item.y, item.confidence))
    ordered = sorted(matches, key=lambda item: item.confidence, reverse=True)
    if priority == "match_index":
        return ordered[max(0, min(int(index or 1) - 1, len(ordered) - 1))]
    return ordered[0]


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
    grayscale: bool = False,
    search_region: dict | tuple[int, int, int, int] | None = None,
    match_priority: str = "highest_confidence",
    match_index: int = 1,
) -> ImageMatch:
    started = time.monotonic()
    deadline = time.monotonic() + max(0.0, timeout)
    best = ImageMatch(False)
    while time.monotonic() <= deadline:
        if stop_requested and stop_requested():
            best.duration = time.monotonic() - started
            return best
        if grayscale or search_region or match_priority != "highest_confidence" or match_index != 1:
            diagnostic = find_reference_matches(
                [image_path], confidence, excluded_regions, search_region, grayscale,
                match_priority, match_index, confidence,
            )
            current = diagnostic.selected
        else:
            current = find_image(image_path, confidence, excluded_regions)
        if current.confidence >= best.confidence:
            best = current
        if current.found:
            current.duration = time.monotonic() - started
            return current
        remaining = max(0.0, min(poll_interval, deadline - time.monotonic()))
        sleep_deadline = time.monotonic() + remaining
        while time.monotonic() < sleep_deadline:
            if stop_requested and stop_requested():
                best.duration = time.monotonic() - started
                return best
            time.sleep(min(0.02, sleep_deadline - time.monotonic()))
    best.duration = time.monotonic() - started
    return best


def wait_for_references(
    reference_paths: list[Path], confidence: float, timeout: float,
    stop_requested: Callable[[], bool] | None = None, poll_interval: float = 0.1,
    excluded_regions: list[tuple[int, int, int, int]] | None = None,
    grayscale: bool = False, search_region: dict | None = None,
    match_priority: str = "highest_confidence", match_index: int = 1,
) -> tuple[ImageMatch, list[str]]:
    started = time.monotonic()
    deadline = started + max(0.0, timeout)
    best = ImageMatch(False)
    warnings: list[str] = []
    while time.monotonic() <= deadline:
        if stop_requested and stop_requested():
            best.duration = time.monotonic() - started
            return best, warnings
        diagnostic = find_reference_matches(
            reference_paths, confidence, excluded_regions, search_region, grayscale,
            match_priority, match_index, confidence,
        )
        warnings = diagnostic.warnings
        current = diagnostic.selected
        if current.confidence >= best.confidence:
            best = current
        if current.found:
            current.duration = time.monotonic() - started
            return current, warnings
        remaining = max(0.0, min(poll_interval, deadline - time.monotonic()))
        sleep_deadline = time.monotonic() + remaining
        while time.monotonic() < sleep_deadline:
            if stop_requested and stop_requested():
                best.duration = time.monotonic() - started
                return best, warnings
            time.sleep(min(0.02, sleep_deadline - time.monotonic()))
    best.duration = time.monotonic() - started
    return best, warnings
