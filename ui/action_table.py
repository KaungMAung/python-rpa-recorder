from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTableWidget, QTableWidgetItem

from rpa.models import ActionStatus, RpaAction


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
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
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
        text = text.strip().lower()
        for row in range(self.rowCount()):
            haystack = " ".join(self.item(row, col).text() for col in range(self.columnCount()) if self.item(row, col)).lower()
            self.setRowHidden(row, bool(text and text not in haystack))

    def update_action(self, row: int, action: RpaAction) -> None:
        if row < 0 or row >= self.rowCount():
            return
        self.blockSignals(True)
        self._set_row(row, action)
        self.blockSignals(False)

    def _set_row(self, row: int, action: RpaAction) -> None:
        values = [
            str(row + 1),
            action.friendly_name(),
            action.summary(),
            f"{action.delay_before:.2f} s",
            Path(str(action.data.get("image", ""))).name,
            self._status_text(action),
        ]
        for col, value in enumerate(values):
            item = QTableWidgetItem(value)
            if col == 1:
                font = QFont(item.font())
                font.setBold(True)
                item.setFont(font)
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

    def _status_text(self, action: RpaAction) -> str:
        if not action.enabled:
            return "Disabled"
        return str(action.status).title()

    def selected_index(self) -> int:
        rows = self.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

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
