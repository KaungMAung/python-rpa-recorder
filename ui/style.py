"""Central application-wide font, palette, and DPI configuration.

Applying these settings once, before any window is created, keeps the
application's appearance consistent across Windows laptops instead of
depending on whatever font/DPI defaults happen to be configured in the
OS. Widget-level stylesheets should only override *weight* (for
headings/buttons) and colour, not the font family or pixel-based sizes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

FONT_FAMILY = "Segoe UI"
FONT_POINT_SIZE = 10

NORMAL_WEIGHT = QFont.Weight.Normal
HEADING_WEIGHT = QFont.Weight.DemiBold

COLOR_TEXT_NORMAL = "#1f2937"
COLOR_TEXT_DISABLED = "#9ca3af"
COLOR_TEXT_PLACEHOLDER = "#9ca3af"
COLOR_TEXT_SELECTED = "#ffffff"
COLOR_SELECTION_BACKGROUND = "#2563eb"

GLOBAL_STYLESHEET = f"""
QWidget {{
    font-family: "{FONT_FAMILY}";
    font-size: {FONT_POINT_SIZE}pt;
}}
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    selection-background-color: {COLOR_SELECTION_BACKGROUND};
    selection-color: {COLOR_TEXT_SELECTED};
}}
"""


def default_font() -> QFont:
    """The application-wide default font used for normal text."""
    font = QFont(FONT_FAMILY)
    font.setPointSize(FONT_POINT_SIZE)
    font.setWeight(NORMAL_WEIGHT)
    return font


def heading_font(point_size: int = FONT_POINT_SIZE) -> QFont:
    """Font to use for headings, buttons, and other important labels."""
    font = QFont(FONT_FAMILY)
    font.setPointSize(point_size)
    font.setWeight(HEADING_WEIGHT)
    return font


def _build_palette() -> QPalette:
    palette = QPalette()
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Active, role, QColor(COLOR_TEXT_NORMAL))
        palette.setColor(QPalette.ColorGroup.Inactive, role, QColor(COLOR_TEXT_NORMAL))
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(COLOR_TEXT_DISABLED))
    palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.PlaceholderText, QColor(COLOR_TEXT_PLACEHOLDER))
    palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.PlaceholderText, QColor(COLOR_TEXT_PLACEHOLDER))
    palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.Highlight, QColor(COLOR_SELECTION_BACKGROUND))
    palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, QColor(COLOR_SELECTION_BACKGROUND))
    palette.setColor(QPalette.ColorGroup.Active, QPalette.ColorRole.HighlightedText, QColor(COLOR_TEXT_SELECTED))
    palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.HighlightedText, QColor(COLOR_TEXT_SELECTED))
    return palette


def enable_high_dpi() -> None:
    """Enable Windows high-DPI scaling. Must run before QApplication is created."""
    for attr_name in ("AA_EnableHighDpiScaling", "AA_UseHighDpiPixmaps"):
        attr = getattr(Qt, attr_name, None)
        if attr is not None:
            QApplication.setAttribute(attr, True)


def apply_global_style(app: QApplication) -> None:
    """Apply the shared font, palette, and stylesheet to the whole application."""
    app.setFont(default_font())
    app.setPalette(_build_palette())
    app.setStyleSheet(GLOBAL_STYLESHEET)
