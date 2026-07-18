"""Schedule Flows page: view and edit automatic run schedules for every flow.

Supports enabling/pausing/disabling a flow's schedule, running it immediately,
inspecting the last run's duration and failure reason, sorting by flow name,
last run, next run, or status, and remembering column widths and sort order
between openings (via QSettings).
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rpa.scheduler import (
    STATUS_FAILED,
    FlowSchedule,
    ScheduleStore,
    schedule_next_run,
)

INTERVAL_OPTIONS = [
    ("Every 15 minutes", 15),
    ("Every 30 minutes", 30),
    ("Every hour", 60),
    ("Every 2 hours", 120),
    ("Every 4 hours", 240),
    ("Every 6 hours", 360),
    ("Every 12 hours", 720),
    ("Every 24 hours", 1440),
]

COLUMN_FLOW = 0
COLUMN_STATE = 1
COLUMN_INTERVAL = 2
COLUMN_LAST_RUN = 3
COLUMN_DURATION = 4
COLUMN_LAST_STATUS = 5
COLUMN_NEXT_RUN = 6
COLUMN_ACTIONS = 7

SORTABLE_COLUMNS = {COLUMN_FLOW, COLUMN_LAST_RUN, COLUMN_LAST_STATUS, COLUMN_NEXT_RUN}

COLUMN_TOOLTIPS = {
    COLUMN_FLOW: "The automation's name (its flow folder).",
    COLUMN_STATE: "Whether the schedule is Enabled, Paused, or Disabled.",
    COLUMN_INTERVAL: "How often this flow runs automatically.",
    COLUMN_LAST_RUN: "When the flow last started running.",
    COLUMN_DURATION: "How long the last run took to finish.",
    COLUMN_LAST_STATUS: "Result of the last run: Success, Failed, Running, or Skipped.",
    COLUMN_NEXT_RUN: "When the flow is next scheduled to run automatically.",
    COLUMN_ACTIONS: "Run this flow immediately, pause/resume its schedule, disable it, or view details.",
}

HELP_TEXT = (
    "<b>Flow</b> - the automation's name.<br>"
    "<b>Enabled</b> - whether the schedule is Enabled, Paused, or Disabled.<br>"
    "<b>Run every</b> - how often the flow runs automatically.<br>"
    "<b>Last run</b> - the timestamp of the most recent run.<br>"
    "<b>Duration</b> - how long the last run took.<br>"
    "<b>Last status</b> - Success, Failed, Running, or Skipped.<br>"
    "<b>Next run</b> - when the flow will run next.<br>"
    "<b>Run Now</b> - runs the flow immediately; it does not change the schedule "
    "or its next run time.<br>"
    "<b>Pause/Resume</b> - temporarily stops automatic runs while keeping the "
    "schedule configuration.<br>"
    "<b>Enable/Disable</b> - turns the schedule fully on or off (asks for "
    "confirmation before disabling).<br>"
    "<b>Details</b> - shows the full last-run information, including the "
    "failure reason if the last run failed."
)


class ScheduleFlowsDialog(QDialog):
    """A page listing every flow, each with its own automatic-run schedule."""

    run_now_requested = Signal(str)

    HEADERS = ["Flow", "Enabled", "Run every", "Last run", "Duration", "Last status", "Next run", "Actions"]

    def __init__(self, store: ScheduleStore, settings: QSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self.setWindowTitle("Schedule Flows")
        self.resize(920, 440)

        self._sort_column = COLUMN_FLOW
        self._sort_order = Qt.AscendingOrder
        if self.settings is not None:
            self._sort_column = int(self.settings.value("schedule_dialog/sort_column", COLUMN_FLOW))
            order_value = int(self.settings.value("schedule_dialog/sort_order", int(Qt.AscendingOrder.value)))
            self._sort_order = Qt.DescendingOrder if order_value == int(Qt.DescendingOrder.value) else Qt.AscendingOrder
            if self._sort_column not in SORTABLE_COLUMNS:
                self._sort_column = COLUMN_FLOW

        info = QLabel(
            "Enabled flows run automatically at the chosen interval while this app stays open. "
            "Only one flow runs at a time; overlapping runs are skipped and marked accordingly."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #64748b;")

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_FLOW, QHeaderView.Stretch)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)
        for column, tooltip in COLUMN_TOOLTIPS.items():
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(tooltip)

        help_btn = QPushButton("?")
        help_btn.setFixedWidth(28)
        help_btn.setToolTip("Explain what each column and button means")
        help_btn.clicked.connect(self._show_help)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setToolTip("Reload schedules from disk now")
        refresh_btn.clicked.connect(self.reload)
        close_btn = QPushButton("Close")
        close_btn.setToolTip("Close this page")
        close_btn.clicked.connect(self.accept)
        buttons_row = QHBoxLayout()
        buttons_row.addWidget(help_btn)
        buttons_row.addStretch(1)
        buttons_row.addWidget(refresh_btn)
        buttons_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.table)
        layout.addLayout(buttons_row)

        self._restore_column_widths()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self.reload)
        self._refresh_timer.start()

        self.reload()

    # -- persistence -----------------------------------------------------

    def _restore_column_widths(self) -> None:
        if self.settings is None:
            return
        for column in range(len(self.HEADERS)):
            if column == COLUMN_FLOW:
                continue
            width = self.settings.value(f"schedule_dialog/col_width_{column}", None)
            if width:
                self.table.setColumnWidth(column, int(width))

    def _save_column_widths(self) -> None:
        if self.settings is None:
            return
        for column in range(len(self.HEADERS)):
            if column == COLUMN_FLOW:
                continue
            self.settings.setValue(f"schedule_dialog/col_width_{column}", self.table.columnWidth(column))

    def accept(self) -> None:
        self._save_column_widths()
        super().accept()

    def reject(self) -> None:
        self._save_column_widths()
        super().reject()

    def closeEvent(self, event) -> None:
        self._save_column_widths()
        super().closeEvent(event)

    # -- sorting -----------------------------------------------------------

    def _on_header_clicked(self, column: int) -> None:
        if column not in SORTABLE_COLUMNS:
            return
        if column == self._sort_column:
            self._sort_order = Qt.DescendingOrder if self._sort_order == Qt.AscendingOrder else Qt.AscendingOrder
        else:
            self._sort_column = column
            self._sort_order = Qt.AscendingOrder
        if self.settings is not None:
            self.settings.setValue("schedule_dialog/sort_column", self._sort_column)
            self.settings.setValue("schedule_dialog/sort_order", int(self._sort_order.value))
        self.reload()

    def _sort_key(self, schedule: FlowSchedule):
        if self._sort_column == COLUMN_LAST_RUN:
            return schedule.last_run_at or ""
        if self._sort_column == COLUMN_LAST_STATUS:
            return schedule.last_status or ""
        if self._sort_column == COLUMN_NEXT_RUN:
            return schedule.next_run_at or ""
        return schedule.flow_name.lower()

    # -- building ------------------------------------------------------------

    def reload(self) -> None:
        self.store.load()
        self.store.remove_missing_flows()
        flow_names = self.store.list_flow_names()
        schedules = [self.store.get(name) for name in flow_names]
        schedules.sort(key=self._sort_key, reverse=(self._sort_order == Qt.DescendingOrder))
        self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
        self.table.setRowCount(len(schedules))
        for row, schedule in enumerate(schedules):
            self._build_row(row, schedule)

    def _build_row(self, row: int, schedule: FlowSchedule) -> None:
        name_item = QTableWidgetItem(schedule.flow_name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
        name_item.setToolTip(schedule.flow_name)
        self.table.setItem(row, COLUMN_FLOW, name_item)

        self.table.setItem(row, COLUMN_STATE, self._readonly_item(self._state_text(schedule)))

        interval_combo = QComboBox()
        interval_combo.setToolTip("How often this flow runs automatically")
        for label, _ in INTERVAL_OPTIONS:
            interval_combo.addItem(label)
        closest_index = min(
            range(len(INTERVAL_OPTIONS)),
            key=lambda i: abs(INTERVAL_OPTIONS[i][1] - schedule.interval_minutes),
        )
        interval_combo.setCurrentIndex(closest_index)
        interval_combo.currentIndexChanged.connect(
            lambda index, name=schedule.flow_name: self._set_interval(name, INTERVAL_OPTIONS[index][1])
        )
        self.table.setCellWidget(row, COLUMN_INTERVAL, interval_combo)

        last_run_item = self._readonly_item(self._format_time(schedule.last_run_at))
        last_run_item.setToolTip(COLUMN_TOOLTIPS[COLUMN_LAST_RUN])
        self.table.setItem(row, COLUMN_LAST_RUN, last_run_item)

        duration_item = self._readonly_item(self._format_duration(schedule.last_duration_seconds))
        duration_item.setToolTip(COLUMN_TOOLTIPS[COLUMN_DURATION])
        self.table.setItem(row, COLUMN_DURATION, duration_item)

        status_item = self._readonly_item(schedule.last_status or "-")
        if schedule.last_status == STATUS_FAILED and schedule.last_error:
            status_item.setToolTip(f"Failure reason: {schedule.last_error}")
        else:
            status_item.setToolTip(COLUMN_TOOLTIPS[COLUMN_LAST_STATUS])
        self.table.setItem(row, COLUMN_LAST_STATUS, status_item)

        next_run_text = "Disabled"
        if schedule.enabled:
            next_run_text = "Paused" if schedule.paused else self._format_time(schedule.next_run_at)
        next_run_item = self._readonly_item(next_run_text)
        next_run_item.setToolTip(COLUMN_TOOLTIPS[COLUMN_NEXT_RUN])
        self.table.setItem(row, COLUMN_NEXT_RUN, next_run_item)

        self.table.setCellWidget(row, COLUMN_ACTIONS, self._build_actions_cell(schedule))

    def _build_actions_cell(self, schedule: FlowSchedule) -> QWidget:
        wrap = QWidget()
        wrap_layout = QHBoxLayout(wrap)
        wrap_layout.setContentsMargins(2, 2, 2, 2)
        wrap_layout.setSpacing(4)

        run_now_btn = QPushButton("Run Now")
        run_now_btn.setToolTip("Run this flow immediately. Does not affect the schedule.")
        run_now_btn.clicked.connect(lambda _=False, name=schedule.flow_name: self.run_now_requested.emit(name))
        wrap_layout.addWidget(run_now_btn)

        pause_btn = QPushButton("Resume" if schedule.paused else "Pause")
        pause_btn.setEnabled(schedule.enabled)
        pause_btn.setToolTip("Resume automatic runs" if schedule.paused else "Pause automatic runs (keeps the schedule configuration)")
        pause_btn.clicked.connect(lambda _=False, name=schedule.flow_name: self._toggle_pause(name))
        wrap_layout.addWidget(pause_btn)

        toggle_btn = QPushButton("Enable" if not schedule.enabled else "Disable")
        toggle_btn.setToolTip("Turn the schedule on" if not schedule.enabled else "Turn the schedule off (asks for confirmation)")
        toggle_btn.clicked.connect(lambda _=False, name=schedule.flow_name: self._toggle_enabled(name))
        wrap_layout.addWidget(toggle_btn)

        details_btn = QPushButton("Details")
        details_btn.setToolTip("View the full last-run details, including the failure reason")
        details_btn.clicked.connect(lambda _=False, name=schedule.flow_name: self._show_details(name))
        wrap_layout.addWidget(details_btn)

        return wrap

    # -- helpers ------------------------------------------------------------

    def _state_text(self, schedule: FlowSchedule) -> str:
        if not schedule.enabled:
            return "Disabled"
        if schedule.paused:
            return "Paused"
        return "Enabled"

    def _readonly_item(self, text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _format_time(self, value: str | None) -> str:
        if not value:
            return "-"
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return "-"
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")

    def _format_duration(self, seconds: float | None) -> str:
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m {secs:02d}s"

    # -- actions --------------------------------------------------------------

    def _set_interval(self, flow_name: str, minutes: int) -> None:
        schedule = self.store.get(flow_name)
        schedule.interval_minutes = minutes
        if schedule.enabled and not schedule.paused:
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self.reload()

    def _toggle_pause(self, flow_name: str) -> None:
        schedule = self.store.get(flow_name)
        if not schedule.enabled:
            return
        schedule.paused = not schedule.paused
        if not schedule.paused:
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self.reload()

    def _toggle_enabled(self, flow_name: str) -> None:
        schedule = self.store.get(flow_name)
        if schedule.enabled:
            reply = QMessageBox.question(
                self,
                "Disable Schedule",
                f"Disable the automatic schedule for '{flow_name}'?\n"
                "It will no longer run automatically until re-enabled.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            schedule.enabled = False
            schedule.paused = False
        else:
            schedule.enabled = True
            schedule.paused = False
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self.reload()

    def _show_details(self, flow_name: str) -> None:
        schedule = self.store.get(flow_name)
        lines = [
            f"Flow: {schedule.flow_name}",
            f"State: {self._state_text(schedule)}",
            f"Run every: {schedule.interval_minutes} minute(s)",
            f"Last run: {self._format_time(schedule.last_run_at)}",
            f"Last finished: {self._format_time(schedule.last_finished_at)}",
            f"Duration: {self._format_duration(schedule.last_duration_seconds)}",
            f"Last status: {schedule.last_status or '-'}",
            f"Next run: {self._format_time(schedule.next_run_at) if schedule.enabled else '-'}",
        ]
        if schedule.last_error:
            lines.append("")
            lines.append(f"Failure reason: {schedule.last_error}")
        QMessageBox.information(self, f"Schedule Details - {flow_name}", "\n".join(lines))

    def _show_help(self) -> None:
        QMessageBox.information(self, "Schedule Flows Help", HELP_TEXT)
