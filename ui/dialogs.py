from __future__ import annotations

from pathlib import Path
import json

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QCheckBox,
    QPlainTextEdit,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rpa.models import (
    ActionType, ProjectSettings, RpaAction, RpaProject, RuntimeInputDefinition, VariableDefinition,
)
from rpa.control_flow import CONTROL_TYPES, METADATA_TYPES
from rpa.variables import (
    INPUT_TYPES, VARIABLE_NAME_PATTERN, VARIABLE_TYPES, coerce_variable_value,
    validate_variable_configuration,
)
from ui.condition_editor import ConditionEditor
from ui.window_target_editor import WindowTargetEditor
from ui.subflow_editor import SubflowEditor
from ui.utility_action_editor import UTILITY_ACTIONS, UtilityActionEditor
import shiboken6


WINDOW_ACTIONS = {
    ActionType.SELECT_WINDOW.value, ActionType.WAIT_WINDOW.value,
    ActionType.ACTIVATE_WINDOW.value, ActionType.MAXIMIZE_WINDOW.value,
    ActionType.MINIMIZE_WINDOW.value, ActionType.RESTORE_WINDOW.value,
    ActionType.CLOSE_WINDOW.value, ActionType.CLICK_WINDOW_RELATIVE.value,
    ActionType.MOVE_WINDOW_RELATIVE.value,
}


def load_default_project_settings() -> ProjectSettings:
    """Build a ProjectSettings using values last saved from the Settings dialog.

    The Settings dialog persists every field to QSettings on accept, but new
    projects previously always started from ProjectSettings()'s hardcoded
    defaults, so changes never carried over to the next flow. This reads
    back whatever was last saved (falling back to the dataclass default for
    any field that hasn't been saved yet).
    """
    qsettings = QSettings("PythonRPARecorder", "PythonRPARecorder")
    defaults = ProjectSettings()
    values: dict = {}
    for key, default_value in defaults.__dict__.items():
        stored = qsettings.value(key, default_value, type=type(default_value))
        values[key] = stored
    return ProjectSettings.from_dict(values)


