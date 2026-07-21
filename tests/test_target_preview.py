from __future__ import annotations

import gc
import os
from pathlib import Path

import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rpa.models import ActionType, RpaAction
from ui.action_editor import ActionEditor
from ui.target_preview import LargeTargetPreviewDialog, TargetPreviewWidget


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize(
    ("source_size", "display_size", "origin"),
    [
        ((560, 200), (280, 100), (0, 40)),       # landscape
        ((100, 400), (45, 180), (117, 0)),       # portrait
        ((40, 30), (40, 30), (120, 75)),         # never upscale
    ],
)
def test_preview_preserves_aspect_ratio_centres_and_never_upscales(
    tmp_path: Path, source_size: tuple[int, int],
    display_size: tuple[int, int], origin: tuple[int, int],
) -> None:
    app()
    path = tmp_path / f"target_{source_size[0]}x{source_size[1]}.png"
    Image.new("RGB", source_size, "#4b83c3").save(path)
    preview = TargetPreviewWidget()

    assert preview.load_image(path)
    assert preview.displayed_image_size == display_size
    assert preview.image_origin == origin
    assert preview.image_label.pixmap().size().toTuple() == (280, 180)
    assert preview.details_label.text() == (
        f"{path.name} · {source_size[0]} × {source_size[1]} px"
    )
    # Strong Pillow and QPixmap references keep the Qt preview valid after GC.
    gc.collect()
    assert preview._source_image is not None
    assert preview._rendered_image is not None
    assert preview._pixmap is not None and not preview._pixmap.isNull()
    preview.close()


def test_downscaling_uses_lanczos_and_draws_recorded_click_crosshair(tmp_path: Path, monkeypatch) -> None:
    app()
    path = tmp_path / "click_0001.png"
    Image.new("RGB", (560, 320), "white").save(path)
    calls = []
    original_resize = Image.Image.resize

    def tracked_resize(self, size, resample=None, *args, **kwargs):
        calls.append((size, resample))
        return original_resize(self, size, resample, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", tracked_resize)
    preview = TargetPreviewWidget()
    assert preview.load_image(path, click_point=(220, 160))

    assert calls and calls[0][1] == Image.Resampling.LANCZOS
    left, top = preview.image_origin
    shown_width, shown_height = preview.displayed_image_size
    marker_x = left + round(220 * shown_width / 560)
    marker_y = top + round(160 * shown_height / 320)
    red, green, blue, _alpha = preview._rendered_image.getpixel((marker_x, marker_y))
    assert red > 180 and green < 90 and blue < 90
    preview.close()


def test_double_click_opens_larger_preview(tmp_path: Path, monkeypatch) -> None:
    application = app()
    path = tmp_path / "target.png"
    Image.new("RGB", (220, 160), "green").save(path)
    preview = TargetPreviewWidget()
    preview.load_image(path, (110, 80))
    preview.show()
    application.processEvents()
    opened = []
    monkeypatch.setattr(LargeTargetPreviewDialog, "exec", lambda self: opened.append(self.windowTitle()))

    QTest.mouseDClick(preview.image_label, Qt.LeftButton)

    assert opened == ["Target Preview — target.png"]
    preview.close()


@pytest.mark.parametrize("size", [(480, 180), (140, 420)])
def test_step_details_loads_landscape_and_portrait_target_metadata(
    tmp_path: Path, size: tuple[int, int],
) -> None:
    app()
    image = tmp_path / "screenshots" / "captured.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "#718096").save(image)
    editor = ActionEditor()
    editor.set_action(RpaAction(ActionType.CLICK_IMAGE.value, {
        "image": "screenshots/captured.png",
        "click_offset_x": size[0] // 2,
        "click_offset_y": size[1] // 2,
    }), tmp_path)

    assert editor.preview.details_label.text() == f"captured.png · {size[0]} × {size[1]} px"
    assert editor.preview.displayed_image_size[0] <= 280
    assert editor.preview.displayed_image_size[1] <= 180
    editor.close()
