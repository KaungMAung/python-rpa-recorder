from __future__ import annotations

import ast
import os
from pathlib import Path

import numpy as np
from PIL import Image

from rpa.generator import generate_python
from rpa.image_matcher import find_reference_matches
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.validator import LEVEL_ERROR, validate_project_detailed

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _pattern() -> np.ndarray:
    return np.array([
        [[240, 20, 10], [10, 220, 30], [20, 30, 230]],
        [[80, 90, 100], [250, 245, 30], [15, 160, 220]],
        [[200, 40, 190], [40, 200, 170], [120, 30, 240]],
    ], dtype=np.uint8)


def test_diagnostics_find_all_matches_and_apply_priority(tmp_path: Path) -> None:
    target = _pattern()
    screen = np.zeros((24, 32, 3), dtype=np.uint8)
    screen[4:7, 5:8] = target
    screen[14:17, 20:23] = target
    reference = tmp_path / "target.png"
    Image.fromarray(target).save(reference)

    diagnostic = find_reference_matches(
        [reference], 0.9, screen=Image.fromarray(screen),
        match_priority="rightmost", diagnostic_min_confidence=0.8,
    )

    assert len(diagnostic.matches) == 2
    assert diagnostic.selected.found
    assert (diagnostic.selected.x, diagnostic.selected.y) == (20, 14)
    assert all(match.reference_image == str(reference) for match in diagnostic.matches)


def test_search_region_and_ordered_references(tmp_path: Path) -> None:
    target = _pattern()
    other = np.flip(target, axis=1).copy()
    screen = np.zeros((24, 32, 3), dtype=np.uint8)
    screen[3:6, 3:6] = target
    screen[15:18, 22:25] = other
    first, second = tmp_path / "first.png", tmp_path / "second.png"
    Image.fromarray(target).save(first)
    Image.fromarray(other).save(second)

    diagnostic = find_reference_matches(
        [first, second], 0.9, screen=Image.fromarray(screen),
        search_region={"x": 18, "y": 10, "width": 14, "height": 14},
        grayscale=True, diagnostic_min_confidence=0.8,
    )

    assert diagnostic.selected.reference_index == 1
    assert Path(diagnostic.selected.reference_image).name == "second.png"
    assert (diagnostic.selected.x, diagnostic.selected.y) == (22, 15)


def test_advanced_image_fields_round_trip_and_generate(tmp_path: Path) -> None:
    for name in ("primary.png", "alternate.png"):
        path = tmp_path / "screenshots" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(_pattern()).save(path)
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/primary.png",
        "reference_images": ["screenshots/alternate.png"],
        "confidence": 0.91,
        "timeout": 4,
        "grayscale": True,
        "search_region": {"x": -1200, "y": 20, "width": 800, "height": 600},
        "match_priority": "rightmost",
        "match_index": 1,
        "click_offset_x": 2,
        "click_offset_y": 2,
        "use_coordinate_fallback": False,
    })
    project = RpaProject(actions=[action])
    manager = ProjectManager()
    manager.save(project, tmp_path)
    loaded = manager.load(tmp_path / "project.json")
    assert loaded.actions[0].data["reference_images"] == ["screenshots/alternate.png"]
    assert loaded.actions[0].data["search_region"]["x"] == -1200

    output = generate_python(loaded, tmp_path)
    text = output.read_text(encoding="utf-8")
    ast.parse(text)
    assert "screenshots/alternate.png" in text
    assert "grayscale=True" in text
    assert "priority='rightmost'" in text


def test_validator_rejects_bad_advanced_image_settings(tmp_path: Path) -> None:
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "missing.png", "reference_images": "not-a-list",
        "confidence": 0.9, "timeout": 2, "grayscale": "yes",
        "match_priority": "random", "match_index": 0,
        "search_region": {"x": 0, "y": 0, "width": 0, "height": 10},
    })
    issues = validate_project_detailed(RpaProject(actions=[action]), tmp_path)
    reasons = "\n".join(issue.reason for issue in issues if issue.level == LEVEL_ERROR)
    assert "reference images" in reasons
    assert "grayscale" in reasons
    assert "priority" in reasons
    assert "match number" in reasons
    assert "search region" in reasons


def test_action_editor_exposes_image_debug_controls(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QFormLayout, QListWidget, QSlider
    from ui.action_editor import ActionEditor

    app = QApplication.instance() or QApplication([])
    image = tmp_path / "screenshots" / "target.png"
    image.parent.mkdir(parents=True)
    Image.fromarray(_pattern()).save(image)
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/target.png", "confidence": 0.86,
        "fallback_x": -20, "fallback_y": 45, "use_coordinate_fallback": True,
    })
    editor = ActionEditor()
    editor.set_action(action, tmp_path)
    app.processEvents()

    slider = editor.findChild(QSlider, "imageConfidenceSlider")
    references = editor.findChild(QListWidget, "imageReferenceList")
    assert slider is not None and slider.value() == 86
    assert references is not None and references.count() == 1
    assert editor.locate_button.text() == "Test Match Now"
    assert "Capture" in editor.recapture_button.text()

    main_labels = [
        editor.form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget().text()
        for row in range(editor.form.rowCount())
        if editor.form.itemAt(row, QFormLayout.ItemRole.LabelRole) is not None
    ]
    target_labels = [
        editor.image_target_form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget().text()
        for row in range(editor.image_target_form.rowCount())
        if editor.image_target_form.itemAt(row, QFormLayout.ItemRole.LabelRole) is not None
    ]
    assert "Coordinate fallback" in main_labels
    assert target_labels == ["Reference images", "Match confidence", "Search area"]

    root = editor.layout()
    form_index = next(i for i in range(root.count()) if root.itemAt(i).layout() is editor.form)
    test_index = next(
        i for i in range(root.count())
        if root.itemAt(i).layout() is not None
        and root.itemAt(i).layout().indexOf(editor.test_step_button) >= 0
    )
    assert form_index < test_index < root.indexOf(editor.preview) < root.indexOf(editor.image_target_widget)

    slider.setValue(92)
    assert action.data["confidence"] == 0.92


def test_runner_uses_ordered_references_and_records_match_evidence(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    import rpa.runner as runner_module
    from rpa.image_matcher import ImageMatch
    from rpa.runner import ReplayRunner

    clicks: list[tuple[int, int]] = []
    runner_module.pyautogui = SimpleNamespace(
        FAILSAFE=True, click=lambda x, y, **_kwargs: clicks.append((x, y)),
    )
    seen: list[list[str]] = []

    def matched(paths, *_args, **_kwargs):
        seen.append([path.name for path in paths])
        return ImageMatch(
            True, x=100, y=200, width=20, height=10, confidence=0.94,
            duration=0.12, reference_image=str(paths[1]), reference_index=1,
        ), []

    monkeypatch.setattr(runner_module, "wait_for_references", matched)
    action = RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "primary.png", "reference_images": ["alternate.png"],
        "confidence": 0.9, "timeout": 1, "click_offset_x": 5, "click_offset_y": 4,
        "use_coordinate_fallback": False,
    })
    runner = ReplayRunner(RpaProject(actions=[action]), tmp_path, lambda _message: None)
    runner.run(include_start_delay=False)

    assert seen == [["primary.png", "alternate.png"]]
    assert clicks == [(105, 204)]
    evidence = runner.step_results[0]["image_match"]
    assert evidence["reference_image"] == "alternate.png"
    assert evidence["confidence"] == 0.94
    assert evidence["search_duration"] == 0.12
