from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QWidget,
)

from rpa.models import ActionType
from rpa.native_utilities import enumerate_processes


UTILITY_ACTIONS = {
    ActionType.LAUNCH_APPLICATION.value, ActionType.WAIT_PROCESS.value,
    ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value,
    ActionType.READ_CLIPBOARD.value, ActionType.WRITE_CLIPBOARD.value,
    ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value,
    ActionType.DELETE_PATH.value, ActionType.WAIT_PATH.value,
    ActionType.RUN_POWERSHELL.value, ActionType.RUN_PYTHON_SCRIPT.value,
    ActionType.SHOW_NOTIFICATION.value,
}


class UtilityActionEditor(QWidget):
    changed = Signal()

    def __init__(
        self, action_type: str, data: dict | None = None,
        variables: list[str] | None = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self.action_type = action_type
        self.initial = dict(data or {})
        self.variables = list(variables or [])
        self.controls: dict[str, QWidget] = {}
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Paths, arguments, commands, and working folders support {{VARIABLE}} placeholders.")
        note.setWordWrap(True); note.setStyleSheet("color: #64748b;")
        self.form.addRow(note)
        self._build()

    def _line(self, key: str, default: str = "", placeholder: str = "") -> QLineEdit:
        field = QLineEdit(str(self.initial.get(key, default)))
        field.setPlaceholderText(placeholder)
        field.textChanged.connect(self.changed)
        self.controls[key] = field
        return field

    def _text(self, key: str, default: str = "", placeholder: str = "") -> QPlainTextEdit:
        field = QPlainTextEdit(str(self.initial.get(key, default)))
        field.setPlaceholderText(placeholder); field.setMinimumHeight(80)
        field.textChanged.connect(self.changed)
        self.controls[key] = field
        return field

    def _check(self, key: str, label: str, default: bool = False) -> QCheckBox:
        field = QCheckBox(label); field.setChecked(bool(self.initial.get(key, default)))
        field.toggled.connect(self.changed); self.controls[key] = field
        return field

    def _timeout(self, default: float = 30.0) -> None:
        field = QDoubleSpinBox(); field.setRange(0.1, 86400); field.setDecimals(1); field.setSuffix(" s")
        field.setValue(float(self.initial.get("timeout", default))); field.valueChanged.connect(self.changed)
        self.controls["timeout"] = field; self.form.addRow("Timeout", field)

    def _browse_row(self, key: str, label: str, mode: str = "file") -> None:
        field = self._line(key)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(field, 1)
        modes = (("File…", "file"), ("Folder…", "folder")) if mode == "source" else (
            (("Save As…", "destination"), ("Folder…", "folder")) if mode == "destination"
            else (("Browse…", mode),)
        )
        for text, browse_mode in modes:
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, selected=browse_mode: self._browse(field, selected))
            row.addWidget(button)
        wrapper = QWidget(); wrapper.setLayout(row); self.form.addRow(label, wrapper)

    def _working_directory(self) -> None:
        self._browse_row("working_directory", "Working folder", "folder")

    def _process_row(self) -> None:
        field = self._line("process_name", placeholder="notepad.exe")
        button = QPushButton("Pick Running…")
        button.setToolTip("Choose from processes currently running under Windows.")
        button.clicked.connect(lambda: self._pick_process(field))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(field, 1); row.addWidget(button)
        wrapper = QWidget(); wrapper.setLayout(row); self.form.addRow("Process", wrapper)

    def _output_fields(self) -> None:
        self.form.addRow("Stdout variable", self._line("output_variable"))
        self.form.addRow("Stderr variable", self._line("stderr_variable"))
        self.form.addRow("Exit code variable", self._line("exit_code_variable"))

    def _build(self) -> None:
        kind = self.action_type
        if kind == ActionType.LAUNCH_APPLICATION.value:
            self._browse_row("path", "Application", "file")
            self.form.addRow("Arguments", self._line("arguments"))
            self._working_directory()
            self.form.addRow("Store process ID as", self._line("output_variable"))
        elif kind in {ActionType.WAIT_PROCESS.value, ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value}:
            self._process_row()
            if kind == ActionType.WAIT_PROCESS.value:
                self._timeout(30.0)
                self.form.addRow("Store process ID as", self._line("output_variable"))
        elif kind == ActionType.READ_CLIPBOARD.value:
            self.form.addRow("Store text as", self._line("output_variable", "CLIPBOARD_VALUE"))
            self.form.addRow("", self._check("sensitive", "Protect clipboard value in logs and reports"))
        elif kind == ActionType.WRITE_CLIPBOARD.value:
            self.form.addRow("Clipboard text", self._text("text", placeholder="Text or {{VARIABLE}}"))
            self.form.addRow("", self._check("sensitive", "Protect clipboard value in logs and reports"))
        elif kind in {ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value}:
            self._browse_row("source", "Source", "source")
            self._browse_row("destination", "Destination", "destination")
            self.form.addRow("Store resulting path as", self._line("output_variable"))
        elif kind == ActionType.DELETE_PATH.value:
            self._browse_row("path", "Delete", "source")
            warning = QLabel("This permanently deletes the selected file or folder when the flow runs.")
            warning.setWordWrap(True); warning.setStyleSheet("color: #9a3412;")
            self.form.addRow(warning)
        elif kind == ActionType.WAIT_PATH.value:
            self._browse_row("path", "File or folder", "source")
            combo = QComboBox()
            for label, value in (("File or folder", "either"), ("File", "file"), ("Folder", "folder")):
                combo.addItem(label, value)
            combo.setCurrentIndex(max(0, combo.findData(self.initial.get("path_type", "either"))))
            combo.currentIndexChanged.connect(self.changed); self.controls["path_type"] = combo
            self.form.addRow("Wait for", combo); self._timeout(30.0)
            self.form.addRow("Store found path as", self._line("output_variable"))
        elif kind == ActionType.RUN_POWERSHELL.value:
            self.form.addRow("PowerShell command", self._text("command", placeholder="Get-ChildItem {{INPUT_FOLDER}}"))
            self._working_directory(); self._timeout(60.0); self._output_fields()
            self.form.addRow("", self._check("allow_nonzero_exit", "Allow a non-zero exit code"))
            self.form.addRow("", self._check("sensitive", "Mask command and arguments in logs"))
        elif kind == ActionType.RUN_PYTHON_SCRIPT.value:
            self._browse_row("path", "Python script", "python")
            self.form.addRow("Arguments", self._line("arguments"))
            self._working_directory(); self._timeout(60.0); self._output_fields()
            self.form.addRow("", self._check("allow_nonzero_exit", "Allow a non-zero exit code"))
            self.form.addRow("", self._check("sensitive", "Mask script arguments in logs"))
        elif kind == ActionType.SHOW_NOTIFICATION.value:
            self.form.addRow("Title", self._line("title", "Python RPA Recorder"))
            self.form.addRow("Message", self._text("message"))

    def _browse(self, field: QLineEdit, mode: str) -> None:
        path = ""
        if mode == "folder":
            path = QFileDialog.getExistingDirectory(self, "Choose folder")
        elif mode == "destination":
            path, _ = QFileDialog.getSaveFileName(self, "Choose destination")
        else:
            file_filter = "Python scripts (*.py *.pyw)" if mode == "python" else "All files (*)"
            path, _ = QFileDialog.getOpenFileName(self, "Choose file", filter=file_filter)
        if path:
            field.setText(path)

    def _pick_process(self, field: QLineEdit) -> None:
        try:
            names = sorted({item["name"] for item in enumerate_processes()}, key=str.casefold)
        except Exception as exc:
            QMessageBox.warning(self, "Pick Process", str(exc)); return
        if not names:
            QMessageBox.information(self, "Pick Process", "No running processes were found."); return
        value, accepted = QInputDialog.getItem(self, "Pick Running Process", "Process", names, 0, False)
        if accepted:
            field.setText(value)

    def data(self) -> dict:
        result: dict = {}
        for key, widget in self.controls.items():
            if isinstance(widget, QLineEdit):
                result[key] = widget.text().strip()
            elif isinstance(widget, QPlainTextEdit):
                result[key] = widget.toPlainText()
            elif isinstance(widget, QCheckBox):
                result[key] = widget.isChecked()
            elif isinstance(widget, QDoubleSpinBox):
                result[key] = widget.value()
            elif isinstance(widget, QComboBox):
                result[key] = widget.currentData()
        return result
