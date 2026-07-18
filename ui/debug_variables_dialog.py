"""Editable runtime-variable view used while replay is paused."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QMessageBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from rpa.models import RpaProject


class DebugVariablesDialog(QDialog):
    def __init__(
        self, project: RpaProject, values: dict[str, Any],
        sensitive_names: set[str], protected_names: set[str] | None = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Variables at Breakpoint")
        self.resize(700, 460)
        self.project = project
        self.original_values = dict(values)
        self.sensitive_names = set(sensitive_names)
        self.protected_names = set(protected_names or set())
        self.values = dict(values)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Edit ordinary project, input, or output values before resuming. "
            "Sensitive and protected built-in values remain read-only."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Variable", "Category", "Value", "Access"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        self._populate()
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Apply Values")
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate(self) -> None:
        self.table.setRowCount(len(self.original_values))
        for row, (name, value) in enumerate(sorted(self.original_values.items())):
            sensitive = name in self.sensitive_names
            protected = name in self.protected_names
            if name in self.project.variables:
                category = "Project"
            elif name in self.project.runtime_inputs:
                category = "Runtime Input"
            elif name in self.project.output_variables:
                category = "Output"
            else:
                category = "Built-in / Runtime"
            access = "Sensitive" if sensitive else "Protected" if protected else "Editable"
            display = "[REDACTED]" if sensitive else self._format(value)
            for column, text in enumerate((name, category, display, access)):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, name)
                if column != 2 or sensitive or protected:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()

    @staticmethod
    def _format(value: Any) -> str:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _apply(self) -> None:
        updated = dict(self.original_values)
        try:
            for row in range(self.table.rowCount()):
                name = self.table.item(row, 0).text()
                if name in self.sensitive_names or name in self.protected_names:
                    continue
                text = self.table.item(row, 2).text()
                updated[name] = self._coerce(text, self.original_values.get(name))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid Variable Value", str(exc))
            return
        self.values = updated
        self.accept()

    @staticmethod
    def _coerce(text: str, original: Any) -> Any:
        if isinstance(original, bool):
            normalized = text.strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError("Boolean values must be true/false, yes/no, or 1/0.")
            return normalized in {"true", "1", "yes"}
        if isinstance(original, int) and not isinstance(original, bool):
            return int(text)
        if isinstance(original, float):
            return float(text)
        if isinstance(original, (dict, list, tuple)):
            parsed = json.loads(text)
            return tuple(parsed) if isinstance(original, tuple) else parsed
        return text
