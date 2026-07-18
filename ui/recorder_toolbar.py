from __future__ import annotations

import time

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class FloatingRecorderToolbar(QWidget):
    pause_requested = Signal()
    resume_requested = Signal()
    stop_requested = Signal()
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Recording")
        self.started = time.monotonic()
        self.paused = False
        self.status = QLabel("REC Recording")
        self.elapsed = QLabel("00:00")
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        self.cancel_btn = QPushButton("Cancel")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.status)
        layout.addWidget(self.elapsed)
        layout.addWidget(self.pause_btn)
        layout.addWidget(self.stop_btn)
        layout.addWidget(self.cancel_btn)

        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self.stop_requested)
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)
        self._apply_colors()

    def set_preparing(self, seconds: int) -> None:
        """Show preparation without enabling capture controls yet."""
        self.status.setText(f"Starting in {seconds}…")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.setStyleSheet(
            "QWidget { background: #eff6ff; border: 1px solid #bfdbfe; } "
            "QLabel { color: #1d4ed8; font-weight: 600; } "
            "QPushButton { padding: 4px 10px; }"
        )

    def set_recording(self) -> None:
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.set_paused(False)

    def _tick(self) -> None:
        seconds = int(time.monotonic() - self.started)
        self.elapsed.setText(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self.status.setText("Paused" if paused else "REC Recording")
        self.pause_btn.setText("Resume" if paused else "Pause")
        self._apply_colors()

    def _apply_colors(self) -> None:
        if self.paused:
            self.setStyleSheet(
                "QWidget { background: #fff7df; } "
                "QLabel { color: #7c2d12; font-weight: 600; } "
                "QPushButton { padding: 4px 10px; }"
            )
            self.pause_btn.setStyleSheet("background: #16a34a; color: white; font-weight: 600;")
        else:
            self.setStyleSheet(
                "QWidget { background: #fff1f1; } "
                "QLabel { color: #7f1d1d; font-weight: 600; } "
                "QPushButton { padding: 4px 10px; }"
            )
            self.pause_btn.setStyleSheet("background: #f59e0b; color: #111827; font-weight: 600;")
        self.stop_btn.setStyleSheet("background: #dc2626; color: white; font-weight: 600;")
        self.cancel_btn.setStyleSheet("background: #f3f4f6; color: #111827;")

    def _toggle_pause(self) -> None:
        if self.paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()


class FloatingExecutionToolbar(QWidget):
    stop_requested = Signal()
    position_changed = Signal(QPoint)
    resume_requested = Signal()
    step_over_requested = Signal()
    skip_requested = Signal()
    restart_requested = Signal()
    variables_requested = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Automation Running")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self.status = QLabel("Running automation")
        self.status.setStyleSheet("font-weight: 600;")
        self.next_step = QLabel("")
        self.next_step.setStyleSheet("color: #475569;")
        self.debug_controls = QWidget()
        debug_layout = QHBoxLayout(self.debug_controls)
        debug_layout.setContentsMargins(0, 2, 0, 0)
        for label, signal in (
            ("Resume", self.resume_requested),
            ("Step Over", self.step_over_requested),
            ("Skip Step", self.skip_requested),
            ("Restart Selected", self.restart_requested),
            ("Variables", self.variables_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(signal)
            debug_layout.addWidget(button)
        self.stop = QPushButton("Stop Run")
        self.stop.setStyleSheet("background: #ea580c; color: white; font-weight: 600; padding: 4px 12px;")
        self.stop.clicked.connect(self.stop_requested)
        header = QHBoxLayout()
        header.addWidget(self.status, 1)
        header.addWidget(self.stop)
        layout.addLayout(header)
        layout.addWidget(self.next_step)
        layout.addWidget(self.debug_controls)
        self.debug_controls.hide()
        self.setStyleSheet("QWidget { background: #eff6ff; border: 1px solid #bfdbfe; }")

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.adjustSize()

    def set_debug_paused(self, current: str, next_step: str = "") -> None:
        self.status.setText(current)
        self.next_step.setText(next_step)
        self.next_step.setVisible(bool(next_step))
        self.debug_controls.show()
        self.setStyleSheet(
            "QWidget { background: #f5f3ff; border: 1px solid #c4b5fd; } "
            "QPushButton { padding: 4px 8px; }"
        )
        self.stop.setStyleSheet("background: #dc2626; color: white; font-weight: 600; padding: 4px 12px;")
        self.adjustSize()

    def set_debug_running(self, text: str) -> None:
        self.status.setText(text)
        self.next_step.clear()
        self.next_step.hide()
        self.debug_controls.hide()
        self.setStyleSheet("QWidget { background: #eff6ff; border: 1px solid #bfdbfe; }")
        self.stop.setStyleSheet("background: #ea580c; color: white; font-weight: 600; padding: 4px 12px;")
        self.adjustSize()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.position_changed.emit(self.pos())
