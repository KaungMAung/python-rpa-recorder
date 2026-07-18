from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QCheckBox,
    QVBoxLayout,
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
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Step")
        self.type_box = QComboBox()
        for label, value in [
            ("Wait", ActionType.WAIT.value),
            ("Type Text", ActionType.TYPE_TEXT.value),
            ("Press Key", ActionType.PRESS_KEY.value),
            ("Hotkey", ActionType.HOTKEY.value),
            ("Scroll", ActionType.SCROLL.value),
            ("Open File", ActionType.OPEN_FILE.value),
            ("Run Python", ActionType.RUN_PYTHON.value),
            ("Python Code", ActionType.PYTHON_CODE.value),
            ("Click Coordinate", ActionType.CLICK_COORDINATE.value),
        ]:
            self.type_box.addItem(label, value)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        layout.addRow("What should this step do?", self.type_box)
        layout.addWidget(buttons)

    def action(self) -> RpaAction:
        kind = self.type_box.currentData()
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
        self.settings.pyautogui_failsafe = self.failsafe.isChecked()
        qsettings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        for key, value in self.settings.__dict__.items():
            qsettings.setValue(key, value)
        super().accept()


def show_error(parent, title: str, message: str) -> None:
    QMessageBox.critical(parent, title, message)
