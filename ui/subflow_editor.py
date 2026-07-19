from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from rpa.project_manager import ProjectManager
from rpa.subflows import discover_saved_flows, mapping_dict


class SubflowEditor(QWidget):
    """Picker and variable mapping editor for a portable subflow reference."""

    changed = Signal()
    open_requested = Signal(str)

    def __init__(
        self, project_dir: Path | None, parent_variables: list[str],
        data: dict | None = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self.project_dir = Path(project_dir) if project_dir else None
        self.parent_variables = sorted(set(parent_variables))
        self._loading = True
        data = data or {}

        self.flow = QComboBox()
        self.flow.setObjectName("subflowPicker")
        self.open_button = QPushButton("Open Flow")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open)
        target_row = QHBoxLayout()
        target_row.addWidget(self.flow, 1)
        target_row.addWidget(self.open_button)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #64748b;")
        self.inputs = self._mapping_table("Subflow input", "Parent variable")
        self.outputs = self._mapping_table("Subflow output", "Parent variable")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(target_row)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("Inputs passed to the subflow"))
        layout.addWidget(self.inputs)
        layout.addWidget(QLabel("Outputs returned to this flow"))
        layout.addWidget(self.outputs)

        current = str(data.get("project", ""))
        flows = discover_saved_flows(self.project_dir) if self.project_dir else []
        self.flow.addItem("Choose a saved flow…", "")
        for saved in flows:
            self.flow.addItem(saved.name, saved.reference)
        selected = self.flow.findData(current)
        if current and selected < 0:
            self.flow.addItem(f"Missing flow ({current})", current)
            selected = self.flow.count() - 1
        self.flow.setCurrentIndex(max(0, selected))
        self._initial_inputs = mapping_dict(data.get("input_mappings"))
        self._initial_outputs = mapping_dict(data.get("output_mappings"))
        self.flow.currentIndexChanged.connect(self._target_changed)
        self._loading = False
        self._target_changed()

    @staticmethod
    def _mapping_table(left: str, right: str) -> QTableWidget:
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels([left, right])
        table.horizontalHeader().setStretchLastSection(True)
        table.setMinimumHeight(105)
        table.setToolTip("Only mapped values are shared between the parent and subflow.")
        return table

    def _load_target(self):
        if not self.project_dir or not self.flow.currentData():
            return None
        try:
            path = (self.project_dir / str(self.flow.currentData())).resolve()
            return ProjectManager().load(path) if path.is_file() else None
        except (OSError, ValueError, TypeError):
            return None

    def _target_changed(self, *_args) -> None:
        project = self._load_target()
        reference = str(self.flow.currentData() or "")
        self.open_button.setEnabled(bool(project and reference))
        self.inputs.setRowCount(0)
        self.outputs.setRowCount(0)
        if project is None:
            self.status.setText("Choose another saved flow in the same projects folder." if not reference else "The referenced flow is missing or cannot be read.")
        else:
            self.status.setText(f"{len(project.actions)} steps • mappings are optional")
            child_inputs = sorted(set(project.variables) | set(project.runtime_inputs))
            child_outputs = sorted(project.output_variables)
            self._populate(self.inputs, child_inputs, self.parent_variables, self._initial_inputs)
            self._populate(self.outputs, child_outputs, self.parent_variables, self._initial_outputs, editable_right=True)
        if not self._loading:
            self._initial_inputs = {}
            self._initial_outputs = {}
            self.changed.emit()

    def _populate(
        self, table: QTableWidget, left_names: list[str], right_names: list[str],
        mappings: dict[str, str], editable_right: bool = False,
    ) -> None:
        for left_name in left_names:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(left_name))
            combo = QComboBox()
            combo.addItem("Not mapped", "")
            for name in right_names:
                combo.addItem(name, name)
            combo.setEditable(editable_right)
            wanted = mappings.get(left_name, "")
            index = combo.findData(wanted)
            if wanted and index < 0 and editable_right:
                combo.setEditText(wanted)
            else:
                combo.setCurrentIndex(max(0, index))
            combo.currentIndexChanged.connect(self.changed)
            if editable_right:
                combo.currentTextChanged.connect(self.changed)
            table.setCellWidget(row, 1, combo)

    @staticmethod
    def _table_data(table: QTableWidget) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            combo = table.cellWidget(row, 1)
            if not item or not isinstance(combo, QComboBox):
                continue
            value = str(combo.currentData() or combo.currentText()).strip()
            if value and value != "Not mapped":
                result[item.text()] = value
        return result

    def data(self) -> dict:
        reference = str(self.flow.currentData() or "")
        return {
            "project": reference,
            "flow_name": self.flow.currentText() if reference else "",
            "input_mappings": self._table_data(self.inputs),
            "output_mappings": self._table_data(self.outputs),
        }

    def _open(self) -> None:
        reference = str(self.flow.currentData() or "")
        if reference:
            self.open_requested.emit(reference)
