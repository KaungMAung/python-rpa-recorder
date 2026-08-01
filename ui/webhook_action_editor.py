from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from rpa.variables import VARIABLE_NAME_PATTERN
from rpa.webhook import builder_rows_to_json, plain_json_to_builder_rows, plain_json_to_object


class WebhookActionEditor(QWidget):
    changed = Signal()

    def __init__(self, data: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        initial = deepcopy(data or {})
        self._changing_mode = False
        self._builder_before_json: list[dict[str, str]] | None = None
        self._converted_json_text: str | None = None
        self.url = QLineEdit(str(initial.get("url", "")))
        self.url.setPlaceholderText("https://...logic.azure.com/...")
        self.mode = QComboBox()
        self.mode.addItem("Key/Value Builder", "builder")
        self.mode.addItem("Plain JSON", "json")
        self.mode.setCurrentIndex(max(0, self.mode.findData(initial.get("payload_mode", "builder"))))
        self._current_mode = str(self.mode.currentData())
        self.rows = QTableWidget(0, 4)
        self.rows.setHorizontalHeaderLabels(["Name", "Value", "Type", ""])
        self.rows.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.rows.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.rows.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.rows.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.rows.verticalHeader().setVisible(False)
        self.rows.setMinimumHeight(150)
        self.rows.itemChanged.connect(self._items_changed)
        add_field = QPushButton("+ Add field")
        add_field.clicked.connect(lambda: self.add_row())
        builder = QWidget()
        builder_layout = QVBoxLayout(builder)
        builder_layout.setContentsMargins(0, 0, 0, 0)
        builder_layout.addWidget(self.rows)
        builder_layout.addWidget(add_field, 0)
        self.json_editor = QPlainTextEdit(str(initial.get("json_payload", "{}")))
        self.json_editor.setPlaceholderText('{\n  "runId": "{{run_id}}"\n}')
        self.json_editor.setMinimumHeight(170)
        self.payload_stack = QStackedWidget()
        self.payload_stack.addWidget(builder)
        self.payload_stack.addWidget(self.json_editor)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(10, 120)
        self.timeout.setDecimals(1)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(float(initial.get("timeout", 60.0)))
        self.output_variable = QLineEdit(str(initial.get("output_variable", "")))
        self.output_variable.setPlaceholderText("Optional variable name")
        self.failure_action = QComboBox()
        self.failure_action.addItem("Stop flow", "stop")
        self.failure_action.addItem("Continue flow", "continue")
        self.failure_action.setCurrentIndex(max(0, self.failure_action.findData(initial.get("failure_action", "stop"))))
        note = QLabel("Sends one synchronous HTTP POST with application/json. No authentication or custom headers are added.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        form = QFormLayout(self)
        form.addRow(note)
        form.addRow("Webhook URL", self.url)
        form.addRow("Payload mode", self.mode)
        form.addRow("Payload", self.payload_stack)
        form.addRow("Timeout", self.timeout)
        form.addRow("Save response as", self.output_variable)
        form.addRow("On failure", self.failure_action)
        for row in initial.get("payload_fields", []) if isinstance(initial.get("payload_fields", []), list) else []:
            if isinstance(row, dict):
                self.add_row(row)
        self.payload_stack.setCurrentIndex(0 if self._current_mode == "builder" else 1)
        self.url.textChanged.connect(self.changed)
        self.json_editor.textChanged.connect(self.changed)
        self.timeout.valueChanged.connect(self.changed)
        self.output_variable.textChanged.connect(self.changed)
        self.failure_action.currentIndexChanged.connect(self.changed)
        self.mode.currentIndexChanged.connect(self._mode_changed)

    def add_row(self, row: dict | None = None) -> None:
        row = dict(row or {})
        index = self.rows.rowCount()
        self.rows.insertRow(index)
        name = QTableWidgetItem(str(row.get("name", "")))
        value = QTableWidgetItem(str(row.get("value", "")))
        self.rows.setItem(index, 0, name)
        self.rows.setItem(index, 1, value)
        field_type = QComboBox()
        for label, key in (("Text", "text"), ("Number", "number"), ("Boolean", "boolean"), ("Null", "null")):
            field_type.addItem(label, key)
        field_type.setCurrentIndex(max(0, field_type.findData(str(row.get("type", "text")).lower())))
        field_type.currentIndexChanged.connect(lambda _index=0, combo=field_type: self._type_changed(combo))
        self.rows.setCellWidget(index, 2, field_type)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda _checked=False, button=remove: self._remove_button_row(button))
        self.rows.setCellWidget(index, 3, remove)
        self._type_changed(field_type, emit=False)
        self.changed.emit()

    def payload_rows(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for row in range(self.rows.rowCount()):
            kind = self.rows.cellWidget(row, 2)
            result.append({
                "name": self.rows.item(row, 0).text().strip() if self.rows.item(row, 0) else "",
                "value": self.rows.item(row, 1).text() if self.rows.item(row, 1) else "",
                "type": str(kind.currentData()) if isinstance(kind, QComboBox) else "text",
            })
        return result

    def data(self) -> dict:
        return {
            "url": self.url.text().strip(),
            "payload_mode": str(self.mode.currentData()),
            "payload_fields": deepcopy(self.payload_rows()),
            "json_payload": self.json_editor.toPlainText(),
            "timeout": self.timeout.value(),
            "output_variable": self.output_variable.text().strip(),
            "failure_action": str(self.failure_action.currentData()),
        }

    def validation_error(self) -> str | None:
        if not self.url.text().strip():
            return "Enter the Power Automate webhook URL."
        output = self.output_variable.text().strip()
        if output and not VARIABLE_NAME_PATTERN.fullmatch(output):
            return "Save response as must be a valid variable name."
        try:
            if self.mode.currentData() == "builder":
                builder_rows_to_json(self.payload_rows())
            else:
                plain_json_to_object(self.json_editor.toPlainText())
        except ValueError as exc:
            return str(exc)
        return None

    def _mode_changed(self, _index: int = 0) -> None:
        if self._changing_mode:
            return
        selected = str(self.mode.currentData())
        try:
            if self._current_mode == "builder" and selected == "json":
                self._builder_before_json = deepcopy(self.payload_rows())
                self._converted_json_text = builder_rows_to_json(self._builder_before_json)
                self.json_editor.setPlainText(self._converted_json_text)
            elif self._current_mode == "json" and selected == "builder":
                current_json = self.json_editor.toPlainText()
                converted = (
                    deepcopy(self._builder_before_json)
                    if self._builder_before_json is not None and current_json == self._converted_json_text
                    else plain_json_to_builder_rows(current_json)
                )
                self.rows.blockSignals(True)
                self.rows.setRowCount(0)
                self.rows.blockSignals(False)
                for row in converted:
                    self.add_row(row)
        except ValueError as exc:
            self._changing_mode = True
            self.mode.setCurrentIndex(self.mode.findData(self._current_mode))
            self._changing_mode = False
            QMessageBox.information(self, "Keep Plain JSON", str(exc))
            return
        self._current_mode = selected
        if selected == "builder":
            self._builder_before_json = None
            self._converted_json_text = None
        self.payload_stack.setCurrentIndex(0 if selected == "builder" else 1)
        self.changed.emit()

    def _remove_button_row(self, button: QPushButton) -> None:
        for row in range(self.rows.rowCount()):
            if self.rows.cellWidget(row, 3) is button:
                self.rows.removeRow(row)
                self.changed.emit()
                return

    def _type_changed(self, combo: QComboBox, emit: bool = True) -> None:
        for row in range(self.rows.rowCount()):
            if self.rows.cellWidget(row, 2) is combo:
                item = self.rows.item(row, 1)
                if item:
                    item.setFlags(
                        item.flags() | Qt.ItemIsEditable
                        if combo.currentData() != "null"
                        else item.flags() & ~Qt.ItemIsEditable
                    )
                break
        if emit:
            self.changed.emit()

    def _items_changed(self, _item: QTableWidgetItem) -> None:
        self.changed.emit()


class PowerAutomateEmailEditor(QWidget):
    changed = Signal()

    def __init__(self, data: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        initial = dict(data or {})
        self.url = QLineEdit(str(initial.get("url", "")))
        self.url.setPlaceholderText("https://...logic.azure.com/...")
        self.to = QLineEdit(str(initial.get("to", "")))
        self.to.setPlaceholderText("recipient@example.com or {{recipient}}")
        self.cc = QLineEdit(str(initial.get("cc", "")))
        self.cc.setPlaceholderText("Optional")
        self.subject = QLineEdit(str(initial.get("subject", "")))
        self.subject.setPlaceholderText("Run {{run_id}} completed")
        self.body = QPlainTextEdit(str(initial.get("body", "")))
        self.body.setPlaceholderText("Plain text or HTML; {{variables}} are supported")
        self.body.setMinimumHeight(190)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(10, 120)
        self.timeout.setDecimals(1)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(float(initial.get("timeout", 60.0)))
        self.output_variable = QLineEdit(str(initial.get("output_variable", "")))
        self.output_variable.setPlaceholderText("Optional variable name")
        self.failure_action = QComboBox()
        self.failure_action.addItem("Stop flow", "stop")
        self.failure_action.addItem("Continue flow", "continue")
        self.failure_action.setCurrentIndex(max(
            0, self.failure_action.findData(initial.get("failure_action", "stop")),
        ))
        note = QLabel("Send an email through a user-provided Power Automate webhook.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748b;")
        form = QFormLayout(self)
        form.addRow(note)
        form.addRow("Webhook URL", self.url)
        form.addRow("To", self.to)
        form.addRow("CC", self.cc)
        form.addRow("Subject", self.subject)
        form.addRow("Body", self.body)
        form.addRow("Timeout", self.timeout)
        form.addRow("Save response as", self.output_variable)
        form.addRow("On failure", self.failure_action)
        for field in (self.url, self.to, self.cc, self.subject, self.output_variable):
            field.textChanged.connect(self.changed)
        self.body.textChanged.connect(self.changed)
        self.timeout.valueChanged.connect(self.changed)
        self.failure_action.currentIndexChanged.connect(self.changed)

    def data(self) -> dict:
        return {
            "url": self.url.text().strip(),
            "to": self.to.text().strip(),
            "cc": self.cc.text().strip(),
            "subject": self.subject.text(),
            "body": self.body.toPlainText(),
            "timeout": self.timeout.value(),
            "output_variable": self.output_variable.text().strip(),
            "failure_action": str(self.failure_action.currentData()),
        }

    def validation_error(self) -> str | None:
        for field, message in (
            (self.url.text(), "Enter the Power Automate webhook URL."),
            (self.to.text(), "Enter at least one recipient in To."),
            (self.subject.text(), "Enter the email subject."),
            (self.body.toPlainText(), "Enter the email body."),
        ):
            if not field.strip():
                return message
        output = self.output_variable.text().strip()
        if output and not VARIABLE_NAME_PATTERN.fullmatch(output):
            return "Save response as must be a valid variable name."
        return None
