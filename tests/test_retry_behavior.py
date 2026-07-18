from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import rpa.runner as runner_module
import rpa.image_matcher as image_matcher_module
from rpa.image_matcher import ImageMatch
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.runner import ReplayActionError, ReplayRunner, StopReplay


def make_runner(tmp_path: Path, actions: list[RpaAction], logs: list[str] | None = None) -> ReplayRunner:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    return ReplayRunner(RpaProject(actions=actions), tmp_path, (logs if logs is not None else []).append)


def test_step_retries_until_success_and_reports_attempts(tmp_path: Path) -> None:
    logs: list[str] = []
    action = RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "variables['tries'] = variables.get('tries', 0) + 1\nif variables['tries'] < 3: raise RuntimeError('not yet')",
        "retry_count": 2,
        "retry_delay": 0,
    })
    runner = make_runner(tmp_path, [action], logs)
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["tries"] == 3
    assert runner.total_attempts == 3
    assert sum("Retry" in message for message in logs) == 2


def test_continue_failure_runs_later_steps_and_retains_failure(tmp_path: Path) -> None:
    actions = [
        RpaAction(ActionType.PYTHON_CODE.value, {
            "code": "raise RuntimeError('broken')", "failure_action": "continue",
        }),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['continued'] = True"}),
    ]
    runner = make_runner(tmp_path, actions)
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["continued"] is True
    assert runner.had_continued_failures
    assert runner.first_failed_index == 0
    assert "broken" in (runner.first_failure_error or "")


def test_jump_failure_skips_to_configured_step(tmp_path: Path) -> None:
    actions = [
        RpaAction(ActionType.PYTHON_CODE.value, {
            "code": "raise RuntimeError('jump')", "failure_action": "jump", "failure_jump_step": 3,
        }),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['wrong'] = True"}),
        RpaAction(ActionType.PYTHON_CODE.value, {"code": "variables['recovered'] = True"}),
    ]
    runner = make_runner(tmp_path, actions)
    runner.run(include_start_delay=False)
    assert "wrong" not in runner.runtime_variables
    assert runner.runtime_variables["recovered"] is True
    assert runner.had_continued_failures


def test_step_timeout_interrupts_checked_wait(tmp_path: Path) -> None:
    action = RpaAction(ActionType.WAIT.value, {"seconds": 2, "step_timeout": 0.05})
    runner = make_runner(tmp_path, [action])
    started = time.monotonic()
    with pytest.raises(ReplayActionError, match="timed out"):
        runner.run(include_start_delay=False)
    assert time.monotonic() - started < 0.5


def test_stop_interrupts_retry_delay_immediately(tmp_path: Path) -> None:
    retry_seen = threading.Event()
    action = RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "raise RuntimeError('again')", "retry_count": 5, "retry_delay": 5,
    })
    runner = make_runner(tmp_path, [action])
    stopped: list[bool] = []

    def execute() -> None:
        try:
            runner.run(include_start_delay=False, retry_callback=lambda *_: retry_seen.set())
        except StopReplay:
            stopped.append(True)

    thread = threading.Thread(target=execute)
    thread.start()
    assert retry_seen.wait(1)
    runner.request_stop()
    thread.join(1)
    assert not thread.is_alive()
    assert stopped == [True]


def test_image_fallback_occurs_only_after_final_retry(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True, click=lambda *a, **k: calls.append("click"))
    monkeypatch.setattr(
        runner_module, "wait_for_image",
        lambda *a, **k: ImageMatch(False, confidence=0.72, duration=0.01),
    )
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/target.png", "confidence": 0.9, "timeout": 0.1,
        "retry_count": 2, "retry_delay": 0, "use_coordinate_fallback": True,
        "fallback_x": 10, "fallback_y": 20,
    })
    runner = ReplayRunner(RpaProject(actions=[action]), tmp_path, lambda _message: None)
    runner.run(include_start_delay=False)
    assert runner.total_attempts == 3
    assert calls == ["click"]


def test_image_failure_keeps_best_confidence_across_retries(tmp_path: Path, monkeypatch) -> None:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    matches = iter([
        ImageMatch(False, confidence=0.82, duration=0.01),
        ImageMatch(False, confidence=0.41, duration=0.01),
    ])
    monkeypatch.setattr(runner_module, "wait_for_image", lambda *a, **k: next(matches))
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "target.png", "confidence": 0.9, "timeout": 0.1,
        "retry_count": 1, "retry_delay": 0, "use_coordinate_fallback": False,
    })
    runner = ReplayRunner(RpaProject(actions=[action]), tmp_path, lambda _message: None)
    with pytest.raises(ReplayActionError) as exc_info:
        runner.run(include_start_delay=False)
    assert "best confidence=0.820" in str(exc_info.value)


def test_final_failure_screenshot_is_saved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "screenshot_image", lambda: Image.new("RGB", (10, 10), "red"))
    action = RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "raise RuntimeError('boom')", "capture_failure_screenshot": True,
    })
    runner = make_runner(tmp_path, [action])
    with pytest.raises(ReplayActionError) as exc_info:
        runner.run(include_start_delay=False)
    screenshots = list((tmp_path / "logs" / "failures").glob("*.png"))
    assert len(screenshots) == 1
    assert "failure screenshot" in str(exc_info.value)


def test_image_polling_returns_best_confidence_seen(tmp_path: Path, monkeypatch) -> None:
    matches = iter([
        ImageMatch(False, confidence=0.2),
        ImageMatch(False, confidence=0.81),
        ImageMatch(False, confidence=0.4),
    ])
    monkeypatch.setattr(
        image_matcher_module, "find_image",
        lambda *args, **kwargs: next(matches, ImageMatch(False, confidence=0.4)),
    )
    best = image_matcher_module.wait_for_image(tmp_path / "target.png", 0.9, 0.02, poll_interval=0.001)
    assert best.confidence == pytest.approx(0.81)


def test_replay_worker_emits_retry_progress_and_final_failure(tmp_path: Path) -> None:
    from ui.main_window import ReplayWorker

    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    project = RpaProject(actions=[RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "raise RuntimeError('broken')",
        "retry_count": 1,
        "retry_delay": 0,
        "failure_action": "continue",
    })])
    worker = ReplayWorker(project, tmp_path, include_start_delay=False)
    retries: list[tuple[int, int, int]] = []
    failures: list[tuple[int, str]] = []
    finished: list[bool] = []
    worker.retry_progress.connect(lambda index, attempt, total, _reason: retries.append((index, attempt, total)))
    worker.failed.connect(lambda index, message: failures.append((index, message)))
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    assert retries == [(0, 2, 2)]
    assert failures and failures[0][0] == 0 and "broken" in failures[0][1]
    assert finished == []
