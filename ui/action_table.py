from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTableWidget, QTableWidgetItem

from rpa.models import ActionStatus, RpaAction
from rpa.control_flow import CONTROL_TYPES, parse_control_flow


STATUS_COLORS = {
    ActionStatus.PENDING.value: "#6b7280",
    ActionStatus.RUNNING.value: "#2563eb",
    ActionStatus.COMPLETED.value: "#16a34a",
    ActionStatus.FAILED.value: "#dc2626",
    ActionStatus.SKIPPED.value: "#ca8a04",
}


class ActionTable(QTableWidget):
    empty_area_clicked = Signal()
    context_action_requested = Signal(str)

    HEADERS = ["Step", "Action", "What it does", "Wait Before", "Target", "Status"]

    def __init__(self) -> None:
        super().__init__(0, len(self.HEADERS))
        self._columns_initialized = False
        self._actions: list[RpaAction] = []
        self._flow = parse_control_flow([])
        self._collapsed_action_ids: set[str] = set()
        self._filter_text = ""
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(
            "QTableWidget { background: #fafafa; alternate-background-color: #f3f6fa; gridline-color: #d9dee7; }"
            "QHeaderView::section { background: #eef1f5; padding: 5px; font-weight: 600; border: 0; border-right: 1px solid #d6dbe3; }"
            "QTableWidget::item { padding: 4px; }"
            "QTableWidget::item:selected { background: #1f6feb; color: white; }"
        )
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.cellClicked.connect(self._cell_clicked)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        # Every column stays user-resizable (Interactive), except "What it does"
        # which stretches to absorb any extra/deficit width so the table adapts
        # smoothly when the panel is resized instead of leaving dead space or
        # requiring a horizontal scrollbar.
        header.setStretchLastSection(False)
        for col in range(len(self.HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.setToolTip("Select a step to review it. Right-click for step commands.")

    def set_actions(self, actions: list[RpaAction]) -> None:
        self._actions = actions
        self._flow = parse_control_flow(actions)
        valid_ids = {actions[row].id for row in self._flow.group_ends}
        self._collapsed_action_ids.intersection_update(valid_ids)
        self.blockSignals(True)
        self.setRowCount(len(actions))
        for row, action in enumerate(actions):
            self._set_row(row, action)
        self.blockSignals(False)
        if not self._columns_initialized:
            self.resizeColumnsToContents()
            self.setColumnWidth(0, 56)
            self.setColumnWidth(1, 140)
            self.setColumnWidth(3, 100)
            self.setColumnWidth(5, 110)
            self._columns_initialized = True

    def apply_filter(self, text: str) -> None:
        self._filter_text = text
        text = text.strip().lower()
        for row in range(self.rowCount()):
            haystack = " ".join(self.item(row, col).text() for col in range(self.columnCount()) if self.item(row, col)).lower()
            filtered = bool(text and text not in haystack)
            collapsed = not text and self._row_is_collapsed(row)
            self.setRowHidden(row, filtered or collapsed)

    def update_action(self, row: int, action: RpaAction) -> None:
        if row < 0 or row >= self.rowCount():
            return
        self.blockSignals(True)
        self._set_row(row, action)
        self.blockSignals(False)

    def _set_row(self, row: int, action: RpaAction) -> None:
        depth = self._flow.depths[row] if row < len(self._flow.depths) else 0
        group = row in self._flow.group_ends
        expanded = action.id not in self._collapsed_action_ids
        step_text = f"{'▾' if expanded else '▸'} {row + 1}" if group else str(row + 1)
        indent = "    " * depth
        values = [
            step_text,
            indent + action.friendly_name(),
            indent + action.summary(),
            f"{action.delay_before:.2f} s",
            self._target_text(action),
            self._status_text(action),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col == 1:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
            if action.action in CONTROL_TYPES:
                item.setBackground(QColor("#eef6ff"))
                if col in (1, 2):
                    font = QFont(item.font()); font.setBold(True); item.setFont(font)
            if col == 5:
                color = STATUS_COLORS[ActionStatus.SKIPPED.value] if not action.enabled else STATUS_COLORS.get(action.status, STATUS_COLORS[ActionStatus.PENDING.value])
                item.setForeground(QColor(color))
            if not action.enabled and col != 5:
                item.setForeground(QColor("#94a3b8"))
            if col in (0, 5):
                item.setTextAlignment(Qt.AlignCenter)
            elif col == 3:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.setItem(row, col, item)

    @staticmethod
    def _target_text(action: RpaAction) -> str:
        image = str(action.data.get("image", ""))
        if image:
            return Path(image).name
        window = action.data.get("window")
        if isinstance(window, dict):
            return str(window.get("window_title") or window.get("process_name") or window.get("class_name") or "Selected window")
        return ""

    def _row_is_collapsed(self, row: int) -> bool:
        for opener, closer in self._flow.group_ends.items():
            if (
                0 <= opener < len(self._actions)
                and opener < row <= closer
                and self._actions[opener].id in self._collapsed_action_ids
            ):
                return True
        return False

    def _cell_clicked(self, row: int, column: int) -> None:
        if column != 0 or row not in self._flow.group_ends or not 0 <= row < len(self._actions):
            return
        action_id = self._actions[row].id
        if action_id in self._collapsed_action_ids:
            self._collapsed_action_ids.remove(action_id)
        else:
            self._collapsed_action_ids.add(action_id)
        self._set_row(row, self._actions[row])
        self.apply_filter(self._filter_text)

    def _status_text(self, action: RpaAction) -> str:
        if not action.enabled:
            return "Disabled"
        return str(action.status).title()

    def selected_index(self) -> int:
        rows = self.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def selected_indices(self) -> list[int]:
        return sorted(row.row() for row in self.selectionModel().selectedRows())

    def mousePressEvent(self, event) -> None:
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self.itemAt(position) is None:
            self.clearSelection()
            self.empty_area_clicked.emit()
        super().mousePressEvent(event)

    def _context_menu(self, position) -> None:
        item = self.itemAt(position)
        if item is not None:
            self.selectRow(item.row())
        selected = self.selected_index() >= 0
        menu = QMenu(self)
        for key, label in [
            ("test", "Test This Step"),
            ("run_from", "Run From Here"),
            ("run_until", "Run Until Here"),
            ("toggle_enabled", "Disable Step"),
            ("separator", ""),
            ("add", "Add Step"),
            ("insert_before", "Insert Before"),
            ("insert_after", "Insert After"),
            ("duplicate", "Duplicate Step"),
            ("delete", "Delete Step"),
            ("move_up", "Move Up"),
            ("move_down", "Move Down"),
            ("deselect", "Deselect All"),
        ]:
            if key == "separator":
                menu.addSeparator()
                continue
            if key == "toggle_enabled" and selected:
                status_item = self.item(self.selected_index(), 5)
                label = "Enable Step" if status_item and status_item.text() == "Disabled" else "Disable Step"
            action = menu.addAction(label)
            action.setEnabled(selected or key == "add")
            action.triggered.connect(lambda checked=False, value=key: self.context_action_requested.emit(value))
        menu.exec(self.viewport().mapToGlobal(position))
