"""Crosshair overlay that returns one global point for native window inspection."""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QShowEvent
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


class WindowPickOverlay(QWidget):
    picked = Signal(int, int)
    canceled = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self._finished = False
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        geometry = QRect()
        for screen in QApplication.screens():
            geometry = geometry.united(screen.geometry())
        if geometry.isNull() and QApplication.primaryScreen():
            geometry = QApplication.primaryScreen().geometry()
        self.setGeometry(geometry)
        label = QLabel("Click the window to target  •  Esc or right-click to cancel")
        label.setStyleSheet(
            "background: rgba(15, 23, 42, 225); color: white; padding: 11px 18px; "
            "font-weight: 600; border: 1px solid #64748b;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(label, 0, Qt.AlignHCenter)
        layout.addStretch(1)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            point = event.position().toPoint() + self.geometry().topLeft()
            self._finished = True
            self.close()
            self.picked.emit(point.x(), point.y())
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self._cancel()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42, 45))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.raise_(); self.activateWindow(); self.setFocus(Qt.ActiveWindowFocusReason)

    def closeEvent(self, event) -> None:
        if not self._finished:
            self._finished = True
            self.canceled.emit()
        event.accept()

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.close()
        self.canceled.emit()
