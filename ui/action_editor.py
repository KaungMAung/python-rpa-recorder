from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from rpa.models import ActionType, RpaAction
from rpa.control_flow import CONTROL_TYPES
from ui.condition_editor import ConditionEditor


class ActionEditor(QWidget):
    action_changed = Signal()
    close_requested = Signal()
    test_step_requested = Signal(RpaAction)
    test_locator_requested = Signal(RpaAction)
    recapture_requested = Signal(RpaAction)
    advanced_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.action: RpaAction | None = None
        self.project_dir: Path | None = None
        self.available_variables: list[str] = []
        self._loading = False

        self.title = QLabel("Step Details")
        self.title.setStyleSheet("font-size: 15px; font-weight: 700; color: #1f2937;")
        self.placeholder = QLabel("Select a step to review or edit it. The step list remains available on the left.")
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet("color: #64748b; padding: 14px 0;")
        self.close_button = QPushButton("Deselect")
        self.close_button.clicked.connect(self.close_requested)

        self.form = QFormLayout()
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(9)

        self.test_step_button = QPushButton("Test This Step")
        self.test_step_button.setStyleSheet("font-weight: 600; padding: 6px 10px;")
        self.test_step_button.clicked.connect(self._test_step)
        self.locate_button = QPushButton("Locate Target")
        self.locate_button.clicked.connect(self._test_locator)
        self.recapture_button = QPushButton("Recapture Target")
        self.recapture_button.clicked.connect(self._recapture_target)

        self.preview_heading = QLabel("Target Preview")
        self.preview_heading.setStyleSheet("font-weight: 600; margin-top: 6px;")
        self.preview = QLabel("No target image")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(260, 140)
        self.preview.setMaximumHeight(220)
        self.preview.setStyleSheet("background: #f8fafc; border: 1px solid #d8dee8; color: #64748b; padding: 4px;")

        self.advanced_button = QPushButton("Advanced Settings")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setStyleSheet("text-align: left; font-weight: 600; padding: 6px;")
        self.advanced_button.toggled.connect(self._toggle_advanced)
        self.advanced_widget = QWidget()
        self.advanced_form = QFormLayout(self.advanced_widget)
        self.advanced_form.setHorizontalSpacing(14)
        self.advanced_form.setVerticalSpacing(9)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 16)
        layout.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        layout.addWidget(self.placeholder)
        layout.addLayout(self.form)
        buttons = QHBoxLayout()
        buttons.addWidget(self.test_step_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        target_buttons = QHBoxLayout()
        target_buttons.addWidget(self.locate_button)
        target_buttons.addWidget(self.recapture_button)
        layout.addLayout(target_buttons)
        layout.addWidget(self.preview_heading)
        layout.addWidget(self.preview)
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.advanced_widget)
        layout.addStretch(1)
        self._set_content_visible(False)

    def set_action(self, action: RpaAction | None, project_dir: Path | None) -> None:
        self.action = action
        self.project_dir = project_dir
        self._rebuild()

    def set_available_variables(self, names) -> None:
        self.available_variables = sorted({str(name) for name in names if str(name)})

    def set_advanced_expanded(self, expanded: bool) -> None:
        self.advanced_button.setChecked(bool(expanded))

    def _set_content_visible(self, visible: bool) -> None:
        for widget in (
            self.close_button,
            self.test_step_button,
            self.locate_button,
            self.recapture_button,
            self.preview_heading,
            self.preview,
            self.advanced_button,
        ):
            widget.setVisible(visible)
        self.advanced_widget.setVisible(visible and self.advanced_button.isChecked())
        self.placeholder.setVisible(not visible)

    def _clear_layout(self, layout: QFormLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget:
                widget.blockSignals(True)
                widget.deleteLater()
            elif child_layout:
                while child_layout.count():
                    child = child_layout.takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()

    def _rebuild(self) -> None:
        self._loading = True
        self._clear_layout(self.form)
        self._clear_layout(self.advanced_form)
        self.preview.clear()
        self.preview.setText("No target image")
        if not self.action:
            self.title.setText("Step Details")
            self._set_content_visible(False)
            self._loading = False
            return

        action = self.action
        self._set_content_visible(True)
        self.locate_button.setVisible(action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value))
        self.recapture_button.setVisible(action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value))
        self.preview_heading.setVisible(action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value))
        self.preview.setVisible(action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value))
        self.title.setText(f"Step Details - {action.friendly_name()}")

        name = QLineEdit(action.name)
        name.setPlaceholderText(self._suggested_name(action))
        name.editingFinished.connect(lambda: self._set_name(name.text()))
        self.form.addRow("Step name", name)
        self.form.addRow("Action", QLabel(action.friendly_name()))
        self.form.addRow("Status", QLabel("Disabled" if not action.enabled else str(action.status).title()))
        self.form.addRow("Enabled", self._check(action.enabled, lambda value: self._set("enabled", value)))
        is_click_image = action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value)
        is_control = action.action in CONTROL_TYPES
        if is_click_image:
            note = QLabel("This step searches continuously for its target (see Search timeout below) instead of waiting a fixed time.")
            note.setWordWrap(True)
            note.setStyleSheet("color: #64748b;")
            self.form.addRow(note)
        elif not is_control:
            self.form.addRow("Wait before", self._double(action.delay_before, lambda value: self._set("delay_before", value), 0, 9999))

        data = action.data
        if action.action in {
            ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value,
            ActionType.IF_WINDOW_EXISTS.value, ActionType.IF_PATH_EXISTS.value,
            ActionType.IF_VARIABLE.value,
        }:
            fixed = {
                ActionType.IF_IMAGE_EXISTS.value: "image_exists",
                ActionType.IF_IMAGE_NOT_EXISTS.value: "image_not_exists",
                ActionType.IF_WINDOW_EXISTS.value: "window_exists",
                ActionType.IF_PATH_EXISTS.value: "path_exists",
                ActionType.IF_VARIABLE.value: "variable",
            }[action.action]
            condition = ConditionEditor(data, fixed_type=fixed, variables=self.available_variables)
            condition.changed.connect(lambda: self._set_condition_data(condition.data()))
            self.form.addRow(condition)
        elif action.action == ActionType.REPEAT_COUNT.value:
            self.form.addRow("Number of times", self._number_field(data.get("count", 3), lambda v: self._set_data("count", v), integer=True))
        elif action.action == ActionType.REPEAT_UNTIL.value:
            condition = ConditionEditor(data, variables=self.available_variables)
            condition.changed.connect(lambda: self._set_condition_data(condition.data()))
            self.form.addRow(condition)
            self.form.addRow("Safety limit", self._spin(data.get("max_iterations", 1000), lambda v: self._set_data("max_iterations", v), 1, 10000))
            self.form.addRow("Delay between loops", self._double(data.get("iteration_delay", 0.0), lambda v: self._set_data("iteration_delay", v), 0, 3600))
        elif action.action in {ActionType.ELSE.value, ActionType.END_IF.value, ActionType.END_LOOP.value, ActionType.BREAK_LOOP.value}:
            note = QLabel({
                ActionType.ELSE.value: "Runs when the matching If condition is false.",
                ActionType.END_IF.value: "Closes the matching If block.",
                ActionType.END_LOOP.value: "Closes the matching Repeat block.",
                ActionType.BREAK_LOOP.value: "Leaves the nearest Repeat block immediately.",
            }[action.action])
            note.setWordWrap(True); self.form.addRow(note)
        elif is_click_image:
            self._click_image_fields(data)
        elif action.action == ActionType.TYPE_TEXT.value:
            text = QPlainTextEdit(str(data.get("text", "")))
            text.setMinimumHeight(90)
            text.textChanged.connect(lambda: self._set_data("text", text.toPlainText()))
            self.form.addRow("Text to type", text)
            self.advanced_form.addRow("Typing interval", self._double(data.get("interval", 0.02), lambda v: self._set_data("interval", v), 0, 10))
            self.advanced_form.addRow("Clear field first", self._check(data.get("clear_first", False), lambda v: self._set_data("clear_first", v)))
            self.advanced_form.addRow("Protected value", self._check(data.get("masked", False), lambda v: self._set_data("masked", v)))
            self.advanced_form.addRow("Store typed value as", self._line(data.get("output_variable", ""), lambda v: self._set_data("output_variable", v)))
        elif action.action == ActionType.PRESS_KEY.value:
            self.form.addRow("Key", self._line(data.get("key", ""), lambda v: self._set_data("key", v)))
            self.advanced_form.addRow("Number of presses", self._spin(data.get("count", 1), lambda v: self._set_data("count", v)))
            self.advanced_form.addRow("Interval", self._double(data.get("interval", 0), lambda v: self._set_data("interval", v), 0, 99))
        elif action.action == ActionType.HOTKEY.value:
            self.form.addRow("Shortcut", self._line("+".join(data.get("keys", [])), lambda v: self._set_data("keys", [p.strip() for p in v.split("+") if p.strip()])))
        elif action.action == ActionType.SCROLL.value:
            self.form.addRow("Scroll amount", self._number_field(data.get("amount", 0), lambda v: self._set_data("amount", v), integer=True))
            self.advanced_form.addRow("Original X", self._number_field(data.get("x", 0), lambda v: self._set_data("x", v), integer=True))
            self.advanced_form.addRow("Original Y", self._number_field(data.get("y", 0), lambda v: self._set_data("y", v), integer=True))
            self.advanced_form.addRow("Move to original position first", self._check(data.get("move_to", True), lambda v: self._set_data("move_to", v)))
        elif action.action == ActionType.WAIT.value:
            self.form.addRow("Wait time", self._number_field(data.get("seconds", 1), lambda v: self._set_data("seconds", v)))
        elif action.action == ActionType.OPEN_FILE.value:
            self.form.addRow("File", self._file_picker(data.get("path", "")))
            self.advanced_form.addRow("Wait after opening", self._double(data.get("wait_after", 1), lambda v: self._set_data("wait_after", v), 0, 999))
            self.advanced_form.addRow("Expected window title", self._line(data.get("expected_window_title", ""), lambda v: self._set_data("expected_window_title", v)))
            self.advanced_form.addRow("Store opened path as", self._line(data.get("output_variable", ""), lambda v: self._set_data("output_variable", v)))
        elif action.action in (ActionType.RUN_PYTHON.value, ActionType.PYTHON_CODE.value):
            warning = QLabel("Trusted code runs with your current user permissions.")
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #9a3412;")
            code = QPlainTextEdit(str(data.get("code", "")))
            code.setFont(QFont("Consolas", 10))
            code.setMinimumHeight(180)
            code.textChanged.connect(lambda: self._set_data("code", code.toPlainText()))
            self.form.addRow(warning)
            self.form.addRow("Python code", code)
            if action.action == ActionType.PYTHON_CODE.value:
                self.advanced_form.addRow("Continue after an error", self._check(data.get("continue_on_error", False), lambda v: self._set_data("continue_on_error", v)))
            self.advanced_form.addRow("Output variable", self._line(data.get("output_variable", ""), lambda v: self._set_data("output_variable", v)))
        elif action.action == ActionType.CLICK_COORDINATE.value:
            self.form.addRow("X", self._number_field(data.get("x", 0), lambda v: self._set_data("x", v), integer=True))
            self.form.addRow("Y", self._number_field(data.get("y", 0), lambda v: self._set_data("y", v), integer=True))
            self.advanced_form.addRow("Mouse button", self._line(data.get("button", "left"), lambda v: self._set_data("button", v)))
        elif action.action == ActionType.MOUSE_MOVE.value:
            self.form.addRow("X", self._number_field(data.get("x", 0), lambda v: self._set_data("x", v), integer=True))
            self.form.addRow("Y", self._number_field(data.get("y", 0), lambda v: self._set_data("y", v), integer=True))
            self.advanced_form.addRow("Move duration", self._double(data.get("duration", 0.2), lambda v: self._set_data("duration", v), 0, 60))
        elif action.action == ActionType.DRAG.value:
            self.form.addRow("Start X", self._number_field(data.get("start_x", 0), lambda v: self._set_data("start_x", v), integer=True))
            self.form.addRow("Start Y", self._number_field(data.get("start_y", 0), lambda v: self._set_data("start_y", v), integer=True))
            self.form.addRow("End X", self._number_field(data.get("end_x", 0), lambda v: self._set_data("end_x", v), integer=True))
            self.form.addRow("End Y", self._number_field(data.get("end_y", 0), lambda v: self._set_data("end_y", v), integer=True))
            self.advanced_form.addRow("Drag duration", self._double(data.get("duration", 0.5), lambda v: self._set_data("duration", v), 0, 60))
        if is_control:
            self._loading = False
            return
        retry_heading = QLabel("Retry and failure handling")
        retry_heading.setStyleSheet("font-weight: 600; margin-top: 8px;")
        self.advanced_form.addRow(retry_heading)
        self.advanced_form.addRow(
            "Retry count",
            self._spin(data.get("retry_count", 0), lambda v: self._set_data("retry_count", v), 0, 20),
        )
        self.advanced_form.addRow(
            "Delay between retries",
            self._double(data.get("retry_delay", 1.0), lambda v: self._set_data("retry_delay", v), 0, 3600),
        )
        self.advanced_form.addRow(
            "Step timeout (0 = off)",
            self._double(data.get("step_timeout", 0.0), lambda v: self._set_data("step_timeout", v), 0, 86400),
        )
        failure_options = [
            ("Stop Flow", "stop"),
            ("Continue", "continue"),
            ("Jump to Step", "jump"),
        ]
        self.advanced_form.addRow(
            "On final failure",
            self._combo(failure_options, data.get("failure_action", "stop"), lambda v: self._set_data("failure_action", v)),
        )
        self.advanced_form.addRow(
            "Jump target step",
            self._spin(data.get("failure_jump_step", 1), lambda v: self._set_data("failure_jump_step", v), 1, 9999),
        )
        self.advanced_form.addRow(
            "Capture final failure",
            self._check(data.get("capture_failure_screenshot", False), lambda v: self._set_data("capture_failure_screenshot", v)),
        )
        self.advanced_form.addRow(
            "Capture before step",
            self._check(data.get("capture_before", False), lambda v: self._set_data("capture_before", v)),
        )
        self.advanced_form.addRow(
            "Capture after step",
            self._check(data.get("capture_after", False), lambda v: self._set_data("capture_after", v)),
        )
        self._loading = False

    def _click_image_fields(self, data: dict) -> None:
        self.advanced_form.addRow("Target image file", self._line(data.get("image", ""), lambda v: self._set_data("image", v)))
        self.advanced_form.addRow("Match accuracy", self._number_field(data.get("confidence", 0.86), lambda v: self._set_data("confidence", v)))
        self.advanced_form.addRow("Search timeout", self._number_field(data.get("timeout", 10), lambda v: self._set_data("timeout", v)))
        self.advanced_form.addRow("Mouse button", self._line(data.get("button", "left"), lambda v: self._set_data("button", v)))
        self.advanced_form.addRow("Original X", self._number_field(data.get("fallback_x", 0), lambda v: self._set_data("fallback_x", v), integer=True))
        self.advanced_form.addRow("Original Y", self._number_field(data.get("fallback_y", 0), lambda v: self._set_data("fallback_y", v), integer=True))
        self.advanced_form.addRow("Use original position if target is not found", self._check(data.get("use_coordinate_fallback", True), lambda v: self._set_data("use_coordinate_fallback", v)))
        self.advanced_form.addRow("Click point offset X", self._number_field(data.get("click_offset_x", 0), lambda v: self._set_data("click_offset_x", v), integer=True))
        self.advanced_form.addRow("Click point offset Y", self._number_field(data.get("click_offset_y", 0), lambda v: self._set_data("click_offset_y", v), integer=True))
        if self.project_dir and data.get("image"):
            image = self.project_dir / str(data["image"])
            if image.exists():
                pixmap = QPixmap(str(image))
                self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.preview.setText("Target image is missing. Recapture it or update the image path in Advanced Settings.")

    def _suggested_name(self, action: RpaAction) -> str:
        summary = action.summary()
        return summary if summary != action.name else action.friendly_name()

    def _set_name(self, value: str) -> None:
        if self.action and not self._loading:
            self.action.name = value.strip()
            if self.action.action == ActionType.PYTHON_CODE.value:
                self.action.data["name"] = self.action.name
            self.action_changed.emit()

    def _set(self, key: str, value) -> None:
        if self.action and not self._loading:
            setattr(self.action, key, value)
            self.action_changed.emit()

    def _set_data(self, key: str, value) -> None:
        if self.action and not self._loading:
            self.action.data[key] = value
            self.action_changed.emit()

    def _set_condition_data(self, values: dict) -> None:
        if self.action and not self._loading:
            condition_keys = {
                "condition_type", "image", "confidence", "window_title", "case_sensitive",
                "path", "path_type", "variable", "operator", "value",
            }
            for key in condition_keys:
                self.action.data.pop(key, None)
            self.action.data.update(values)
            self.action_changed.emit()

    def _line(self, value, callback) -> QLineEdit:
        widget = QLineEdit(str(value))
        widget.editingFinished.connect(lambda: callback(widget.text()))
        return widget

    def _number_field(self, value, callback, integer: bool = False) -> QLineEdit:
        widget = QLineEdit(str(value))
        widget.setPlaceholderText("Number or {{VARIABLE}}")
        def commit() -> None:
            text = widget.text().strip()
            if "{{" in text:
                callback(text)
                return
            try:
                callback(int(float(text)) if integer else float(text))
            except ValueError:
                callback(text)
        widget.editingFinished.connect(commit)
        return widget

    def _spin(self, value, callback, minimum=1, maximum=9999) -> QWidget:
        if "{{" in str(value):
            return self._number_field(value, callback, integer=True)
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(int(value or 0))
        widget.valueChanged.connect(callback)
        return widget

    def _double(self, value, callback, minimum=0, maximum=9999) -> QWidget:
        if "{{" in str(value):
            return self._number_field(value, callback)
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(2)
        widget.setValue(float(value or 0))
        widget.valueChanged.connect(callback)
        return widget

    def _check(self, value, callback) -> QCheckBox:
        widget = QCheckBox()
        widget.setChecked(bool(value))
        widget.toggled.connect(callback)
        return widget

    def _combo(self, options: list[tuple[str, str]], value: str, callback) -> QComboBox:
        widget = QComboBox()
        for label, data in options:
            widget.addItem(label, data)
        index = widget.findData(str(value))
        widget.setCurrentIndex(index if index >= 0 else 0)
        widget.currentIndexChanged.connect(lambda: callback(widget.currentData()))
        return widget

    def _file_picker(self, value: str) -> QWidget:
        edit = self._line(value, lambda v: self._set_data("path", v))
        button = QPushButton("Browse")
        button.clicked.connect(lambda: self._browse_file(edit))
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(edit)
        row.addWidget(button)
        box = QWidget()
        box.setLayout(row)
        return box

    def _browse_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select file")
        if path:
            edit.setText(path)
            self._set_data("path", path)

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_button.setText("Advanced Settings - Hide" if expanded else "Advanced Settings")
        self.advanced_widget.setVisible(expanded and self.action is not None)
        if not self._loading:
            self.advanced_changed.emit(expanded)

    def _test_locator(self) -> None:
        if self.action:
            self.test_locator_requested.emit(self.action)

    def _test_step(self) -> None:
        if self.action:
            self.test_step_requested.emit(self.action)

    def _recapture_target(self) -> None:
        if self.action:
            self.recapture_requested.emit(self.action)

    def focus_main_field(self) -> None:
        for widget_type in (QPlainTextEdit, QLineEdit, QDoubleSpinBox, QSpinBox, QCheckBox):
            widget = self.findChild(widget_type)
            if widget and widget.isVisible():
                widget.setFocus()
                return
