"""Plain-language editor for a reusable window target."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)
import shiboken6


class WindowTargetEditor(QWidget):
    changed = Signal()
    pick_requested = Signal()

    def __init__(self, data: dict | None = None, allow_selected: bool = True, parent=None) -> None:
        super().__init__(parent)
        self._disposed = False
        data = data or {}
        source = data.get("window") if isinstance(data.get("window"), dict) else data
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.use_selected = QCheckBox("Use the window selected by an earlier Select / Target Window step")
        self.use_selected.setChecked(bool(data.get("use_selected_window", False)))
        self.use_selected.setVisible(allow_selected)
        if allow_selected:
            layout.addWidget(self.use_selected)

        pick_row = QHBoxLayout()
        self.pick_button = QPushButton("Pick Window")
        self.pick_button.setToolTip("Hide the recorder, then click a visible window. Esc or right-click cancels.")
        self.captured_label = QLabel("Click Pick Window, or enter matching details below.")
        self.captured_label.setWordWrap(True)
        self.captured_label.setStyleSheet("color: #475569;")
        pick_row.addWidget(self.pick_button)
        pick_row.addWidget(self.captured_label, 1)
        layout.addLayout(pick_row)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(7)
        self.process_name = QLineEdit(str(source.get("process_name", "")))
        self.process_name.setPlaceholderText("For example: notepad.exe")
        self.process_name.setToolTip("Process filename only. Leave blank when title/class matching is sufficient.")
        self.window_title = QLineEdit(str(source.get("window_title", "")))
        self.window_title.setPlaceholderText("Visible window title")
        self.title_match = QComboBox()
        for label, value in (("Contains", "contains"), ("Exact", "exact"), ("Regular Expression", "regex")):
            self.title_match.addItem(label, value)
        self.title_match.setCurrentIndex(max(0, self.title_match.findData(str(source.get("title_match", "contains")))))
        title_row = QHBoxLayout(); title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(self.title_match); title_row.addWidget(self.window_title, 1)
        title_wrap = QWidget(); title_wrap.setLayout(title_row)
        self.class_name = QLineEdit(str(source.get("class_name", "")))
        self.class_name.setPlaceholderText("Optional native window class")
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(0, 3600); self.timeout.setDecimals(2)
        self.timeout.setValue(float(source.get("timeout", 10.0) or 0)); self.timeout.setSuffix(" s")
        self.timeout.setToolTip("How long to wait for the target window to appear.")
        self.retry = QDoubleSpinBox(); self.retry.setRange(0.05, 60); self.retry.setDecimals(2)
        self.retry.setValue(float(source.get("retry_interval", 0.25) or 0.25)); self.retry.setSuffix(" s")
        self.multiple = QComboBox()
        for label, value in (("Show an error", "error"), ("Use top-most match", "first"), ("Use active match", "active")):
            self.multiple.addItem(label, value)
        self.multiple.setCurrentIndex(max(0, self.multiple.findData(str(source.get("multiple_match", "error")))))
        form.addRow("Process", self.process_name)
        form.addRow("Window title", title_wrap)
        form.addRow("Class name", self.class_name)
        form.addRow("Wait up to", self.timeout)
        form.addRow("Check every", self.retry)
        form.addRow("If several match", self.multiple)
        layout.addLayout(form)

        self.pick_button.clicked.connect(self.pick_requested)
        self.use_selected.toggled.connect(self._selected_changed)
        for line in (self.process_name, self.window_title, self.class_name):
            line.textChanged.connect(self.changed)
        for combo in (self.title_match, self.multiple):
            combo.currentIndexChanged.connect(self.changed)
        self.timeout.valueChanged.connect(self.changed)
        self.retry.valueChanged.connect(self.changed)
        self._selected_changed()

    def _selected_changed(self, *_args) -> None:
        if self._disposed or not shiboken6.isValid(self.use_selected):
            return
        enabled = not self.use_selected.isVisible() or not self.use_selected.isChecked()
        for widget in (
            self.pick_button, self.process_name, self.window_title, self.title_match, self.class_name,
        ):
            widget.setEnabled(enabled)
        self.changed.emit()

    def set_target(self, target: dict, description: str = "") -> None:
        if self._disposed or not shiboken6.isValid(self) or not shiboken6.isValid(self.use_selected):
            return
        self.use_selected.setChecked(False)
        self.process_name.setText(str(target.get("process_name", "")))
        self.window_title.setText(str(target.get("window_title", "")))
        self.title_match.setCurrentIndex(max(0, self.title_match.findData(str(target.get("title_match", "exact")))))
        self.class_name.setText(str(target.get("class_name", "")))
        self.captured_label.setText(description or "Window details captured.")
        self.changed.emit()

    def data(self) -> dict:
        if self._disposed or not shiboken6.isValid(self) or not shiboken6.isValid(self.use_selected):
            return {"use_selected_window": False, "window": {}}
        return {
            "use_selected_window": self.use_selected.isVisible() and self.use_selected.isChecked(),
            "window": {
                "process_name": self.process_name.text().strip(),
                "window_title": self.window_title.text().strip(),
                "title_match": self.title_match.currentData(),
                "class_name": self.class_name.text().strip(),
                "timeout": self.timeout.value(),
                "retry_interval": self.retry.value(),
                "multiple_match": self.multiple.currentData(),
            },
        }

    def dispose(self) -> None:
        """Disconnect callbacks before the dynamic Add Step form deletes this editor."""
        if self._disposed:
            return
        self._disposed = True
        if shiboken6.isValid(self):
            self.pick_button.clicked.disconnect(self.pick_requested)
            self.use_selected.toggled.disconnect(self._selected_changed)
            for line in (self.process_name, self.window_title, self.class_name):
                line.textChanged.disconnect(self.changed)
            for combo in (self.title_match, self.multiple):
                combo.currentIndexChanged.disconnect(self.changed)
            self.timeout.valueChanged.disconnect(self.changed)
            self.retry.valueChanged.disconnect(self.changed)
