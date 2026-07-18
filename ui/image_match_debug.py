"""Image-match diagnostic results and an on-screen rectangle overlay."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QDialog, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from rpa.image_matcher import ImageDiagnostic, ImageMatch


class MatchHighlightOverlay(QWidget):
    def __init__(self, matches: list[ImageMatch], selected: ImageMatch | None = None, parent=None) -> None:
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
        super().__init__(parent, flags)
        self.matches = matches
        self.selected = selected
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        geometry = QRect()
        for screen in QApplication.screens():
            geometry = geometry.united(screen.geometry())
        self.setGeometry(geometry)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        origin = self.geometry().topLeft()
        for index, match in enumerate(self.matches, start=1):
            chosen = match is self.selected
            color = QColor("#16a34a" if chosen else "#f59e0b")
            painter.setPen(QPen(color, 4 if chosen else 2))
            rect = QRect(match.x - origin.x(), match.y - origin.y(), match.width, match.height)
            painter.drawRect(rect)
            painter.fillRect(QRect(rect.left(), max(0, rect.top() - 22), 150, 22), QColor(15, 23, 42, 220))
            painter.setPen(Qt.white)
            painter.drawText(rect.left() + 4, max(15, rect.top() - 6), f"#{index}  {match.confidence:.1%}")


class MatchResultsDialog(QDialog):
    match_chosen = Signal(object)
    highlight_requested = Signal(object)

    def __init__(self, diagnostic: ImageDiagnostic, click_offset: tuple[int, int], parent=None) -> None:
        super().__init__(parent)
        self.diagnostic = diagnostic
        self.click_offset = click_offset
        self.setWindowTitle("Image Match Results")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        title = QLabel(f"{len(diagnostic.matches)} detected match(es) | search time {diagnostic.duration:.2f}s")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(title)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Reference", "Confidence", "Top-left", "Click location", "Rank"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        for row, match in enumerate(diagnostic.matches):
            self.table.insertRow(row)
            click_x, click_y = match.x + click_offset[0], match.y + click_offset[1]
            values = (
                Path(match.reference_image).name, f"{match.confidence:.1%}",
                f"({match.x}, {match.y})", f"({click_x}, {click_y})", str(match.match_index),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, match)
                self.table.setItem(row, column, item)
        buttons = QHBoxLayout()
        self.highlight_button = QPushButton("Highlight on Screen")
        self.use_button = QPushButton("Use Selected Match")
        close = QPushButton("Close")
        buttons.addWidget(self.highlight_button)
        buttons.addWidget(self.use_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._choose())
        self.highlight_button.clicked.connect(self._highlight)
        self.use_button.clicked.connect(self._choose)
        close.clicked.connect(self.accept)
        if diagnostic.matches:
            selected_row = next(
                (row for row, item in enumerate(diagnostic.matches) if item is diagnostic.selected), 0
            )
            self.table.selectRow(selected_row)
        else:
            self.detail.setText("No candidates were detected. Review the warnings and target settings.")
            self.highlight_button.setEnabled(False)
            self.use_button.setEnabled(False)
        if diagnostic.warnings:
            self.detail.setText("\n".join(f"- {warning}" for warning in diagnostic.warnings))

    def selected_match(self) -> ImageMatch | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _selection_changed(self) -> None:
        match = self.selected_match()
        if match:
            click_x, click_y = match.x + self.click_offset[0], match.y + self.click_offset[1]
            self.detail.setText(
                f"Selected {Path(match.reference_image).name} at {match.confidence:.1%}. "
                f"This step would click ({click_x}, {click_y})."
            )

    def _highlight(self) -> None:
        match = self.selected_match()
        if match:
            self.highlight_requested.emit(match)

    def _choose(self) -> None:
        match = self.selected_match()
        if match:
            self.match_chosen.emit(match)
            self.accept()