class ManualActionDialog(QDialog):
    """Plain-language step builder. Screen capture is delegated to MainWindow."""

    screen_pick_requested = Signal(str)
    diagnostic = Signal(str)
    test_match_requested = Signal(RpaAction)
    test_step_requested = Signal(RpaAction)

    GUIDED_INTENTS = [
        ("click", "Click something", "Choose a point, image, mouse movement, drag, or scroll.", [
            ("Click once", ActionType.CLICK_COORDINATE.value),
            ("Double-click", ActionType.DOUBLE_CLICK_IMAGE.value),
            ("Right-click", "right_click"),
            ("Click an image", ActionType.CLICK_IMAGE.value),
            ("Move the mouse", ActionType.MOUSE_MOVE.value),
            ("Drag something", ActionType.DRAG.value),
            ("Scroll", ActionType.SCROLL.value),
        ]),
        ("type", "Type text", "Type text, press one key, or use a keyboard shortcut.", [
            ("Type text", ActionType.TYPE_TEXT.value),
            ("Press one key", ActionType.PRESS_KEY.value),
            ("Use a keyboard shortcut", ActionType.HOTKEY.value),
        ]),
        ("open", "Open an application", "Start an application or open a document.", [
            ("Launch an application", ActionType.LAUNCH_APPLICATION.value),
            ("Open an application or file", ActionType.OPEN_FILE.value),
        ]),
        ("wait", "Wait for something", "Wait for time to pass or for something to appear.", [
            ("Wait for a length of time", ActionType.WAIT.value),
            ("Wait for a window", ActionType.WAIT_WINDOW.value),
            ("Wait for an application process", ActionType.WAIT_PROCESS.value),
            ("Wait for a file or folder", ActionType.WAIT_PATH.value),
        ]),
        ("window", "Work with a window", "Find, activate, resize, close, or click inside a window.", [
            ("Remember a target window", ActionType.SELECT_WINDOW.value),
            ("Wait for a window", ActionType.WAIT_WINDOW.value),
            ("Bring a window to the front", ActionType.ACTIVATE_WINDOW.value),
            ("Maximize a window", ActionType.MAXIMIZE_WINDOW.value),
            ("Minimize a window", ActionType.MINIMIZE_WINDOW.value),
            ("Restore a window", ActionType.RESTORE_WINDOW.value),
            ("Close a window", ActionType.CLOSE_WINDOW.value),
            ("Click inside a window", ActionType.CLICK_WINDOW_RELATIVE.value),
            ("Move the mouse inside a window", ActionType.MOVE_WINDOW_RELATIVE.value),
        ]),
        ("file", "Work with a file", "Open, copy, move, rename, delete, or wait for a file or folder.", [
            ("Open a file", ActionType.OPEN_FILE.value),
            ("Copy a file or folder", ActionType.COPY_PATH.value),
            ("Move a file or folder", ActionType.MOVE_PATH.value),
            ("Rename a file or folder", ActionType.RENAME_PATH.value),
            ("Delete a file or folder", ActionType.DELETE_PATH.value),
            ("Wait for a file or folder", ActionType.WAIT_PATH.value),
        ]),
        ("condition", "Add a condition", "Run steps only when an image, window, path, or value matches.", [
            ("If an image exists", ActionType.IF_IMAGE_EXISTS.value),
            ("If an image does not exist", ActionType.IF_IMAGE_NOT_EXISTS.value),
            ("If a window exists", ActionType.IF_WINDOW_EXISTS.value),
            ("If a file or folder exists", ActionType.IF_PATH_EXISTS.value),
            ("If a variable matches", ActionType.IF_VARIABLE.value),
        ]),
        ("repeat", "Repeat steps", "Repeat a block a number of times or until something happens.", [
            ("Repeat a number of times", ActionType.REPEAT_COUNT.value),
            ("Repeat until a condition is met", ActionType.REPEAT_UNTIL.value),
            ("Leave the current repeat block", ActionType.BREAK_LOOP.value),
        ]),
        ("subflow", "Run another flow", "Choose another saved flow and optionally map its variables.", [
            ("Run another saved flow", ActionType.RUN_SUBFLOW.value),
        ]),
        ("variable", "Work with a variable", "Set, read, increment, append, update, or delete a shared flow value.", [
            ("Set a variable", ActionType.SET_VARIABLE.value),
            ("Read a variable", ActionType.GET_VARIABLE.value),
            ("Increment a number", ActionType.INCREMENT_VARIABLE.value),
            ("Append to a list", ActionType.APPEND_VARIABLE.value),
            ("Set an object property", ActionType.SET_OBJECT_PROPERTY.value),
            ("Delete a variable", ActionType.DELETE_VARIABLE.value),
        ]),
        ("script", "Run a script or command", "Run PowerShell, a Python script, or advanced Python code.", [
            ("Run a PowerShell command", ActionType.RUN_POWERSHELL.value),
            ("Run a Python script", ActionType.RUN_PYTHON_SCRIPT.value),
            ("Run Python code", ActionType.PYTHON_CODE.value),
        ]),
    ]

    def __init__(
        self, settings: ProjectSettings, variables: dict[str, str], parent=None,
        project_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Step")
        self.settings = settings
        self.variables = variables
        self.project_dir = Path(project_dir) if project_dir else None
        self.picked: dict[str, tuple[int, int]] = {}
        self._picker_active = False
        self._picker_snapshot: dict = {}
        self._guided_mode = True
        self._selected_intent: str | None = None
        self.type_box = QComboBox()
        for label, value in [
            ("Click", ActionType.CLICK_COORDINATE.value),
            ("Double Click", ActionType.DOUBLE_CLICK_IMAGE.value),
            ("Right Click", "right_click"),
            ("Image Click", ActionType.CLICK_IMAGE.value),
            ("Mouse Move", ActionType.MOUSE_MOVE.value),
            ("Drag", ActionType.DRAG.value),
            ("Wait", ActionType.WAIT.value),
            ("Type Text", ActionType.TYPE_TEXT.value),
            ("Press Key", ActionType.PRESS_KEY.value),
            ("Hotkey", ActionType.HOTKEY.value),
            ("Scroll", ActionType.SCROLL.value),
            ("Open File", ActionType.OPEN_FILE.value),
            ("Launch Application", ActionType.LAUNCH_APPLICATION.value),
            ("Wait for Process", ActionType.WAIT_PROCESS.value),
            ("Activate Process", ActionType.ACTIVATE_PROCESS.value),
            ("Close Process", ActionType.CLOSE_PROCESS.value),
            ("Read Clipboard", ActionType.READ_CLIPBOARD.value),
            ("Write Clipboard", ActionType.WRITE_CLIPBOARD.value),
            ("Copy File or Folder", ActionType.COPY_PATH.value),
            ("Move File or Folder", ActionType.MOVE_PATH.value),
            ("Rename File or Folder", ActionType.RENAME_PATH.value),
            ("Delete File or Folder", ActionType.DELETE_PATH.value),
            ("Wait for File or Folder", ActionType.WAIT_PATH.value),
            ("Run PowerShell Command", ActionType.RUN_POWERSHELL.value),
            ("Run Python Script", ActionType.RUN_PYTHON_SCRIPT.value),
            ("Show Desktop Notification", ActionType.SHOW_NOTIFICATION.value),
            ("Run Python", ActionType.RUN_PYTHON.value),
            ("Python Code", ActionType.PYTHON_CODE.value),
            ("Run Subflow", ActionType.RUN_SUBFLOW.value),
            ("If Image Exists", ActionType.IF_IMAGE_EXISTS.value),
            ("If Image Does Not Exist", ActionType.IF_IMAGE_NOT_EXISTS.value),
            ("If Window Exists", ActionType.IF_WINDOW_EXISTS.value),
            ("If File or Folder Exists", ActionType.IF_PATH_EXISTS.value),
            ("If Variable", ActionType.IF_VARIABLE.value),
            ("Else", ActionType.ELSE.value),
            ("End If", ActionType.END_IF.value),
            ("Repeat N Times", ActionType.REPEAT_COUNT.value),
            ("Repeat Until", ActionType.REPEAT_UNTIL.value),
            ("End Loop", ActionType.END_LOOP.value),
            ("Break Loop", ActionType.BREAK_LOOP.value),
            ("Select / Target Window", ActionType.SELECT_WINDOW.value),
            ("Wait for Window", ActionType.WAIT_WINDOW.value),
            ("Activate Window", ActionType.ACTIVATE_WINDOW.value),
            ("Maximize Window", ActionType.MAXIMIZE_WINDOW.value),
            ("Minimize Window", ActionType.MINIMIZE_WINDOW.value),
            ("Restore Window", ActionType.RESTORE_WINDOW.value),
            ("Close Window", ActionType.CLOSE_WINDOW.value),
            ("Click Relative to Window", ActionType.CLICK_WINDOW_RELATIVE.value),
            ("Move Mouse Relative to Window", ActionType.MOVE_WINDOW_RELATIVE.value),
            ("Set Variable", ActionType.SET_VARIABLE.value),
            ("Get Variable", ActionType.GET_VARIABLE.value),
            ("Increment Variable", ActionType.INCREMENT_VARIABLE.value),
            ("Append to List", ActionType.APPEND_VARIABLE.value),
            ("Set Object Property", ActionType.SET_OBJECT_PROPERTY.value),
            ("Delete Variable", ActionType.DELETE_VARIABLE.value),
        ]:
            self.type_box.addItem(label, value)
        self.form = QFormLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background: #f1f5f9; color: #334155; padding: 8px; border: 1px solid #d8dee8;")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box = buttons
        self.confirm_button = buttons.button(QDialogButtonBox.Ok)
        self.confirm_button.setText("Add Step")
        buttons.button(QDialogButtonBox.Cancel).setText("Discard")
        self.confirm_button.setDefault(True)
        self.confirm_button.setToolTip("Add this configured step to the current flow")
        # Wire the visible primary button directly. Do not also connect the
        # button-box accepted signal, which can obscure which path closed the
        # dialog when diagnosing a rejected result.
        self.confirm_button.clicked.connect(self._confirm)
        buttons.rejected.connect(self.reject)
        self.pages = QStackedWidget()
        self.intent_page = self._build_intent_page()
        self.choice_page = self._build_choice_page()
        self.details_page = QScrollArea()
        self.details_page.setWidgetResizable(True)
        self.details_page.setFrameShape(QScrollArea.NoFrame)
        details_content = QWidget()
        self.details_page.setWidget(details_content)
        details_layout = QVBoxLayout(details_content)
        details_layout.setContentsMargins(4, 4, 4, 4)
        details_layout.setSpacing(10)
        details_header = QHBoxLayout()
        self.details_back_button = QPushButton("← Back")
        self.details_back_button.clicked.connect(self._back_from_details)
        self.details_heading = QLabel("Configure this step")
        self.details_heading.setStyleSheet("font-size: 16px; font-weight: 650;")
        details_header.addWidget(self.details_back_button)
        details_header.addWidget(self.details_heading, 1)
        details_layout.addLayout(details_header)
        self.advanced_toggle = QPushButton("Advanced  ▸")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolTip("Show the complete action list for experienced users")
        self.advanced_toggle.toggled.connect(self._toggle_guided_advanced)
        details_layout.addWidget(self.advanced_toggle)
        self.type_selector_widget = QWidget()
        top = QFormLayout(self.type_selector_widget)
        top.setContentsMargins(0, 0, 0, 0)
        top.addRow("Technical action type", self.type_box)
        self.type_selector_widget.setVisible(False)
        details_layout.addWidget(self.type_selector_widget)
        details_layout.addLayout(self.form)
        test_row = QHBoxLayout()
        self.test_match_button = QPushButton("Test Match")
        self.test_match_button.setToolTip("Check the target image now without adding the step")
        self.test_match_button.clicked.connect(self._test_match)
        self.test_step_button = QPushButton("Test Step")
        self.test_step_button.setToolTip("Run this configured step once without adding it to the flow")
        self.test_step_button.clicked.connect(self._test_step)
        test_row.addWidget(self.test_match_button)
        test_row.addWidget(self.test_step_button)
        test_row.addStretch(1)
        details_layout.addLayout(test_row)
        details_layout.addWidget(QLabel("Live step summary"))
        details_layout.addWidget(self.summary)
        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        details_layout.addWidget(self.validation_label)
        confirmation_note = QLabel("Click Add Step to add this step. Discard closes without changing the flow.")
        confirmation_note.setStyleSheet("color: #475569;")
        details_layout.addWidget(confirmation_note)
        details_layout.addWidget(buttons)
        self.pages.addWidget(self.intent_page)
        self.pages.addWidget(self.choice_page)
        self.pages.addWidget(self.details_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(self.pages)
        self.type_box.currentIndexChanged.connect(self._rebuild)
        self._rebuild()
        self.pages.setCurrentWidget(self.intent_page)
        self.resize(720, 620)

    def _build_intent_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("What would you like the flow to do?")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        description = QLabel("Choose the outcome in everyday language. You can fine-tune it on the next screens.")
        description.setWordWrap(True)
        description.setStyleSheet("color: #64748b;")
        layout.addWidget(title)
        layout.addWidget(description)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.intent_buttons: dict[str, QPushButton] = {}
        for index, (key, label, help_text, _choices) in enumerate(self.GUIDED_INTENTS):
            button = QPushButton(label)
            button.setObjectName(f"intent_{key}")
            button.setMinimumHeight(46)
            button.setToolTip(help_text)
            button.setStyleSheet("text-align: left; padding: 9px 12px; font-weight: 600;")
            button.clicked.connect(lambda _checked=False, selected=key: self._choose_intent(selected))
            self.intent_buttons[key] = button
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch(1)
        full = QPushButton("Use the full step editor")
        full.setToolTip("Show every technical action type in the existing editor")
        full.clicked.connect(self._use_full_editor)
        layout.addWidget(full)
        return page

    def _build_choice_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        back = QPushButton("← Back")
        back.clicked.connect(lambda: self.pages.setCurrentWidget(self.intent_page))
        self.choice_heading = QLabel("Choose an action")
        self.choice_heading.setStyleSheet("font-size: 18px; font-weight: 700;")
        header.addWidget(back)
        header.addWidget(self.choice_heading, 1)
        layout.addLayout(header)
        self.choice_help = QLabel()
        self.choice_help.setWordWrap(True)
        self.choice_help.setStyleSheet("color: #64748b;")
        layout.addWidget(self.choice_help)
        question = QLabel("Which best describes what should happen?")
        question.setStyleSheet("font-weight: 600;")
        layout.addWidget(question)
        self.guided_type_box = QComboBox()
        self.guided_type_box.setObjectName("guidedActionChoice")
        self.guided_type_box.currentIndexChanged.connect(self._guided_choice_changed)
        layout.addWidget(self.guided_type_box)
        self.choice_validation = QLabel("Choose one option to continue.")
        self.choice_validation.setStyleSheet("color: #b45309;")
        layout.addWidget(self.choice_validation)
        layout.addStretch(1)
        controls = QHBoxLayout()
        full = QPushButton("Use the full step editor")
        full.clicked.connect(self._use_full_editor)
        self.choice_continue = QPushButton("Continue →")
        self.choice_continue.setDefault(True)
        self.choice_continue.clicked.connect(self._show_guided_details)
        controls.addWidget(full)
        controls.addStretch(1)
        controls.addWidget(self.choice_continue)
        layout.addLayout(controls)
        return page

    def _intent_definition(self, key: str):
        return next((item for item in self.GUIDED_INTENTS if item[0] == key), None)

    def _choose_intent(self, key: str) -> None:
        definition = self._intent_definition(key)
        if definition is None:
            return
        self._selected_intent = key
        _key, label, help_text, choices = definition
        self.choice_heading.setText(label)
        self.choice_help.setText(help_text)
        self.guided_type_box.blockSignals(True)
        self.guided_type_box.clear()
        self.guided_type_box.addItem("Choose what should happen…", None)
        for choice_label, action_type in choices:
            self.guided_type_box.addItem(choice_label, action_type)
        self.guided_type_box.setCurrentIndex(0)
        self.guided_type_box.blockSignals(False)
        self._guided_choice_changed()
        self.pages.setCurrentWidget(self.choice_page)

    def _guided_choice_changed(self, _index: int | None = None) -> None:
        selected = self.guided_type_box.currentData() is not None
        self.choice_continue.setEnabled(selected)
        self.choice_validation.setText("Ready to continue." if selected else "Choose one option to continue.")
        self.choice_validation.setStyleSheet("color: #166534;" if selected else "color: #b45309;")

    def _show_guided_details(self) -> None:
        action_type = self.guided_type_box.currentData()
        if action_type is None:
            self.choice_validation.setText("Choose what should happen before continuing.")
            return
        index = self.type_box.findData(action_type)
        if index < 0:
            self.choice_validation.setText("That step type is unavailable in this version.")
            return
        self._guided_mode = True
        if self.type_box.currentIndex() == index:
            self._rebuild()
        else:
            self.type_box.setCurrentIndex(index)
        self.type_selector_widget.setVisible(self.advanced_toggle.isChecked())
        self.details_heading.setText(self.guided_type_box.currentText())
        self.pages.setCurrentWidget(self.details_page)
        self._update_summary()

    def _use_full_editor(self) -> None:
        self._guided_mode = False
        self._selected_intent = None
        self.details_heading.setText("Full Step Editor")
        self.type_selector_widget.setVisible(True)
        self.advanced_toggle.setVisible(False)
        self.details_back_button.setText("← Guided choices")
        self._rebuild()
        self.pages.setCurrentWidget(self.details_page)
        self._update_summary()

    def _back_from_details(self) -> None:
        self.advanced_toggle.setVisible(True)
        self.details_back_button.setText("← Back")
        if self._guided_mode and self._selected_intent:
            self.pages.setCurrentWidget(self.choice_page)
        else:
            self._guided_mode = True
            self.type_selector_widget.setVisible(False)
            self.pages.setCurrentWidget(self.intent_page)

    def _toggle_guided_advanced(self, expanded: bool) -> None:
        self.advanced_toggle.setText("Advanced  ▾" if expanded else "Advanced  ▸")
        if self._guided_mode:
            self.type_selector_widget.setVisible(expanded)

    def select_intent(self, key: str, action_type: str | None = None) -> None:
        """Public helper used by keyboard integrations and UI regression tests."""
        self._choose_intent(key)
        if action_type is not None:
            index = self.guided_type_box.findData(action_type)
            self.guided_type_box.setCurrentIndex(index)

    def _test_match(self) -> None:
        error = self._validation_error()
        if error:
            self._show_inline_validation(error)
            return
        action = self.action()
        if action.action not in (
            ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value,
            ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value,
        ):
            self._show_inline_validation("Capture or choose a target image before testing the match.")
            return
        self.test_match_requested.emit(action)

    def _test_step(self) -> None:
        error = self._validation_error()
        if error:
            self._show_inline_validation(error)
            return
        self.test_step_button.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.validation_label.setText("Testing this step… Use the floating Stop control to cancel.")
        self.test_step_requested.emit(self.action())

    def finish_step_test(self) -> None:
        if not shiboken6.isValid(self):
            return
        self._update_summary()

    def _show_inline_validation(self, message: str | None) -> None:
        if message:
            self.validation_label.setText(f"What is still needed: {message}")
            self.validation_label.setStyleSheet(
                "color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; padding: 7px;"
            )
        else:
            self.validation_label.setText("Ready to add. All required information is present.")
            self.validation_label.setStyleSheet(
                "color: #166534; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 7px;"
            )

    def _clear_form(self) -> None:
        window_editor = getattr(self, "window_editor", None)
        if isinstance(window_editor, WindowTargetEditor) and shiboken6.isValid(window_editor):
            window_editor.dispose()
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()
        # Dynamic form controls are replaced whenever the action type changes.
        # Keeping Python attributes to their deleted C++ objects is unsafe and
        # was the root cause of Pick Window reading a deleted QCheckBox.
        for name in (
            "target_x", "target_y", "target_pick_button", "capture_image", "mode_note", "image_file",
            "start_x", "start_y", "start_pick_button", "end_x", "end_y", "end_pick_button",
            "direction", "amount", "text", "wait_ms", "path", "key", "keys", "condition_editor",
            "repeat_count", "max_iterations", "iteration_delay", "window_editor", "relative_x", "relative_y",
            "scale_window", "absolute_fallback", "window_button", "window_move_duration",
            "subflow_editor",
            "utility_editor",
        ):
            if hasattr(self, name):
                delattr(self, name)

    def _pick_row(self, role: str, label: str) -> tuple[QSpinBox, QSpinBox]:
        x, y = QSpinBox(), QSpinBox()
        for field in (x, y):
            field.setRange(-99999, 99999)
            field.valueChanged.connect(self._update_summary)
        button = QPushButton("Pick on Screen")
        button.setToolTip("Hide this dialog and select a position. Esc or right-click cancels without changing this step.")
        button.clicked.connect(lambda: self.screen_pick_requested.emit(role))
        setattr(self, f"{role}_pick_button", button)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("X")); row.addWidget(x); row.addWidget(QLabel("Y")); row.addWidget(y); row.addWidget(button)
        box = QVBoxLayout(); box.addLayout(row)
        wrapper = QWidget(); wrapper.setLayout(box)
        self.form.addRow(label, wrapper)
        setattr(self, f"{role}_x", x); setattr(self, f"{role}_y", y)
        return x, y

    def _rebuild(self) -> None:
        if self._picker_active:
            return
        self._clear_form()
        kind = self.type_box.currentData()
        if kind in (ActionType.CLICK_COORDINATE.value, ActionType.DOUBLE_CLICK_IMAGE.value, "right_click", ActionType.CLICK_IMAGE.value):
            self._pick_row("target", "Target position")
            self.capture_image = QCheckBox("Also capture a target image (recommended)")
            self.capture_image.setChecked(kind in (ActionType.DOUBLE_CLICK_IMAGE.value, ActionType.CLICK_IMAGE.value))
            self.capture_image.setToolTip("Uses image matching first; coordinate fallback keeps the step resilient.")
            self.capture_image.toggled.connect(self._update_summary)
            self.form.addRow("How to find it", self.capture_image)
            self.mode_note = QLabel()
            self.mode_note.setWordWrap(True)
            self.mode_note.setStyleSheet("color: #475569;")
            self.form.addRow("Execution mode", self.mode_note)
            self.image_file = QLineEdit(); self.image_file.setPlaceholderText("Choose an existing target image, or use Pick on Screen")
            browse = QPushButton("Choose Image")
            browse.clicked.connect(self._choose_image)
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(self.image_file); row.addWidget(browse)
            box = QWidget(); box.setLayout(row)
            self.form.addRow("Target image", box)
        elif kind == ActionType.MOUSE_MOVE.value:
            self._pick_row("target", "Move to")
        elif kind == ActionType.DRAG.value:
            self._pick_row("start", "Drag from")
            self._pick_row("end", "Drag to")
        elif kind == ActionType.SCROLL.value:
            self.direction = QComboBox(); self.direction.addItems(["Down", "Up"])
            self.amount = QSpinBox(); self.amount.setRange(1, 9999); self.amount.setValue(3)
            self.direction.currentIndexChanged.connect(self._update_summary); self.amount.valueChanged.connect(self._update_summary)
            self.form.addRow("Direction", self.direction); self.form.addRow("Amount", self.amount)
        elif kind == ActionType.TYPE_TEXT.value:
            self.text = QPlainTextEdit(); self.text.setPlaceholderText("Enter the text to type")
            self.text.textChanged.connect(self._update_summary)
            add_var = QPushButton("Insert Variable")
            add_var.clicked.connect(self._insert_variable)
            self.form.addRow("Text", self.text); self.form.addRow("", add_var)
        elif kind == ActionType.WAIT.value:
            self.wait_ms = QSpinBox(); self.wait_ms.setRange(0, 3_600_000); self.wait_ms.setValue(1000); self.wait_ms.setSuffix(" ms")
            self.wait_ms.valueChanged.connect(self._update_summary); self.form.addRow("Wait", self.wait_ms)
        elif kind == ActionType.OPEN_FILE.value:
            self.path = QLineEdit(); browse = QPushButton("Browse") ; browse.clicked.connect(self._browse_file)
            row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(self.path); row.addWidget(browse)
            box = QWidget(); box.setLayout(row); self.form.addRow("Application or file", box)
        elif kind == ActionType.PRESS_KEY.value:
            self.key = QComboBox(); self.key.setEditable(True); self.key.addItems(["enter", "tab", "escape", "space", "backspace", "delete", "up", "down", "left", "right"])
            self.form.addRow("Key", self.key)
        elif kind == ActionType.HOTKEY.value:
            self.keys = QLineEdit("ctrl+s"); self.keys.setToolTip("Separate keys with +, for example Ctrl+Shift+S")
            self.form.addRow("Shortcut", self.keys)
        elif kind in {
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
            }[kind]
            self.condition_editor = ConditionEditor(fixed_type=fixed, variables=self.variables)
            self.condition_editor.changed.connect(self._update_summary)
            self.form.addRow(self.condition_editor)
        elif kind == ActionType.REPEAT_COUNT.value:
            self.repeat_count = QSpinBox(); self.repeat_count.setRange(0, 10000); self.repeat_count.setValue(3)
            self.repeat_count.setToolTip("Use 0 to skip the block")
            self.repeat_count.valueChanged.connect(self._update_summary)
            self.form.addRow("Number of times", self.repeat_count)
        elif kind == ActionType.REPEAT_UNTIL.value:
            self.condition_editor = ConditionEditor(variables=self.variables)
            self.condition_editor.changed.connect(self._update_summary)
            self.max_iterations = QSpinBox(); self.max_iterations.setRange(1, 10000); self.max_iterations.setValue(1000)
            self.iteration_delay = QDoubleSpinBox(); self.iteration_delay.setRange(0, 3600); self.iteration_delay.setDecimals(2)
            self.condition_editor.changed.connect(self._update_summary)
            self.form.addRow(self.condition_editor)
            self.form.addRow("Safety limit", self.max_iterations)
            self.form.addRow("Delay between loops", self.iteration_delay)
        elif kind in {ActionType.ELSE.value, ActionType.END_IF.value, ActionType.END_LOOP.value, ActionType.BREAK_LOOP.value}:
            note = QLabel({
                ActionType.ELSE.value: "Starts the alternative branch of the nearest If block.",
                ActionType.END_IF.value: "Closes the nearest If block.",
                ActionType.END_LOOP.value: "Closes the nearest Repeat block.",
                ActionType.BREAK_LOOP.value: "Leaves the nearest Repeat block immediately.",
            }[kind])
            note.setWordWrap(True); self.form.addRow(note)
        elif kind in WINDOW_ACTIONS:
            allow_selected = kind != ActionType.SELECT_WINDOW.value
            self.window_editor = WindowTargetEditor(allow_selected=allow_selected)
            self.window_editor.changed.connect(self._update_summary)
            self.window_editor.pick_requested.connect(lambda: self.screen_pick_requested.emit("window_target"))
            self.form.addRow(self.window_editor)
            if kind in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value}:
                self.relative_x = QSpinBox(); self.relative_y = QSpinBox()
                for field in (self.relative_x, self.relative_y):
                    field.setRange(-99999, 99999); field.valueChanged.connect(self._update_summary)
                point_row = QHBoxLayout(); point_row.setContentsMargins(0, 0, 0, 0)
                point_row.addWidget(QLabel("X from left")); point_row.addWidget(self.relative_x)
                point_row.addWidget(QLabel("Y from top")); point_row.addWidget(self.relative_y)
                point_wrap = QWidget(); point_wrap.setLayout(point_row)
                self.form.addRow("Position in window", point_wrap)
                self.scale_window = QCheckBox("Scale this position when the window is resized")
                self.scale_window.setChecked(True); self.scale_window.toggled.connect(self._update_summary)
                self.form.addRow("", self.scale_window)
                self.absolute_fallback = QCheckBox("Use the picked absolute position if the window cannot be used")
                self.absolute_fallback.setChecked(False)
                self.absolute_fallback.setToolTip("Off by default. Enable only when an absolute screen click is safe.")
                self.absolute_fallback.toggled.connect(self._update_summary)
                self.form.addRow("Fallback", self.absolute_fallback)
                self.original_window_size = (0, 0)
                self.absolute_point = (0, 0)
                if kind == ActionType.CLICK_WINDOW_RELATIVE.value:
                    self.window_button = QComboBox()
                    for label, value in (("Left", "left"), ("Right", "right"), ("Middle", "middle")):
                        self.window_button.addItem(label, value)
                    self.form.addRow("Mouse button", self.window_button)
                else:
                    self.window_move_duration = QDoubleSpinBox(); self.window_move_duration.setRange(0, 60)
                    self.window_move_duration.setDecimals(2); self.window_move_duration.setValue(0.2); self.window_move_duration.setSuffix(" s")
                    self.form.addRow("Move duration", self.window_move_duration)
        elif kind in {
            ActionType.SET_VARIABLE.value, ActionType.GET_VARIABLE.value,
            ActionType.INCREMENT_VARIABLE.value, ActionType.APPEND_VARIABLE.value,
            ActionType.SET_OBJECT_PROPERTY.value, ActionType.DELETE_VARIABLE.value,
        }:
            self.variable_name = QComboBox()
            self.variable_name.setEditable(True)
            self.variable_name.addItems(sorted(self.variables))
            self.variable_name.setToolTip("Choose a flow variable, runtime input, or prior output.")
            self.variable_name.currentTextChanged.connect(self._update_summary)
            self.form.addRow("Variable", self.variable_name)
            if kind in {ActionType.SET_VARIABLE.value, ActionType.APPEND_VARIABLE.value}:
                self.variable_value = QPlainTextEdit()
                self.variable_value.setMaximumHeight(100)
                self.variable_value.setPlaceholderText("A value, JSON, or {{another_variable}}")
                self.variable_value.textChanged.connect(self._update_summary)
                self.form.addRow("Value", self.variable_value)
            elif kind == ActionType.INCREMENT_VARIABLE.value:
                self.variable_amount = QDoubleSpinBox()
                self.variable_amount.setRange(-1_000_000_000, 1_000_000_000)
                self.variable_amount.setValue(1)
                self.form.addRow("Increase by", self.variable_amount)
            elif kind == ActionType.GET_VARIABLE.value:
                self.variable_output = QComboBox()
                self.variable_output.setEditable(True)
                self.variable_output.addItems(sorted(self.variables))
                self.variable_output.setToolTip("Optional: copy the value into another named variable.")
                self.form.addRow("Copy to", self.variable_output)
            elif kind == ActionType.SET_OBJECT_PROPERTY.value:
                self.variable_property = QLineEdit()
                self.variable_property.setPlaceholderText("approved or customer.address.city")
                self.variable_value = QPlainTextEdit()
                self.variable_value.setMaximumHeight(100)
                self.variable_value.setPlaceholderText("A value, JSON, or {{another_variable}}")
                self.form.addRow("Property", self.variable_property)
                self.form.addRow("Value", self.variable_value)
        elif kind == ActionType.RUN_SUBFLOW.value:
            self.subflow_editor = SubflowEditor(
                self.project_dir, list(self.variables), parent=self,
            )
            self.subflow_editor.changed.connect(self._update_summary)
            self.form.addRow("Saved flow", self.subflow_editor)
        elif kind in UTILITY_ACTIONS:
            self.utility_editor = UtilityActionEditor(
                kind, variables=list(self.variables), parent=self, guided=self._guided_mode,
            )
            self.utility_editor.changed.connect(self._update_summary)
            self.form.addRow(self.utility_editor)
        else:
            self.form.addRow(QLabel("This advanced step can be edited after insertion."))
        self._update_summary()

    def begin_picker(self, role: str) -> dict | None:
        """Freeze volatile widget state before the child picker starts."""
        if self._picker_active or not shiboken6.isValid(self):
            return None
        snapshot: dict = {"role": role, "action_type": self.type_box.currentData()}
        if role == "window_target":
            editor = getattr(self, "window_editor", None)
            if not isinstance(editor, WindowTargetEditor) or not shiboken6.isValid(editor):
                return None
            snapshot["window_data"] = editor.data()
            if hasattr(self, "scale_window") and shiboken6.isValid(self.scale_window):
                snapshot["scale_with_window"] = self.scale_window.isChecked()
            if hasattr(self, "absolute_fallback") and shiboken6.isValid(self.absolute_fallback):
                snapshot["use_absolute_fallback"] = self.absolute_fallback.isChecked()
        elif hasattr(self, "capture_image") and shiboken6.isValid(self.capture_image):
            snapshot["capture_image"] = self.capture_image.isChecked()
        self._picker_active = True
        self._picker_snapshot = snapshot
        self.type_box.setEnabled(False)
        self.confirm_button.setEnabled(False)
        self.test_match_button.setEnabled(False)
        self.test_step_button.setEnabled(False)
        return dict(snapshot)

    def finish_picker(self) -> None:
        if not shiboken6.isValid(self):
            return
        self._picker_active = False
        self._picker_snapshot = {}
        self.type_box.setEnabled(True)
        self._update_summary()

    def set_screen_point(self, role: str, x: int, y: int, image: str | None = None, offsets: tuple[int, int] | None = None) -> None:
        getattr(self, f"{role}_x").setValue(x); getattr(self, f"{role}_y").setValue(y)
        self.picked[role] = (x, y)
        if image and hasattr(self, "image_file"):
            self.image_file.setText(image); self.capture_image.setChecked(True)
        if offsets and role == "target":
            self.target_offsets = offsets
        self._update_summary()

    def set_window_target(self, target: dict, window_info: dict, point: tuple[int, int]) -> None:
        editor = getattr(self, "window_editor", None)
        if not isinstance(editor, WindowTargetEditor) or not shiboken6.isValid(editor):
            return
        editor.set_target(
            target,
            f"Captured {window_info.get('process_name') or 'window'} — {window_info.get('title') or 'untitled'}",
        )
        if hasattr(self, "relative_x"):
            x, y = point
            self.relative_x.setValue(x - int(window_info.get("left", 0)))
            self.relative_y.setValue(y - int(window_info.get("top", 0)))
            self.original_window_size = (
                int(window_info.get("width", 0)), int(window_info.get("height", 0)),
            )
            self.absolute_point = (x, y)
        self._update_summary()

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select target image", filter="Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.image_file.setText(path); self.capture_image.setChecked(True); self._update_summary()

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select application or file")
        if path: self.path.setText(path); self._update_summary()

    def _insert_variable(self) -> None:
        if not self.variables:
            QMessageBox.information(self, "Variables", "Add project variables first, then they will appear here.")
            return
        name, ok = QInputDialog.getItem(self, "Insert Variable", "Variable", sorted(self.variables), 0, False)
        if ok: self.text.insertPlainText("{{" + name + "}}")

    def _update_summary(self, *_args) -> None:
        if hasattr(self, "mode_note"):
            self.mode_note.setText(
                "Image matching with coordinate fallback" if self.capture_image.isChecked()
                else "Coordinates only"
            )
        try:
            action = self.action()
            self.summary.setText(self._plain_summary(action))
            error = self._validation_error()
            self._show_inline_validation(error)
            image_action = action.action in (
                ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value,
                ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value,
            )
            self.test_match_button.setVisible(image_action)
            self.test_step_button.setVisible(action.action not in CONTROL_TYPES | METADATA_TYPES)
            self.test_match_button.setEnabled(not error and not self._picker_active)
            self.test_step_button.setEnabled(not error and not self._picker_active)
            self.confirm_button.setEnabled(not self._picker_active)
        except Exception:
            self.summary.setText("Complete the fields above to configure this step.")
            self._show_inline_validation("Complete the visible fields for this step.")
            self.test_match_button.setVisible(False)
            self.test_step_button.setVisible(False)
            self.confirm_button.setEnabled(not self._picker_active)

    def _plain_summary(self, action: RpaAction) -> str:
        data = action.data
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            target = Path(str(data.get("image", "target image"))).name or "target image"
            timeout = float(data.get("timeout", self.settings.default_timeout) or self.settings.default_timeout)
            verb = "double-click it" if action.action == ActionType.DOUBLE_CLICK_IMAGE.value else "click it"
            return f"Wait up to {timeout:g} seconds for {target}, then {verb}."
        if action.action == ActionType.WAIT_WINDOW.value:
            window = data.get("window", {})
            target = window.get("window_title") or window.get("process_name") or "the selected window"
            timeout = float(window.get("timeout", data.get("timeout", 10)) or 10)
            return f"Wait up to {timeout:g} seconds for {target}."
        if action.action == ActionType.LAUNCH_APPLICATION.value:
            target = Path(str(data.get("path", "application"))).name or "the application"
            return f"Open {target}."
        if action.action == ActionType.RUN_SUBFLOW.value:
            return f"Run the saved flow {data.get('flow_name') or 'you select'}."
        text = action.summary()
        return text if text.endswith(".") else f"{text}."

    def _validation_error(self) -> str | None:
        action = self.action()
        data = action.data
        if action.action == ActionType.TYPE_TEXT.value and not str(data.get("text", "")).strip():
            return "Enter the text this step should type."
        if action.action == ActionType.OPEN_FILE.value and not str(data.get("path", "")).strip():
            return "Use Browse to select an application or file."
        if action.action == ActionType.PRESS_KEY.value and not str(data.get("key", "")).strip():
            return "Choose or enter a key to press."
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value) and not str(data.get("image", "")).strip():
            return "Use Pick on Screen or Choose Image to set the target image."
        if action.action in {ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value} and not str(data.get("image", "")).strip():
            return "Choose an image for this condition."
        if action.action == ActionType.IF_WINDOW_EXISTS.value and not str(data.get("window_title", "")).strip():
            return "Enter part of the window title."
        if action.action == ActionType.IF_PATH_EXISTS.value and not str(data.get("path", "")).strip():
            return "Choose or enter a file or folder path."
        if action.action == ActionType.IF_VARIABLE.value and not str(data.get("variable", "")).strip():
            return "Choose a variable to compare."
        if action.action == ActionType.REPEAT_UNTIL.value:
            condition_type = str(data.get("condition_type", "variable"))
            required_key = {"variable": "variable", "window_exists": "window_title", "path_exists": "path"}.get(condition_type, "image")
            if not str(data.get(required_key, "")).strip():
                return "Complete the Repeat Until condition."
        if action.action == ActionType.RUN_SUBFLOW.value and not str(data.get("project", "")).strip():
            return "Choose the saved flow to run."
        if action.action in {
            ActionType.SET_VARIABLE.value, ActionType.GET_VARIABLE.value,
            ActionType.INCREMENT_VARIABLE.value, ActionType.APPEND_VARIABLE.value,
            ActionType.SET_OBJECT_PROPERTY.value, ActionType.DELETE_VARIABLE.value,
        } and not str(data.get("variable", "")).strip():
            return "Choose or enter a variable name."
        if action.action == ActionType.SET_OBJECT_PROPERTY.value and not str(data.get("property", "")).strip():
            return "Enter the object property to update."
        required = {
            ActionType.LAUNCH_APPLICATION.value: ("path", "Choose an application to launch."),
            ActionType.WAIT_PROCESS.value: ("process_name", "Choose or enter a process name."),
            ActionType.ACTIVATE_PROCESS.value: ("process_name", "Choose or enter a process name."),
            ActionType.CLOSE_PROCESS.value: ("process_name", "Choose or enter a process name."),
            ActionType.READ_CLIPBOARD.value: ("output_variable", "Enter a variable for the clipboard text."),
            ActionType.COPY_PATH.value: ("source", "Choose the source file or folder."),
            ActionType.MOVE_PATH.value: ("source", "Choose the source file or folder."),
            ActionType.RENAME_PATH.value: ("source", "Choose the source file or folder."),
            ActionType.DELETE_PATH.value: ("path", "Choose the file or folder to delete."),
            ActionType.WAIT_PATH.value: ("path", "Enter the file or folder to wait for."),
            ActionType.RUN_POWERSHELL.value: ("command", "Enter a PowerShell command."),
            ActionType.RUN_PYTHON_SCRIPT.value: ("path", "Choose a Python script."),
            ActionType.SHOW_NOTIFICATION.value: ("message", "Enter the notification message."),
        }
        if action.action in required:
            field, message = required[action.action]
            if not str(data.get(field, "")).strip():
                return message
        if action.action in {ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value} and not str(data.get("destination", "")).strip():
            return "Choose the destination path."
        if action.action in WINDOW_ACTIONS:
            window = data.get("window", {})
            has_target = any(str(window.get(key, "")).strip() for key in ("process_name", "window_title", "class_name"))
            if not has_target and not data.get("use_selected_window"):
                return "Use Pick Window or enter a process, title, or class name."
            if action.action in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value}:
                if data.get("scale_with_window") and (
                    int(data.get("original_window_width", 0) or 0) <= 0
                    or int(data.get("original_window_height", 0) or 0) <= 0
                ):
                    return "Use Pick Window before enabling resize-aware positioning."
        return None

    def _confirm(self) -> None:
        self.diagnostic.emit("[Add Step] confirmation clicked")
        error = self._validation_error()
        if error:
            self.diagnostic.emit(f"[Add Step] validation failed: {error}")
            QMessageBox.warning(self, "Step needs more information", error)
            return
        self.diagnostic.emit("[Add Step] validation passed")
        self.diagnostic.emit("[Add Step] accept() called")
        QDialog.accept(self)

    def accept(self) -> None:
        """Keep Enter/default-button acceptance on the same validated path."""
        self._confirm()

    def reject(self) -> None:
        self.diagnostic.emit("[Add Step] reject() called")
        QDialog.reject(self)

    def action(self) -> RpaAction:
        kind = self.type_box.currentData()
        if kind in (ActionType.CLICK_COORDINATE.value, ActionType.DOUBLE_CLICK_IMAGE.value, "right_click", ActionType.CLICK_IMAGE.value):
            x, y = self.target_x.value(), self.target_y.value()
            button = "right" if kind == "right_click" else "left"
            image = self.image_file.text().strip()
            if self.capture_image.isChecked() and image:
                action = ActionType.DOUBLE_CLICK_IMAGE.value if kind == ActionType.DOUBLE_CLICK_IMAGE.value else ActionType.CLICK_IMAGE.value
                offset_x, offset_y = getattr(self, "target_offsets", (self.settings.crop_width // 2, self.settings.crop_height // 2))
                return RpaAction(action, {"image": image, "button": button, "fallback_x": x, "fallback_y": y, "click_offset_x": offset_x, "click_offset_y": offset_y, "confidence": self.settings.default_confidence, "timeout": self.settings.default_timeout, "use_coordinate_fallback": True})
            return RpaAction(ActionType.CLICK_COORDINATE.value, {"x": x, "y": y, "button": button})
        if kind == ActionType.MOUSE_MOVE.value:
            return RpaAction(kind, {"x": self.target_x.value(), "y": self.target_y.value(), "duration": 0.2})
        if kind == ActionType.DRAG.value:
            return RpaAction(kind, {"start_x": self.start_x.value(), "start_y": self.start_y.value(), "end_x": self.end_x.value(), "end_y": self.end_y.value(), "duration": 0.5, "button": "left"})
        if kind == ActionType.SCROLL.value:
            return RpaAction(kind, {"amount": self.amount.value() * (1 if self.direction.currentText() == "Up" else -1), "move_to": False})
        if kind == ActionType.TYPE_TEXT.value:
            return RpaAction(kind, {"text": self.text.toPlainText(), "interval": self.settings.typing_interval, "clear_first": False, "masked": False})
        if kind == ActionType.WAIT.value:
            return RpaAction(kind, {"seconds": self.wait_ms.value() / 1000})
        if kind == ActionType.OPEN_FILE.value:
            return RpaAction(kind, {"path": self.path.text().strip(), "wait_after": 1.0, "expected_window_title": ""})
        if kind == ActionType.PRESS_KEY.value:
            return RpaAction(kind, {"key": self.key.currentText().strip(), "count": 1, "interval": 0.0})
        if kind == ActionType.HOTKEY.value:
            return RpaAction(kind, {"keys": [part.strip().lower() for part in self.keys.text().split("+") if part.strip()]})
        if kind in {
            ActionType.SET_VARIABLE.value, ActionType.GET_VARIABLE.value,
            ActionType.INCREMENT_VARIABLE.value, ActionType.APPEND_VARIABLE.value,
            ActionType.SET_OBJECT_PROPERTY.value, ActionType.DELETE_VARIABLE.value,
        }:
            data = {"variable": self.variable_name.currentText().strip()}
            if kind in {ActionType.SET_VARIABLE.value, ActionType.APPEND_VARIABLE.value}:
                data["value"] = self._parse_variable_step_value(self.variable_value.toPlainText())
            elif kind == ActionType.INCREMENT_VARIABLE.value:
                amount = self.variable_amount.value()
                data["amount"] = int(amount) if amount.is_integer() else amount
            elif kind == ActionType.GET_VARIABLE.value:
                data["output_variable"] = self.variable_output.currentText().strip()
            elif kind == ActionType.SET_OBJECT_PROPERTY.value:
                data["property"] = self.variable_property.text().strip()
                data["value"] = self._parse_variable_step_value(self.variable_value.toPlainText())
            return RpaAction(kind, data)
        if kind in {
            ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value,
            ActionType.IF_WINDOW_EXISTS.value, ActionType.IF_PATH_EXISTS.value,
            ActionType.IF_VARIABLE.value,
        }:
            return RpaAction(kind, self.condition_editor.data())
        if kind == ActionType.REPEAT_COUNT.value:
            return RpaAction(kind, {"count": self.repeat_count.value()})
        if kind == ActionType.REPEAT_UNTIL.value:
            return RpaAction(kind, {
                **self.condition_editor.data(), "max_iterations": self.max_iterations.value(),
                "iteration_delay": self.iteration_delay.value(),
            })
        if kind in {ActionType.ELSE.value, ActionType.END_IF.value, ActionType.END_LOOP.value, ActionType.BREAK_LOOP.value}:
            return RpaAction(kind, {})
        if kind in WINDOW_ACTIONS:
            data = self.window_editor.data()
            if kind in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value}:
                width, height = getattr(self, "original_window_size", (0, 0))
                fallback_x, fallback_y = getattr(self, "absolute_point", (0, 0))
                data.update({
                    "relative_x": self.relative_x.value(), "relative_y": self.relative_y.value(),
                    "scale_with_window": self.scale_window.isChecked(),
                    "original_window_width": width, "original_window_height": height,
                    "use_absolute_fallback": self.absolute_fallback.isChecked(),
                    "fallback_x": fallback_x, "fallback_y": fallback_y,
                })
                if kind == ActionType.CLICK_WINDOW_RELATIVE.value:
                    data["button"] = self.window_button.currentData()
                else:
                    data["duration"] = self.window_move_duration.value()
            return RpaAction(kind, data)
        if kind == ActionType.RUN_SUBFLOW.value:
            return RpaAction(kind, self.subflow_editor.data())
        if kind in UTILITY_ACTIONS:
            return RpaAction(kind, self.utility_editor.data())
        defaults = {
            ActionType.WAIT.value: {"seconds": 1.0},
            ActionType.TYPE_TEXT.value: {"text": "", "interval": 0.02, "clear_first": False, "masked": False},
            ActionType.PRESS_KEY.value: {"key": "enter", "count": 1, "interval": 0.0},
            ActionType.HOTKEY.value: {"keys": ["ctrl", "s"]},
            ActionType.SCROLL.value: {"amount": -3, "x": 0, "y": 0, "move_to": False},
            ActionType.OPEN_FILE.value: {"path": "", "wait_after": 1.0, "expected_window_title": ""},
            ActionType.RUN_PYTHON.value: {"code": "result = variables.get('quantity', 0)", "output_variable": "result"},
            ActionType.PYTHON_CODE.value: {"name": "Python Code", "code": "variables['result'] = 1", "continue_on_error": False},
            ActionType.CLICK_COORDINATE.value: {"x": 0, "y": 0, "button": "left"},
        }
        return RpaAction(kind, defaults[kind])

    @staticmethod
    def _parse_variable_step_value(text: str):
        value = text.strip()
        if not value:
            return ""
        if value.startswith("{{") and value.endswith("}}"):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text


class RuntimeInputEditorDialog(QDialog):
    def __init__(self, name: str = "", definition: RuntimeInputDefinition | None = None, parent=None) -> None:
        super().__init__(parent)
        definition = definition or RuntimeInputDefinition()
        self.setWindowTitle("Runtime Input")
        self.name_edit = QLineEdit(name)
        self.type_combo = QComboBox()
        for kind in INPUT_TYPES:
            self.type_combo.addItem(kind.replace("_", " ").title(), kind)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(definition.type)))
        self.default_edit = QLineEdit(str(definition.default or ""))
        if definition.sensitive or definition.type == "password":
            self.default_edit.setEchoMode(QLineEdit.Password)
        self.required_check = QCheckBox("Required")
        self.required_check.setChecked(definition.required)
        self.sensitive_check = QCheckBox("Sensitive (mask in logs and reports)")
        self.sensitive_check.setChecked(definition.sensitive)
        self.type_combo.currentIndexChanged.connect(lambda _index: self._update_default_mask())
        self.sensitive_check.toggled.connect(lambda _checked: self._update_default_mask())
        self.options_edit = QLineEdit(", ".join(definition.options))
        self.options_edit.setPlaceholderText("For dropdowns: option one, option two")
        self.description_edit = QLineEdit(definition.description)
        form = QFormLayout(self)
        form.addRow("Variable name", self.name_edit)
        form.addRow("Input type", self.type_combo)
        form.addRow("Default value", self.default_edit)
        form.addRow("Choices", self.options_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("", self.required_check)
        form.addRow("", self.sensitive_check)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

    def _update_default_mask(self) -> None:
        masked = self.sensitive_check.isChecked() or self.type_combo.currentData() == "password"
        self.default_edit.setEchoMode(QLineEdit.Password if masked else QLineEdit.Normal)

    def _accept_if_valid(self) -> None:
        if not VARIABLE_NAME_PATTERN.fullmatch(self.name_edit.text().strip()):
            QMessageBox.warning(self, "Invalid Name", "Use letters, numbers, and underscores, starting with a letter or underscore.")
            return
        if self.type_combo.currentData() == "dropdown" and not self.options():
            QMessageBox.warning(self, "Dropdown Choices", "Add at least one dropdown choice.")
            return
        self.accept()

    def options(self) -> list[str]:
        return [item.strip() for item in self.options_edit.text().split(",") if item.strip()]

    def result_value(self) -> tuple[str, RuntimeInputDefinition]:
        return self.name_edit.text().strip(), RuntimeInputDefinition(
            type=str(self.type_combo.currentData()), default=self.default_edit.text(),
            required=self.required_check.isChecked(), sensitive=self.sensitive_check.isChecked(),
            options=self.options(), description=self.description_edit.text().strip(),
        )


class VariableEditorDialog(QDialog):
    """Edit one typed flow variable without exposing Python syntax."""

    def __init__(self, name: str = "", definition: VariableDefinition | None = None, parent=None) -> None:
        super().__init__(parent)
        definition = definition or VariableDefinition()
        self.setWindowTitle("Flow Variable")
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("Example: order_quantity")
        self.type_combo = QComboBox()
        labels = {
            "text": "Text", "integer": "Integer", "decimal": "Decimal",
            "boolean": "Boolean", "list": "List", "object": "Object / JSON",
            "null": "Null", "secret_text": "Secret Text",
        }
        for kind in VARIABLE_TYPES:
            self.type_combo.addItem(labels[kind], kind)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(definition.type)))
        self.value_edit = QLineEdit()
        self.json_edit = QPlainTextEdit()
        self.json_edit.setMinimumHeight(130)
        self.json_edit.setPlaceholderText('["item"] or {"key": "value"}')
        self.value_stack = QStackedWidget()
        self.value_stack.addWidget(self.value_edit)
        self.value_stack.addWidget(self.json_edit)
        if definition.type in {"list", "object"}:
            self.json_edit.setPlainText(json.dumps(definition.default, indent=2, ensure_ascii=False))
        elif definition.default is not None:
            if definition.type == "boolean":
                self.value_edit.setText("true" if bool(definition.default) else "false")
            else:
                self.value_edit.setText(str(definition.default))
        self.description_edit = QLineEdit(definition.description)
        self.secret_check = QCheckBox("Secret (mask this value in the UI and logs)")
        self.secret_check.setChecked(definition.secret or definition.type == "secret_text")
        note = QLabel("Secret values are masked, but project.json is local storage—not encrypted secure storage.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Name", self.name_edit)
        form.addRow("Type", self.type_combo)
        form.addRow("Default Value", self.value_stack)
        form.addRow("Description", self.description_edit)
        form.addRow("", self.secret_check)
        form.addRow(note)
        form.addWidget(buttons)
        self.type_combo.currentIndexChanged.connect(self._update_value_editor)
        self.secret_check.toggled.connect(self._update_value_editor)
        self._update_value_editor()

    def _update_value_editor(self) -> None:
        kind = str(self.type_combo.currentData())
        is_json = kind in {"list", "object"}
        self.value_stack.setCurrentIndex(1 if is_json else 0)
        self.value_stack.setEnabled(kind != "null")
        secret = self.secret_check.isChecked() or kind == "secret_text"
        self.value_edit.setEchoMode(QLineEdit.Password if secret else QLineEdit.Normal)
        if kind == "secret_text":
            self.secret_check.setChecked(True)
            self.secret_check.setEnabled(False)
        else:
            self.secret_check.setEnabled(True)

    def _raw_value(self):
        return self.json_edit.toPlainText() if self.type_combo.currentData() in {"list", "object"} else self.value_edit.text()

    def _accept_if_valid(self) -> None:
        name = self.name_edit.text().strip()
        if not VARIABLE_NAME_PATTERN.fullmatch(name):
            QMessageBox.warning(self, "Invalid Variable Name", "Use letters, numbers, and underscores, starting with a letter or underscore.")
            return
        _value, error = coerce_variable_value(name, str(self.type_combo.currentData()), self._raw_value())
        if error:
            QMessageBox.warning(self, "Invalid Default Value", error)
            return
        self.accept()

    def result_value(self) -> tuple[str, VariableDefinition]:
        name = self.name_edit.text().strip()
        kind = str(self.type_combo.currentData())
        value, error = coerce_variable_value(name, kind, self._raw_value())
        if error:  # guarded by _accept_if_valid; useful for direct callers/tests
            raise ValueError(error)
        return name, VariableDefinition(
            type=kind, default=value, description=self.description_edit.text().strip(),
            secret=self.secret_check.isChecked() or kind == "secret_text",
        )


