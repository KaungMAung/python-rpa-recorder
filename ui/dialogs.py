from __future__ import annotations

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QFileDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QCheckBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from rpa.models import ActionType, ProjectSettings, RpaAction


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

    def __init__(self, settings: ProjectSettings, variables: dict[str, str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Step")
        self.settings = settings
        self.variables = variables
        self.picked: dict[str, tuple[int, int]] = {}
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
            ("Run Python", ActionType.RUN_PYTHON.value),
            ("Python Code", ActionType.PYTHON_CODE.value),
        ]:
            self.type_box.addItem(label, value)
        self.form = QFormLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background: #f1f5f9; color: #334155; padding: 8px; border: 1px solid #d8dee8;")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        top = QFormLayout()
        top.addRow("What should this step do?", self.type_box)
        layout.addLayout(top)
        layout.addLayout(self.form)
        layout.addWidget(QLabel("Step summary"))
        layout.addWidget(self.summary)
        layout.addWidget(buttons)
        self.type_box.currentIndexChanged.connect(self._rebuild)
        self._rebuild()

    def _clear_form(self) -> None:
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()

    def _pick_row(self, role: str, label: str) -> tuple[QSpinBox, QSpinBox]:
        x, y = QSpinBox(), QSpinBox()
        for field in (x, y):
            field.setRange(-99999, 99999)
            field.valueChanged.connect(self._update_summary)
        button = QPushButton("Pick on Screen")
        button.setToolTip("Hide this dialog and select a position. Esc or right-click cancels without changing this step.")
        button.clicked.connect(lambda: self.screen_pick_requested.emit(role))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("X")); row.addWidget(x); row.addWidget(QLabel("Y")); row.addWidget(y); row.addWidget(button)
        box = QVBoxLayout(); box.addLayout(row)
        wrapper = QWidget(); wrapper.setLayout(box)
        self.form.addRow(label, wrapper)
        setattr(self, f"{role}_x", x); setattr(self, f"{role}_y", y)
        return x, y

    def _rebuild(self) -> None:
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
        else:
            self.form.addRow(QLabel("This advanced step can be edited after insertion."))
        self._update_summary()

    def set_screen_point(self, role: str, x: int, y: int, image: str | None = None, offsets: tuple[int, int] | None = None) -> None:
        getattr(self, f"{role}_x").setValue(x); getattr(self, f"{role}_y").setValue(y)
        self.picked[role] = (x, y)
        if image and hasattr(self, "image_file"):
            self.image_file.setText(image); self.capture_image.setChecked(True)
        if offsets and role == "target":
            self.target_offsets = offsets
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
        try: self.summary.setText(self.action().summary())
        except Exception: self.summary.setText("Complete the fields above to configure this step.")

    def accept(self) -> None:
        action = self.action()
        data = action.data
        if action.action == ActionType.TYPE_TEXT.value and not str(data.get("text", "")).strip():
            QMessageBox.warning(self, "Text is required", "Enter the text this step should type.")
            return
        if action.action == ActionType.OPEN_FILE.value and not str(data.get("path", "")).strip():
            QMessageBox.warning(self, "Application or file is required", "Use Browse to select an application or file.")
            return
        if action.action == ActionType.PRESS_KEY.value and not str(data.get("key", "")).strip():
            QMessageBox.warning(self, "Key is required", "Choose or enter a key to press.")
            return
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value) and not str(data.get("image", "")).strip():
            QMessageBox.warning(self, "Target image is required", "Use Pick on Screen or Choose Image to set the target image.")
            return
        super().accept()

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


class VariablesDialog(QDialog):
    def __init__(self, variables: dict[str, str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Variables")
        self.variables = dict(variables)
        self.list = QListWidget()
        self._refresh()
        add = QPushButton("Add")
        edit = QPushButton("Edit")
        delete = QPushButton("Delete")
        add.clicked.connect(self._add)
        edit.clicked.connect(self._edit)
        delete.clicked.connect(self._delete)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(edit)
        row.addWidget(delete)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _refresh(self) -> None:
        self.list.clear()
        for key, value in sorted(self.variables.items()):
            self.list.addItem(f"{key} = {value}")

    def _selected_key(self) -> str | None:
        item = self.list.currentItem()
        if not item:
            return None
        return item.text().split(" = ", 1)[0]

    def _add(self) -> None:
        key, ok = QInputDialog.getText(self, "Variable", "Name")
        if not ok or not key:
            return
        value, ok = QInputDialog.getText(self, "Variable", "Value")
        if ok:
            self.variables[key] = value
            self._refresh()

    def _edit(self) -> None:
        key = self._selected_key()
        if not key:
            return
        value, ok = QInputDialog.getText(self, "Variable", "Value", text=self.variables.get(key, ""))
        if ok:
            self.variables[key] = value
            self._refresh()

    def _delete(self) -> None:
        key = self._selected_key()
        if key:
            self.variables.pop(key, None)
            self._refresh()


class SettingsDialog(QDialog):
    def __init__(self, settings: ProjectSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
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
        layout.addRow("PyAutoGUI failsafe", self.failsafe)
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
        self.settings.pyautogui_failsafe = self.failsafe.isChecked()
        qsettings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        for key, value in self.settings.__dict__.items():
            qsettings.setValue(key, value)
        super().accept()


def show_error(parent, title: str, message: str) -> None:
    QMessageBox.critical(parent, title, message)
