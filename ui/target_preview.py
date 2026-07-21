"""Aspect-correct Pillow-backed target image previews for Step Details."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)


PREVIEW_WIDTH = 280
PREVIEW_HEIGHT = 180


def _downscaled_size(width: int, height: int, maximum: tuple[int, int]) -> tuple[int, int]:
    """Fit inside maximum without ever enlarging the source."""
    max_width, max_height = maximum
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _resize_down(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    if image.size == size:
        return image.copy()
    return image.resize(size, Image.Resampling.LANCZOS)


def _draw_crosshair(
    image: Image.Image, point: tuple[int, int] | None, source_size: tuple[int, int],
) -> Image.Image:
    if point is None:
        return image
    source_width, source_height = source_size
    source_x, source_y = point
    if not (0 <= source_x < source_width and 0 <= source_y < source_height):
        return image
    marked = image.copy()
    x = round(source_x * marked.width / source_width)
    y = round(source_y * marked.height / source_height)
    radius = 7
    draw = ImageDraw.Draw(marked)
    # A pale outline keeps the marker readable on dark and light targets.
    draw.line((x - radius, y, x + radius, y), fill=(255, 255, 255, 230), width=3)
    draw.line((x, y - radius, x, y + radius), fill=(255, 255, 255, 230), width=3)
    draw.ellipse((x - 4, y - 4, x + 4, y + 4), outline=(255, 255, 255, 230), width=3)
    draw.line((x - radius, y, x + radius, y), fill=(220, 38, 38, 255), width=1)
    draw.line((x, y - radius, x, y + radius), fill=(220, 38, 38, 255), width=1)
    draw.ellipse((x - 4, y - 4, x + 4, y + 4), outline=(220, 38, 38, 255), width=1)
    return marked


def _pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    raw = rgba.tobytes("raw", "RGBA")
    qimage = QImage(raw, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888).copy()
    return QPixmap.fromImage(qimage)


class LargeTargetPreviewDialog(QDialog):
    """Larger, scroll-safe view opened by double-clicking the target preview."""

    def __init__(
        self, source: Image.Image, filename: str,
        click_point: tuple[int, int] | None = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Target Preview — {filename}")
        self._source_image = source.copy()
        self._rendered_image: Image.Image | None = None
        self._pixmap: QPixmap | None = None

        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        maximum = (
            max(320, int(available.width() * 0.8)) if available else 1000,
            max(240, int(available.height() * 0.72)) if available else 700,
        )
        size = _downscaled_size(*self._source_image.size, maximum)
        resized = _resize_down(self._source_image.convert("RGBA"), size)
        self._rendered_image = _draw_crosshair(resized, click_point, self._source_image.size)
        self._pixmap = _pixmap(self._rendered_image)

        image_label = QLabel()
        image_label.setAlignment(Qt.AlignCenter)
        image_label.setPixmap(self._pixmap)
        image_label.setStyleSheet("background: #f3f4f6;")
        scroll = QScrollArea()
        scroll.setWidget(image_label)
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setStyleSheet("QScrollArea { background: #f3f4f6; border: 1px solid #d1d5db; }")
        details = QLabel(f"{filename} · {self._source_image.width} × {self._source_image.height} px")
        details.setAlignment(Qt.AlignCenter)
        details.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(details)
        self.resize(min(maximum[0] + 40, 1100), min(maximum[1] + 80, 820))


class _PreviewCanvasLabel(QLabel):
    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        event.accept()


class TargetPreviewWidget(QWidget):
    """Fixed neutral preview canvas that never stretches or enlarges a capture."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._path: Path | None = None
        self._source_image: Image.Image | None = None
        self._rendered_image: Image.Image | None = None
        self._pixmap: QPixmap | None = None
        self._click_point: tuple[int, int] | None = None
        self.displayed_image_size = (0, 0)
        self.image_origin = (0, 0)

        self.image_label = _PreviewCanvasLabel("No target image")
        self.image_label.setObjectName("targetPreviewCanvas")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setFixedSize(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.image_label.setStyleSheet(
            "background: #f3f4f6; border: 1px solid #d1d5db; "
            "border-radius: 4px; color: #6b7280;"
        )
        self.image_label.setToolTip("Double-click to open a larger preview")
        self.image_label.double_clicked.connect(self._open_larger_preview)
        self.details_label = QLabel()
        self.details_label.setObjectName("targetPreviewDetails")
        self.details_label.setAlignment(Qt.AlignCenter)
        self.details_label.setStyleSheet("color: #64748b;")
        self.details_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.image_label, 0, Qt.AlignHCenter)
        layout.addWidget(self.details_label)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def clear(self) -> None:
        self._path = None
        self._source_image = None
        self._rendered_image = None
        self._pixmap = None
        self._click_point = None
        self.displayed_image_size = (0, 0)
        self.image_origin = (0, 0)
        self.image_label.clear()
        self.image_label.setText("No target image")
        self.details_label.clear()

    def setText(self, text: str) -> None:  # Qt-compatible convenience for ActionEditor
        self.clear()
        self.image_label.setText(text)

    def load_image(self, path: Path, click_point: tuple[int, int] | None = None) -> bool:
        self.clear()
        self._path = Path(path)
        self._click_point = click_point
        try:
            with Image.open(self._path) as opened:
                self._source_image = opened.convert("RGBA").copy()
        except (OSError, ValueError):
            self.image_label.setText("Target image could not be loaded.")
            self.details_label.setText(self._path.name)
            self.details_label.setToolTip(str(self._path))
            return False

        size = _downscaled_size(
            self._source_image.width, self._source_image.height,
            (PREVIEW_WIDTH, PREVIEW_HEIGHT),
        )
        resized = _resize_down(self._source_image, size)
        marked = _draw_crosshair(resized, click_point, self._source_image.size)
        canvas = Image.new("RGBA", (PREVIEW_WIDTH, PREVIEW_HEIGHT), (243, 244, 246, 255))
        left = (PREVIEW_WIDTH - marked.width) // 2
        top = (PREVIEW_HEIGHT - marked.height) // 2
        canvas.alpha_composite(marked, (left, top))
        self._rendered_image = canvas
        self._pixmap = _pixmap(canvas)
        self.image_label.setText("")
        self.image_label.setPixmap(self._pixmap)
        self.displayed_image_size = size
        self.image_origin = (left, top)
        self.details_label.setText(
            f"{self._path.name} · {self._source_image.width} × {self._source_image.height} px"
        )
        self.details_label.setToolTip(str(self._path))
        return True

    def _open_larger_preview(self) -> None:
        if self._source_image is not None and self._path is not None:
            LargeTargetPreviewDialog(
                self._source_image, self._path.name, self._click_point, self,
            ).exec()