class VariablesDialog(QDialog):
    def __init__(
        self, project_or_variables: RpaProject | dict[str, str], current_values: dict | None = None, parent=None,
    ) -> None:
        if isinstance(current_values, QWidget) and parent is None:
            parent = current_values
            current_values = None
        super().__init__(parent)
        self.setWindowTitle("Flow Variables")
        self.resize(980, 600)
        self.project = project_or_variables if isinstance(project_or_variables, RpaProject) else None
        self.variables = dict(self.project.variables if self.project else project_or_variables)
        self.variable_definitions = dict(self.project.variable_definitions if self.project else {})
        for name, value in self.variables.items():
            self.variable_definitions.setdefault(name, VariableDefinition.from_dict(value))
        self.runtime_inputs = dict(self.project.runtime_inputs if self.project else {})
        self.output_variables = list(self.project.output_variables if self.project else [])
        self.current_values = dict(
            self.project.persisted_variable_values
            if self.project and self.project.settings.persist_variable_values else {}
        )
        self.current_values.update(dict(current_values or {}))
        self.list = QTableWidget(0, 6)
        self.list.setHorizontalHeaderLabels([
            "Name", "Type", "Default Value", "Current Runtime Value", "Description", "Secret",
        ])
        self.list.setSelectionBehavior(QTableWidget.SelectRows)
        self.list.setSelectionMode(QTableWidget.SingleSelection)
        self.list.setAlternatingRowColors(True)
        self.list.doubleClicked.connect(self._edit)
        self._refresh()
        add = QPushButton("Add Variable")
        edit = QPushButton("Edit")
        delete = QPushButton("Delete")
        duplicate = QPushButton("Duplicate")
        import_json = QPushButton("Import JSON")
        export_json = QPushButton("Export JSON")
        reset_runtime = QPushButton("Reset Runtime Values")
        add.clicked.connect(self._add)
        edit.clicked.connect(self._edit)
        delete.clicked.connect(self._delete)
        duplicate.clicked.connect(self._duplicate)
        import_json.clicked.connect(self._import_json)
        export_json.clicked.connect(self._export_json)
        reset_runtime.clicked.connect(self._reset_runtime_values)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(edit)
        row.addWidget(delete)
        row.addWidget(duplicate)
        row.addStretch(1)
        row.addWidget(import_json)
        row.addWidget(export_json)
        row.addWidget(reset_runtime)
        project_tab = QWidget()
        project_layout = QVBoxLayout(project_tab)
        project_layout.addWidget(QLabel("Typed shared values saved with this flow and available to every step during a run."))
        project_layout.addWidget(self.list)
        project_layout.addLayout(row)
        self.persist_values = QCheckBox("Persist variable values between runs")
        self.persist_values.setChecked(bool(self.project and self.project.settings.persist_variable_values))
        self.persist_values.setToolTip("Off by default. When off, every run starts from a deep copy of the defaults.")
        project_layout.addWidget(self.persist_values)

        runtime_tab = QWidget()
        runtime_layout = QVBoxLayout(runtime_tab)
        runtime_layout.addWidget(QLabel("Requested before manual runs; schedules can provide their own values."))
        self.runtime_table = QTableWidget(0, 5)
        self.runtime_table.setHorizontalHeaderLabels(["Name", "Type", "Default", "Required", "Sensitive"])
        runtime_layout.addWidget(self.runtime_table)
        runtime_buttons = QHBoxLayout()
        add_runtime = QPushButton("Add Input")
        edit_runtime = QPushButton("Edit")
        remove_runtime = QPushButton("Remove")
        add_runtime.clicked.connect(self._add_runtime)
        edit_runtime.clicked.connect(self._edit_runtime)
        remove_runtime.clicked.connect(self._remove_runtime)
        for button in (add_runtime, edit_runtime, remove_runtime):
            runtime_buttons.addWidget(button)
        runtime_buttons.addStretch(1)
        runtime_layout.addLayout(runtime_buttons)

        output_tab = QWidget()
        output_layout = QVBoxLayout(output_tab)
        output_layout.addWidget(QLabel("Values produced by earlier steps. Add names here for documentation and debugging."))
        self.output_list = QListWidget()
        output_layout.addWidget(self.output_list)
        output_buttons = QHBoxLayout()
        add_output = QPushButton("Add Output")
        remove_output = QPushButton("Remove")
        add_output.clicked.connect(self._add_output)
        remove_output.clicked.connect(self._remove_output)
        output_buttons.addWidget(add_output)
        output_buttons.addWidget(remove_output)
        output_buttons.addStretch(1)
        output_layout.addLayout(output_buttons)

        current_tab = QWidget()
        current_layout = QVBoxLayout(current_tab)
        current_layout.addWidget(QLabel("Current values from the latest or active debug run. Sensitive values stay masked."))
        self.current_table = QTableWidget(0, 3)
        self.current_table.setHorizontalHeaderLabels(["Variable", "Category", "Value"])
        current_layout.addWidget(self.current_table)

        tabs = QTabWidget()
        tabs.addTab(project_tab, "Project Variables")
        tabs.addTab(runtime_tab, "Runtime Inputs")
        tabs.addTab(output_tab, "Output Variables")
        tabs.addTab(current_tab, "Current Values")
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self._refresh_categories()
        buttons.accepted.disconnect()
        buttons.accepted.connect(self._save_and_accept)

    def _refresh(self) -> None:
        selected = self._selected_key()
        rows = sorted(self.variable_definitions.items())
        self.list.setRowCount(len(rows))
        for row, (name, definition) in enumerate(rows):
            value = self.variables.get(name, definition.default)
            current = self.current_values.get(name, value)
            secret = definition.secret or definition.type == "secret_text"
            values = (
                name, definition.type.replace("_", " ").title(),
                "********" if secret and value not in (None, "") else self._display_value(value),
                "********" if secret and current not in (None, "") else self._display_value(current),
                definition.description, "Yes" if secret else "No",
            )
            for column, display in enumerate(values):
                item = QTableWidgetItem(display)
                item.setToolTip(display)
                self.list.setItem(row, column, item)
            if name == selected:
                self.list.selectRow(row)
        self.list.resizeColumnsToContents()
        self.list.horizontalHeader().setStretchLastSection(True)

    @staticmethod
    def _display_value(value) -> str:
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return "null"
        return str(value)

    def _selected_key(self) -> str | None:
        item = self.list.item(self.list.currentRow(), 0)
        return item.text() if item else None

    def _add(self) -> None:
        dialog = VariableEditorDialog(parent=self)
        if dialog.exec() != QDialog.Accepted:
            return
        name, definition = dialog.result_value()
        if self._name_exists(name):
            QMessageBox.warning(self, "Duplicate Variable", f"A variable named '{name}' already exists.")
            return
        self.variable_definitions[name] = definition
        self.variables[name] = definition.default
        self._refresh()

    def _edit(self, *_args) -> None:
        key = self._selected_key()
        if not key:
            return
        dialog = VariableEditorDialog(key, self.variable_definitions[key], self)
        if dialog.exec() != QDialog.Accepted:
            return
        name, definition = dialog.result_value()
        if name != key and self._name_exists(name):
            QMessageBox.warning(self, "Duplicate Variable", f"A variable named '{name}' already exists.")
            return
        self.variable_definitions.pop(key, None)
        self.variables.pop(key, None)
        self.variable_definitions[name] = definition
        self.variables[name] = definition.default
        self._refresh()

    def _delete(self) -> None:
        key = self._selected_key()
        if key:
            self.variables.pop(key, None)
            self.variable_definitions.pop(key, None)
            self.current_values.pop(key, None)
            self._refresh()

    def _name_exists(self, name: str) -> bool:
        return name in self.variable_definitions or name in self.runtime_inputs or name in self.output_variables

    def _duplicate(self) -> None:
        key = self._selected_key()
        if not key:
            return
        base = f"{key}_copy"
        name = base
        number = 2
        while self._name_exists(name):
            name = f"{base}_{number}"
            number += 1
        original = self.variable_definitions[key]
        definition = VariableDefinition(
            type=original.type, default=json.loads(json.dumps(original.default)),
            description=original.description, secret=original.secret,
        )
        self.variable_definitions[name] = definition
        self.variables[name] = definition.default
        self._refresh()

    def _import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Flow Variables", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            raw = payload.get("definitions", payload) if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                raise ValueError("The JSON root must be an object of variable names.")
            imported: list[tuple[str, VariableDefinition]] = []
            for name, item in raw.items():
                if not VARIABLE_NAME_PATTERN.fullmatch(str(name)):
                    raise ValueError(f"Invalid variable name: {name}")
                if self._name_exists(str(name)):
                    raise ValueError(f"Variable already exists: {name}")
                definition = VariableDefinition.from_dict(item)
                value, error = coerce_variable_value(str(name), definition.type, definition.default)
                if error:
                    raise ValueError(error)
                definition.default = value
                imported.append((str(name), definition))
            for name, definition in imported:
                self.variable_definitions[name] = definition
                self.variables[name] = definition.default
            self._refresh()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Import Variables Failed", str(exc))

    def _export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Flow Variables", "variables.json", "JSON Files (*.json)")
        if not path:
            return
        try:
            payload = {"definitions": {
                name: {
                    "type": definition.type, "default": definition.default,
                    "description": definition.description, "secret": definition.secret,
                }
                for name, definition in sorted(self.variable_definitions.items())
            }}
            Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Export Variables Failed", str(exc))

    def _reset_runtime_values(self) -> None:
        self.current_values = {name: definition.default for name, definition in self.variable_definitions.items()}
        if self.project:
            self.project.persisted_variable_values.clear()
        self._refresh()
        self._refresh_categories()

    def _refresh_categories(self) -> None:
        self.runtime_table.setRowCount(len(self.runtime_inputs))
        for row, (name, definition) in enumerate(sorted(self.runtime_inputs.items())):
            values = (name, definition.type, str(definition.default or ""), "Yes" if definition.required else "No", "Yes" if definition.sensitive else "No")
            for column, value in enumerate(values):
                display = "[REDACTED]" if column == 2 and definition.sensitive and value else value
                self.runtime_table.setItem(row, column, QTableWidgetItem(display))
        self.output_list.clear()
        self.output_list.addItems(sorted(self.output_variables))
        sensitive = {name for name, definition in self.runtime_inputs.items() if definition.sensitive or definition.type == "password"}
        sensitive.update(
            name for name, definition in self.variable_definitions.items()
            if definition.secret or definition.type == "secret_text"
        )
        rows = []
        for name, value in sorted(self.current_values.items()):
            if name in self.variables:
                category = "Project"
            elif name in self.runtime_inputs:
                category = "Runtime Input"
            else:
                category = "Output / Built-in"
            rows.append((name, category, "[REDACTED]" if name in sensitive else str(value)))
        self.current_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.current_table.setItem(row, column, QTableWidgetItem(value))

    def _selected_runtime_name(self) -> str | None:
        item = self.runtime_table.item(self.runtime_table.currentRow(), 0)
        return item.text() if item else None

    def _add_runtime(self) -> None:
        dialog = RuntimeInputEditorDialog(parent=self)
        if dialog.exec() == QDialog.Accepted:
            name, definition = dialog.result_value()
            if name in self.variables or name in self.runtime_inputs:
                QMessageBox.warning(self, "Duplicate Variable", f"{name} already exists.")
                return
            self.runtime_inputs[name] = definition
            self._refresh_categories()

    def _edit_runtime(self) -> None:
        name = self._selected_runtime_name()
        if not name:
            return
        dialog = RuntimeInputEditorDialog(name, self.runtime_inputs[name], self)
        if dialog.exec() == QDialog.Accepted:
            new_name, definition = dialog.result_value()
            if new_name != name and (new_name in self.variables or new_name in self.runtime_inputs):
                QMessageBox.warning(self, "Duplicate Variable", f"{new_name} already exists.")
                return
            self.runtime_inputs.pop(name)
            self.runtime_inputs[new_name] = definition
            self._refresh_categories()

    def _remove_runtime(self) -> None:
        name = self._selected_runtime_name()
        if name:
            self.runtime_inputs.pop(name, None)
            self._refresh_categories()

    def _add_output(self) -> None:
        name, ok = QInputDialog.getText(self, "Output Variable", "Name")
        name = name.strip()
        if ok and VARIABLE_NAME_PATTERN.fullmatch(name) and name not in self.output_variables:
            self.output_variables.append(name)
            self._refresh_categories()
        elif ok:
            QMessageBox.warning(self, "Invalid Name", "Enter a unique variable name using letters, numbers, and underscores.")

    def _remove_output(self) -> None:
        item = self.output_list.currentItem()
        if item:
            self.output_variables.remove(item.text())
            self._refresh_categories()

    def _save_and_accept(self) -> None:
        if self.project:
            candidate = RpaProject(
                project=self.project.project, settings=self.project.settings, variables=self.variables,
                variable_definitions=self.variable_definitions,
                persisted_variable_values=self.project.persisted_variable_values,
                runtime_inputs=self.runtime_inputs, output_variables=self.output_variables,
                actions=self.project.actions,
            )
            errors = validate_variable_configuration(candidate)
            if errors:
                QMessageBox.warning(self, "Check Variables", "\n".join(f"• {error}" for error in errors))
                return
            self.project.settings.persist_variable_values = self.persist_values.isChecked()
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, settings: ProjectSettings, parent=None, project: RpaProject | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Flow Settings")
        self.settings = settings
        self.project = project
        self.timing_mode = QComboBox()
        self.timing_mode.addItems(["recorded", "none"])
        self.timing_mode.setCurrentText(settings.timing_mode)
        self.crop_width = self._spin(settings.crop_width, 40, 2000)
        self.crop_height = self._spin(settings.crop_height, 40, 2000)
        self.confidence = self._double(settings.default_confidence, 0, 1)
        self.timeout = self._double(settings.default_timeout, 0, 999)
        self.text_flush = self._double(settings.text_flush_timeout, 0.1, 10)
        self.double_click = self._double(settings.double_click_interval, 0.05, 5)
        self.coordinate_fallback = QCheckBox()
        self.coordinate_fallback.setChecked(settings.coordinate_fallback)
        self.typing_interval = self._double(settings.typing_interval, 0, 10)
        self.start_delay = self._double(settings.start_delay, 0, 60)
        self.pre_click_pause = self._double(settings.pre_click_pause, 0, 5)
        self.ignore_app = QCheckBox()
        self.ignore_app.setChecked(settings.ignore_application_window)
        self.failsafe = QCheckBox()
        self.failsafe.setChecked(settings.pyautogui_failsafe)
        self.show_desktop = QCheckBox()
        self.show_desktop.setChecked(settings.show_desktop_before_recording)
        self.show_desktop.setToolTip("Minimize open windows before capture begins. Windows are not restored afterward.")
        self.hide_during_replay = QCheckBox()
        self.hide_during_replay.setChecked(settings.hide_window_during_replay)
        self.hide_during_replay.setToolTip("Keeps the recorder out of the way while a floating Stop Run control remains available.")
        self.evidence_retention = self._spin(settings.evidence_retention_runs, 10, 1000)
        self.evidence_retention.setToolTip("Maximum timestamped run-evidence folders retained for this flow.")
        self.completion_enabled = QCheckBox("Verify explicit completion criteria")
        self.completion_enabled.setChecked(bool(project and project.success_when))
        self.completion_mode = QComboBox()
        self.completion_mode.addItem("All conditions must pass", "all")
        self.completion_mode.addItem("Any condition may pass", "any")
        if project and project.success_when:
            self.completion_mode.setCurrentIndex(max(0, self.completion_mode.findData(project.success_when.get("mode", "all"))))
        self.completion_conditions = QPlainTextEdit()
        self.completion_conditions.setMinimumHeight(110)
        self.completion_conditions.setPlaceholderText(
            '[{"type": "file_exists", "value": "${output_file}"}]'
        )
        conditions = project.success_when.get("conditions", []) if project and project.success_when else []
        self.completion_conditions.setPlainText(json.dumps(conditions, indent=2, ensure_ascii=False))
        completion_note = QLabel(
            "Optional. Uses the same condition types as Expected Result. Without criteria, completed runs are marked COMPLETED_UNVERIFIED."
        )
        completion_note.setWordWrap(True)
        completion_note.setStyleSheet("color: #64748b;")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        layout.addRow("Timing mode", self.timing_mode)
        layout.addRow("Screenshot crop width", self.crop_width)
        layout.addRow("Screenshot crop height", self.crop_height)
        layout.addRow("Default confidence", self.confidence)
        layout.addRow("Default image timeout", self.timeout)
        layout.addRow("Text flush timeout", self.text_flush)
        layout.addRow("Double-click interval", self.double_click)
        layout.addRow("Coordinate fallback", self.coordinate_fallback)
        layout.addRow("Typing interval", self.typing_interval)
        layout.addRow("Start delay", self.start_delay)
        layout.addRow("Pre-click pause", self.pre_click_pause)
        layout.addRow("Ignore application window", self.ignore_app)
        layout.addRow("Show desktop before recording", self.show_desktop)
        layout.addRow("Hide recorder while running", self.hide_during_replay)
        layout.addRow("Run evidence retention", self.evidence_retention)
        layout.addRow("PyAutoGUI failsafe", self.failsafe)
        layout.addRow(QLabel("Completion Criteria"))
        layout.addRow("", self.completion_enabled)
        layout.addRow("Mode", self.completion_mode)
        layout.addRow("Conditions (JSON)", self.completion_conditions)
        layout.addRow(completion_note)
        layout.addWidget(buttons)

    def _spin(self, value, minimum, maximum):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(int(value))
        return widget

    def _double(self, value, minimum, maximum):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(3)
        widget.setValue(float(value))
        return widget

    def accept(self) -> None:
        completion: dict | None = None
        if self.completion_enabled.isChecked():
            try:
                conditions = json.loads(self.completion_conditions.toPlainText() or "[]")
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, "Invalid Completion Criteria", f"Conditions are not valid JSON: {exc}")
                return
            if not isinstance(conditions, list) or not conditions or any(not isinstance(item, dict) for item in conditions):
                QMessageBox.warning(self, "Invalid Completion Criteria", "Add one or more JSON condition objects.")
                return
            completion = {"mode": str(self.completion_mode.currentData()), "conditions": conditions}
        self.settings.timing_mode = self.timing_mode.currentText()
        self.settings.crop_width = self.crop_width.value()
        self.settings.crop_height = self.crop_height.value()
        self.settings.default_confidence = self.confidence.value()
        self.settings.default_timeout = self.timeout.value()
        self.settings.text_flush_timeout = self.text_flush.value()
        self.settings.double_click_interval = self.double_click.value()
        self.settings.coordinate_fallback = self.coordinate_fallback.isChecked()
        self.settings.typing_interval = self.typing_interval.value()
        self.settings.start_delay = self.start_delay.value()
        self.settings.pre_click_pause = self.pre_click_pause.value()
        self.settings.ignore_application_window = self.ignore_app.isChecked()
        self.settings.show_desktop_before_recording = self.show_desktop.isChecked()
        self.settings.hide_window_during_replay = self.hide_during_replay.isChecked()
        self.settings.evidence_retention_runs = self.evidence_retention.value()
        self.settings.pyautogui_failsafe = self.failsafe.isChecked()
        if self.project is not None:
            self.project.success_when = completion
        qsettings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        for key, value in self.settings.__dict__.items():
            qsettings.setValue(key, value)
        super().accept()


def show_error(parent, title: str, message: str) -> None:
    QMessageBox.critical(parent, title, message)
