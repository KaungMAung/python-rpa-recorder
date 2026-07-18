from __future__ import annotations

from pathlib import Path
import shutil
from uuid import uuid4
import shiboken6

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
    QListWidget,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from rpa.models import ActionType, RpaAction
from rpa.control_flow import CONTROL_TYPES
from ui.condition_editor import ConditionEditor
from ui.window_target_editor import WindowTargetEditor


WINDOW_ACTIONS = {
    ActionType.SELECT_WINDOW.value, ActionType.WAIT_WINDOW.value,
    ActionType.ACTIVATE_WINDOW.value, ActionType.MAXIMIZE_WINDOW.value,
    ActionType.MINIMIZE_WINDOW.value, ActionType.RESTORE_WINDOW.value,
    ActionType.CLOSE_WINDOW.value, ActionType.CLICK_WINDOW_RELATIVE.value,
    ActionType.MOVE_WINDOW_RELATIVE.value,
}


class ActionEditor(QWidget):
    action_changed = Signal()
    close_requested = Signal()
    test_step_requested = Signal(RpaAction)
    test_locator_requested = Signal(RpaAction)
    recapture_requested = Signal(RpaAction)
    search_region_requested = Signal(RpaAction)
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
        self.locate_button = QPushButton("Test Match Now")
        self.locate_button.setToolTip("Find every visible match and preview the exact click location.")
        self.locate_button.clicked.connect(self._test_locator)
        self.recapture_button = QPushButton("Capture / Crop Target")
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
                if isinstance(widget, WindowTargetEditor) and shiboken6.isValid(widget):
                    widget.dispose()
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
        elif action.action in WINDOW_ACTIONS:
            target = WindowTargetEditor(data, allow_selected=action.action != ActionType.SELECT_WINDOW.value)
            target.pick_button.setVisible(False)
            target.captured_label.setText("Edit matching details here. Use Pick Window when adding a new step.")
            target.changed.connect(lambda: self._set_window_data(target.data()))
            self.form.addRow(target)
            if action.action in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value}:
                self.form.addRow("X from window left", self._number_field(data.get("relative_x", 0), lambda v: self._set_data("relative_x", v), integer=True))
                self.form.addRow("Y from window top", self._number_field(data.get("relative_y", 0), lambda v: self._set_data("relative_y", v), integer=True))
                self.form.addRow("Scale when resized", self._check(data.get("scale_with_window", False), lambda v: self._set_data("scale_with_window", v)))
                self.advanced_form.addRow("Original window width", self._number_field(data.get("original_window_width", 0), lambda v: self._set_data("original_window_width", v), integer=True))
                self.advanced_form.addRow("Original window height", self._number_field(data.get("original_window_height", 0), lambda v: self._set_data("original_window_height", v), integer=True))
                self.advanced_form.addRow("Use absolute fallback", self._check(data.get("use_absolute_fallback", False), lambda v: self._set_data("use_absolute_fallback", v)))
                self.advanced_form.addRow("Fallback X", self._number_field(data.get("fallback_x", 0), lambda v: self._set_data("fallback_x", v), integer=True))
                self.advanced_form.addRow("Fallback Y", self._number_field(data.get("fallback_y", 0), lambda v: self._set_data("fallback_y", v), integer=True))
                if action.action == ActionType.CLICK_WINDOW_RELATIVE.value:
                    self.form.addRow("Mouse button", self._combo(
                        [("Left", "left"), ("Right", "right"), ("Middle", "middle")],
                        data.get("button", "left"), lambda v: self._set_data("button", v),
                    ))
                else:
                    self.form.addRow("Move duration", self._double(data.get("duration", 0.2), lambda v: self._set_data("duration", v), 0, 60))
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
        references = [str(data.get("image", ""))] if data.get("image") else []
        references.extend(str(item) for item in data.get("reference_images", []) if str(item))
        reference_list = QListWidget()
        reference_list.setObjectName("imageReferenceList")
        reference_list.setMinimumHeight(78)
        for index, reference in enumerate(references):
            reference_list.addItem(f"{index + 1}. {Path(reference).name}")
        reference_list.setToolTip("References are attempted in this order; the first qualifying image wins.")
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        for label, handler in (
            ("Add Images", lambda: self._add_reference_images(reference_list)),
            ("Replace", lambda: self._replace_reference_image(reference_list)),
            ("Remove", lambda: self._remove_reference_image(reference_list)),
            ("Move Up", lambda: self._move_reference(reference_list, -1)),
            ("Move Down", lambda: self._move_reference(reference_list, 1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            controls.addWidget(button)
        reference_box = QWidget()
        reference_layout = QVBoxLayout(reference_box)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        reference_layout.addWidget(reference_list)
        reference_layout.addLayout(controls)
        self.form.addRow("Reference images", reference_box)

        confidence = float(data.get("confidence", 0.86))
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("imageConfidenceSlider")
        slider.setRange(50, 100)
        slider.setValue(round(confidence * 100))
        slider.setToolTip("Higher values reduce false matches but tolerate fewer visual changes.")
        value_label = QLabel(f"{confidence:.0%}")
        slider.valueChanged.connect(
            lambda value: (value_label.setText(f"{value}%"), self._set_data("confidence", value / 100.0))
        )
        confidence_layout = QHBoxLayout()
        confidence_layout.setContentsMargins(0, 0, 0, 0)
        confidence_layout.addWidget(slider, 1)
        confidence_layout.addWidget(value_label)
        confidence_box = QWidget(); confidence_box.setLayout(confidence_layout)
        self.form.addRow("Match confidence", confidence_box)

        region = data.get("search_region") or {}
        region_label = QLabel(
            f"X {region.get('x')}, Y {region.get('y')}, {region.get('width')} x {region.get('height')}"
            if region else "Entire desktop"
        )
        choose_region = QPushButton("Select on Screen")
        clear_region = QPushButton("Clear")
        choose_region.clicked.connect(self._select_search_region)
        clear_region.clicked.connect(lambda: self._clear_search_region(region_label))
        region_layout = QHBoxLayout(); region_layout.setContentsMargins(0, 0, 0, 0)
        region_layout.addWidget(region_label, 1); region_layout.addWidget(choose_region); region_layout.addWidget(clear_region)
        region_box = QWidget(); region_box.setLayout(region_layout)
        self.form.addRow("Search area", region_box)

        fallback_enabled = bool(data.get("use_coordinate_fallback", True))
        fallback_check = self._check(fallback_enabled, lambda value: self._set_data("use_coordinate_fallback", value))
        fallback_status = QLabel()
        self._update_fallback_status(fallback_status, fallback_enabled)
        fallback_check.toggled.connect(lambda enabled: self._update_fallback_status(fallback_status, enabled))
        fallback_layout = QHBoxLayout(); fallback_layout.setContentsMargins(0, 0, 0, 0)
        fallback_layout.addWidget(fallback_check); fallback_layout.addWidget(fallback_status, 1)
        fallback_box = QWidget(); fallback_box.setLayout(fallback_layout)
        self.form.addRow("Coordinate fallback", fallback_box)

        self.advanced_form.addRow("Search timeout", self._number_field(data.get("timeout", 10), lambda v: self._set_data("timeout", v)))
        self.advanced_form.addRow("Grayscale matching", self._check(data.get("grayscale", False), lambda v: self._set_data("grayscale", v)))
        self.advanced_form.addRow("Match priority", self._combo([
            ("Highest confidence", "highest_confidence"), ("Leftmost", "leftmost"),
            ("Rightmost", "rightmost"), ("Topmost", "topmost"),
            ("Bottommost", "bottommost"), ("Specific match number", "match_index"),
        ], data.get("match_priority", "highest_confidence"), lambda v: self._set_data("match_priority", v)))
        self.advanced_form.addRow("Match number", self._spin(data.get("match_index", 1), lambda v: self._set_data("match_index", v), 1, 100))
        self.advanced_form.addRow("Mouse button", self._combo([
            ("Left", "left"), ("Right", "right"), ("Middle", "middle")
        ], data.get("button", "left"), lambda v: self._set_data("button", v)))
        self.advanced_form.addRow("Original X", self._number_field(data.get("fallback_x", 0), lambda v: self._set_data("fallback_x", v), integer=True))
        self.advanced_form.addRow("Original Y", self._number_field(data.get("fallback_y", 0), lambda v: self._set_data("fallback_y", v), integer=True))
        self.advanced_form.addRow("Click point offset X", self._number_field(data.get("click_offset_x", 0), lambda v: self._set_data("click_offset_x", v), integer=True))
        self.advanced_form.addRow("Click point offset Y", self._number_field(data.get("click_offset_y", 0), lambda v: self._set_data("click_offset_y", v), integer=True))
        if self.project_dir and data.get("image"):
            image = self.project_dir / str(data["image"])
            if image.exists():
                pixmap = QPixmap(str(image))
                self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.preview.setText("Target image is missing. Capture it again or add a replacement image.")

    def _reference_paths(self) -> list[str]:
        if not self.action:
            return []
        primary = str(self.action.data.get("image", ""))
        return ([primary] if primary else []) + [str(item) for item in self.action.data.get("reference_images", []) if str(item)]

    def _store_reference_paths(self, references: list[str]) -> None:
        if not self.action or not references:
            return
        self.action.data["image"] = references[0]
        if len(references) > 1:
            self.action.data["reference_images"] = references[1:]
        else:
            self.action.data.pop("reference_images", None)
        self.action_changed.emit()
        self._rebuild()

    def _add_reference_images(self, _widget: QListWidget) -> None:
        if not self.action or not self.project_dir:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Reference Images", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if not paths:
            return
        destination = self.project_dir / "screenshots"
        destination.mkdir(parents=True, exist_ok=True)
        references = self._reference_paths()
        for source_text in paths:
            source = Path(source_text)
            target = destination / f"reference_{self.action.id[:8]}_{uuid4().hex[:8]}{source.suffix.lower()}"
            shutil.copy2(source, target)
            references.append(target.relative_to(self.project_dir).as_posix())
        self._store_reference_paths(references)

    def _remove_reference_image(self, widget: QListWidget) -> None:
        row = widget.currentRow()
        references = self._reference_paths()
        if row < 0 or row >= len(references) or len(references) == 1:
            return
        references.pop(row)
        self._store_reference_paths(references)

    def _replace_reference_image(self, widget: QListWidget) -> None:
        if not self.action or not self.project_dir:
            return
        row = widget.currentRow()
        references = self._reference_paths()
        if row < 0 or row >= len(references):
            return
        source_text, _ = QFileDialog.getOpenFileName(
            self, "Replace Reference Image", "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if not source_text:
            return
        source = Path(source_text)
        destination = self.project_dir / "screenshots"
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"reference_{self.action.id[:8]}_{uuid4().hex[:8]}{source.suffix.lower()}"
        shutil.copy2(source, target)
        references[row] = target.relative_to(self.project_dir).as_posix()
        self._store_reference_paths(references)

    def _move_reference(self, widget: QListWidget, offset: int) -> None:
        row = widget.currentRow()
        references = self._reference_paths()
        target = row + offset
        if row < 0 or target < 0 or target >= len(references):
            return
        references[row], references[target] = references[target], references[row]
        self._store_reference_paths(references)

    def _select_search_region(self) -> None:
        if self.action:
            self.search_region_requested.emit(self.action)

    def _clear_search_region(self, label: QLabel) -> None:
        if self.action:
            self.action.data.pop("search_region", None)
            label.setText("Entire desktop")
            self.action_changed.emit()

    def _update_fallback_status(self, label: QLabel, enabled: bool) -> None:
        if not self.action:
            return
        label.setText(
            f"Enabled at ({self.action.data.get('fallback_x', 0)}, {self.action.data.get('fallback_y', 0)})"
            if enabled else "Disabled - image match is required"
        )
        label.setStyleSheet("color: #166534;" if enabled else "color: #9a3412;")

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

    def _set_window_data(self, values: dict) -> None:
        if self.action and not self._loading:
            self.action.data["use_selected_window"] = bool(values.get("use_selected_window", False))
            self.action.data["window"] = dict(values.get("window") or {})
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
