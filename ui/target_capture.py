from __future__ import annotations

from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class TargetCaptureOverlay(QWidget):
    confirmed = Signal(int, int, int, int)
    canceled = Signal()

    def __init__(self, captured_image: Image.Image, crop_width: int, crop_height: int, parent=None) -> None:
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.captured_image = captured_image
        self._finished = False
        self._target: QPoint | None = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

        geometry = QRect()
        for screen in QApplication.screens():
            geometry = geometry.united(screen.geometry())
        if geometry.isNull() and QApplication.primaryScreen():
            geometry = QApplication.primaryScreen().geometry()
        self.setGeometry(geometry)

        self.instructions = QLabel("Click the target. Adjust the crop size, then confirm.")
        self.instructions.setStyleSheet(
            "background: rgba(15, 23, 42, 220); color: white; padding: 10px 16px; "
            "font-weight: 600; border: 1px solid #475569;"
        )
        self.instructions.setAlignment(Qt.AlignCenter)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(40, max(40, captured_image.width))
        self.width_spin.setValue(max(40, min(crop_width, captured_image.width)))
        self.width_spin.setSuffix(" px")
        self.height_spin = QSpinBox()
        self.height_spin.setRange(40, max(40, captured_image.height))
        self.height_spin.setValue(max(40, min(crop_height, captured_image.height)))
        self.height_spin.setSuffix(" px")
        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setEnabled(False)
        self.confirm_button.setStyleSheet("background: #16a34a; color: white; font-weight: 600; padding: 6px 14px;")
        self.cancel_button = QPushButton("Cancel")

        controls = QWidget()
        controls.setStyleSheet("QWidget { background: rgba(248, 250, 252, 235); } QLabel { background: transparent; }")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(12, 8, 12, 8)
        controls_layout.addWidget(QLabel("Width"))
        controls_layout.addWidget(self.width_spin)
        controls_layout.addWidget(QLabel("Height"))
        controls_layout.addWidget(self.height_spin)
        controls_layout.addWidget(self.confirm_button)
        controls_layout.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(self.instructions, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        layout.addWidget(controls, 0, Qt.AlignHCenter)

        self.width_spin.valueChanged.connect(self.update)
        self.height_spin.valueChanged.connect(self.update)
        self.confirm_button.clicked.connect(self._confirm)
        self.cancel_button.clicked.connect(self._cancel)

    def selected_target(self) -> tuple[int, int] | None:
        if self._target is None:
            return None
        return self._target.x(), self._target.y()

    def selection_rect(self) -> QRect:
        if self._target is None:
            return QRect()
        local = self._target - self.geometry().topLeft()
        width = self.width_spin.value()
        height = self.height_spin.value()
        return QRect(local.x() - width // 2, local.y() - height // 2, width, height)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            local = event.position().toPoint()
            self._target = local + self.geometry().topLeft()
            self.confirm_button.setEnabled(True)
            self.instructions.setText(
                f"Target selected at ({self._target.x()}, {self._target.y()}). Adjust the crop or confirm."
            )
            self.update()
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
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self._target is not None:
            self._confirm()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(15, 23, 42, 70))
        selection = self.selection_rect()
        if not selection.isNull():
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(selection, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#ef4444"), 2))
            painter.drawRect(selection.adjusted(0, 0, -1, -1))
            center = selection.center()
            painter.drawLine(center.x() - 10, center.y(), center.x() + 10, center.y())
            painter.drawLine(center.x(), center.y() - 10, center.x(), center.y() + 10)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.ActiveWindowFocusReason)

    def closeEvent(self, event) -> None:
        if not self._finished:
            self._finished = True
            self.canceled.emit()
        event.accept()

    def _confirm(self) -> None:
        if self._target is None or self._finished:
            return
        self._finished = True
        target_x, target_y = self._target.x(), self._target.y()
        width, height = self.width_spin.value(), self.height_spin.value()
        # Close this always-on-top overlay before notifying listeners, otherwise any
        # dialog they show (e.g. a confirmation message box) renders underneath the
        # still-visible overlay and the app appears completely frozen.
        self.close()
        self.confirmed.emit(target_x, target_y, width, height)

    def _cancel(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.close()
        self.canceled.emit()
        self.close()
