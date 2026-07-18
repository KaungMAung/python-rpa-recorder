from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


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
