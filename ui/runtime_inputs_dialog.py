"""Interactive runtime-input collection using controls appropriate to each definition."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from rpa.models import RpaProject
from rpa.variables import prepare_runtime_variables


class RuntimeInputsDialog(QDialog):
    def __init__(
        self,
        project: RpaProject,
        initial_values: dict[str, Any] | None = None,
        clipboard_text: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self.initial_values = dict(initial_values or {})
        self.clipboard_text = clipboard_text
        self.input_values: dict[str, Any] = {}
        self.runtime_variables: dict[str, Any] = {}
        self.widgets: dict[str, QWidget] = {}
        self.setWindowTitle("Run Inputs")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        title = QLabel("Values needed for this run")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        description = QLabel("Confirm the values below before the recorder hides and execution starts.")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        for name, definition in project.runtime_inputs.items():
            widget = self._create_widget(name, definition)
            label = f"{name}{' *' if definition.required else ''}"
            if definition.description:
                widget.setToolTip(definition.description)
            form.addRow(label, widget)
            self.widgets[name] = widget
        layout.addLayout(form)
        required = QLabel("* Required · Password values are masked and excluded from logs.")
        required.setStyleSheet("color: #64748b;")
        layout.addWidget(required)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Start Run")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_widget(self, name, definition) -> QWidget:
        value = self.initial_values.get(name, definition.default)
        kind = definition.type.casefold()
        if kind == "dropdown":
            combo = QComboBox()
            combo.addItems(definition.options)
            combo.setCurrentText(str(value or ""))
            return combo
        if kind == "date":
            edit = QDateEdit()
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
            parsed = QDate.fromString(str(value or date.today().isoformat()), "yyyy-MM-dd")
            edit.setDate(parsed if parsed.isValid() else QDate.currentDate())
            return edit
        if kind in {"file", "folder"}:
            wrap = QWidget()
            row = QHBoxLayout(wrap)
            row.setContentsMargins(0, 0, 0, 0)
            line = QLineEdit(str(value or ""))
            line.setObjectName("path_value")
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _checked=False, target=line, folder=kind == "folder": self._browse(target, folder))
            row.addWidget(line, 1)
            row.addWidget(browse)
            return wrap
        line = QLineEdit(str(value or ""))
        if kind == "number":
            line.setValidator(QDoubleValidator(line))
            line.setPlaceholderText("Enter a number")
        if kind == "password" or definition.sensitive:
            line.setEchoMode(QLineEdit.Password)
        return line

    def _browse(self, line: QLineEdit, folder: bool) -> None:
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Select Folder", line.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select File", line.text())
        if path:
            line.setText(path)

    def _raw_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, widget in self.widgets.items():
            if isinstance(widget, QComboBox):
                values[name] = widget.currentText()
            elif isinstance(widget, QDateEdit):
                values[name] = widget.date().toString("yyyy-MM-dd")
            elif isinstance(widget, QLineEdit):
                values[name] = widget.text()
            else:
                line = widget.findChild(QLineEdit, "path_value")
                values[name] = line.text() if line else ""
        return values

    def _validate_and_accept(self) -> None:
        self.input_values = self._raw_values()
        variables, errors = prepare_runtime_variables(
            self.project, self.input_values, self.clipboard_text, validate_paths=True,
        )
        if errors:
            QMessageBox.warning(self, "Check Run Inputs", "Please correct these values:\n\n" + "\n".join(f"• {item}" for item in errors))
            return
        self.runtime_variables = variables
        self.accept()
