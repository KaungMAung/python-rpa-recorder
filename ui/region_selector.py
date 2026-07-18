"""Drag-to-select overlay for limiting image-search regions."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


class RegionSelectionOverlay(QWidget):
    selected = Signal(int, int, int, int)
    canceled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._start: QPoint | None = None
        self._current: QPoint | None = None
        self._finished = False
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        geometry = QRect()
        for screen in QApplication.screens():
            geometry = geometry.united(screen.geometry())
        self.setGeometry(geometry)
        label = QLabel("Drag around the area to search  |  Esc or right-click to cancel")
        label.setStyleSheet(
            "background: rgba(15,23,42,225); color:white; padding:10px 16px; font-weight:600;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(label, 0, Qt.AlignHCenter)
        layout.addStretch(1)

    def selection_rect(self) -> QRect:
        if self._start is None or self._current is None:
            return QRect()
        return QRect(self._start, self._current).normalized()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.RightButton:
            self._cancel()
            return
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._current = self._start
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._start is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or self._start is None:
            return
        self._current = event.position().toPoint()
        rect = self.selection_rect()
        if rect.width() < 5 or rect.height() < 5:
            self._cancel()
            return
        global_rect = rect.translated(self.geometry().topLeft())
        self._finished = True
        self.close()
        self.selected.emit(global_rect.x(), global_rect.y(), global_rect.width(), global_rect.height())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42, 65))
        rect = self.selection_rect()
        if not rect.isNull():
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#38bdf8"), 3))
            painter.drawRect(rect)

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.close()
        self.canceled.emit()
