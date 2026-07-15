from __future__ import annotations

from .models import TimingMode


def optimize_delay(seconds: float) -> float:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 0.1:
        return 0.0
    if seconds <= 0.5:
        return round(seconds, 3)
    if seconds <= 2.0:
        return 0.3
    return 0.0


def runtime_delay(recorded_delay: float, timing_mode: str) -> float:
    if timing_mode == TimingMode.NONE.value:
        return 0.0
    return max(0.0, float(recorded_delay or 0.0))
