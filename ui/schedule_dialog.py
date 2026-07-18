"""Daily-use schedule management for recorded flows."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rpa.scheduler import STATUS_FAILED, STATUS_RUNNING, FlowSchedule, ScheduleStore, schedule_next_run

INTERVAL_OPTIONS = [
    ("Every 5 minutes", 5),
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
    COLUMN_LAST_STATUS: "Result of the last run.",
    COLUMN_NEXT_RUN: "When the flow is next scheduled to run automatically.",
    COLUMN_ACTIONS: "Run, pause, enable, disable, or inspect this schedule.",
}

HELP_TEXT = (
    "Search by flow name or use the status filter to narrow the list.<br><br>"
    "Select a row to review its run history and edit its interval in the Details panel. "
    "The Actions menu contains Run Now, Pause/Resume, Enable/Disable, and Details. "
    "Run Now does not change the automatic schedule."
)

BADGE_COLORS = {
    "Enabled": ("#dcfce7", "#166534"),
    "Paused": ("#fef3c7", "#92400e"),
    "Disabled": ("#e2e8f0", "#475569"),
    "Running": ("#dbeafe", "#1d4ed8"),
    "Success": ("#dcfce7", "#166534"),
    "Failed": ("#fee2e2", "#b91c1c"),
    "Skipped": ("#fef3c7", "#92400e"),
}


class ScheduleFlowsDialog(QDialog):
    """List and manage every flow's automatic-run schedule."""

    run_now_requested = Signal(str)
    HEADERS = ["Flow", "State", "Run every", "Last run", "Duration", "Last status", "Next run", "Actions"]

    def __init__(self, store: ScheduleStore, settings: QSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self._selected_flow_name: str | None = None
        self._detail_schedule: FlowSchedule | None = None
        self.setWindowTitle("Schedule Flows")
        self.setMinimumSize(820, 500)
        self.resize(1280, 700)

        self._sort_column = COLUMN_FLOW
        self._sort_order = Qt.AscendingOrder
        if self.settings is not None:
            self._sort_column = int(self.settings.value("schedule_dialog/sort_column", COLUMN_FLOW))
            order_value = int(self.settings.value("schedule_dialog/sort_order", int(Qt.AscendingOrder.value)))
            self._sort_order = Qt.DescendingOrder if order_value == int(Qt.DescendingOrder.value) else Qt.AscendingOrder
            if self._sort_column not in SORTABLE_COLUMNS:
                self._sort_column = COLUMN_FLOW

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        root.addLayout(self._build_header())
        root.addLayout(self._build_filters())
        root.addWidget(self._build_content(), 1)
        root.addLayout(self._build_footer())

        self._restore_column_widths()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self.reload)
        self._refresh_timer.start()
        self.reload()

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        title_column = QVBoxLayout()
        title = QLabel("Schedule Flows")
        title.setStyleSheet("font-size: 20px; font-weight: 650; color: #0f172a;")
        description = QLabel("Monitor scheduled automations, review results, and make quick schedule changes.")
        description.setStyleSheet("color: #64748b;")
        description.setWordWrap(True)
        title_column.addWidget(title)
        title_column.addWidget(description)
        layout.addLayout(title_column, 1)

        self.auto_refresh_label = QLabel("Auto-refresh on · every 5 sec")
        self.auto_refresh_label.setStyleSheet("color: #64748b;")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Reload schedules from disk now (F5)")
        self.refresh_btn.setShortcut("F5")
        self.refresh_btn.clicked.connect(self.reload)
        layout.addWidget(self.auto_refresh_label)
        layout.addWidget(self.refresh_btn)
        return layout

    def _build_filters(self) -> QVBoxLayout:
        outer = QVBoxLayout()
        summary = QHBoxLayout()
        self.summary_labels: dict[str, QLabel] = {}
        for state in ("Enabled", "Paused", "Disabled", "Running"):
            label = QLabel(f"{state}  0")
            label.setStyleSheet("padding: 5px 9px; background: #f1f5f9; border-radius: 6px; color: #334155;")
            self.summary_labels[state] = label
            summary.addWidget(label)
        summary.addStretch(1)
        outer.addLayout(summary)

        filters = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search flows…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setToolTip("Filter by flow name (Ctrl+F)")
        self.search_box.setMinimumWidth(220)
        self.search_box.textChanged.connect(self.reload)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All statuses", "Enabled", "Paused", "Disabled", "Running", "Success", "Failed", "Skipped"])
        self.status_filter.setToolTip("Show only schedules with this state or result")
        self.status_filter.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.search_box, 1)
        filters.addWidget(self.status_filter)
        outer.addLayout(filters)
        return outer

    def _build_content(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        table_wrap = QWidget()
        table_layout = QVBoxLayout(table_wrap)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(6)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.setStyleSheet(
            "QTableWidget { border: 1px solid #dbe3ec; border-radius: 7px; }"
            "QTableWidget::item { padding: 6px; border-bottom: 1px solid #edf2f7; }"
            "QTableWidget::item:selected { background: #dbeafe; color: #0f172a; }"
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COLUMN_FLOW, QHeaderView.Stretch)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._on_header_clicked)
        self.table.currentCellChanged.connect(self._on_current_cell_changed)
        for column, tooltip in COLUMN_TOOLTIPS.items():
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(tooltip)

        self.empty_label = QLabel("No scheduled flows found\nCreate a flow or change the filters to see schedules here.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("padding: 32px; color: #64748b; font-size: 14px;")
        self.empty_label.setVisible(False)
        table_layout.addWidget(self.table, 1)
        table_layout.addWidget(self.empty_label, 1)
        splitter.addWidget(table_wrap)
        splitter.addWidget(self._build_details_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 480])
        return splitter

    def _build_details_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setMinimumWidth(340)
        panel.setStyleSheet("QFrame { background: #f8fafc; border: 1px solid #dbe3ec; border-radius: 7px; }")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        self.detail_name = QLabel("Flow details")
        self.detail_name.setWordWrap(True)
        self.detail_name.setStyleSheet("font-size: 16px; font-weight: 650; border: none;")
        self.detail_state = QLabel("Select a flow")
        heading.addWidget(self.detail_name, 1)
        heading.addWidget(self.detail_state)
        layout.addLayout(heading)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        self.detail_values: dict[str, QLabel] = {}
        fields = [
            ("Last run", "last_run"), ("Duration", "duration"), ("Result", "result"),
            ("Next run", "next_run"), ("Error", "error"),
        ]
        for row, (caption, key) in enumerate(fields):
            caption_label = QLabel(caption)
            caption_label.setStyleSheet("color: #64748b; border: none;")
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setStyleSheet("color: #0f172a; border: none;")
            grid.addWidget(caption_label, row, 0, Qt.AlignTop)
            grid.addWidget(value, row, 1)
            self.detail_values[key] = value
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        interval_label = QLabel("Schedule interval")
        interval_label.setStyleSheet("font-weight: 600; border: none;")
        self.detail_interval = QComboBox()
        for label, minutes in INTERVAL_OPTIONS:
            self.detail_interval.addItem(label, minutes)
        self.detail_interval.setEnabled(False)
        self.detail_interval.currentIndexChanged.connect(self._detail_interval_changed)
        layout.addWidget(interval_label)
        layout.addWidget(self.detail_interval)

        history_heading = QHBoxLayout()
        history_title = QLabel("Run history")
        history_title.setStyleSheet("font-weight: 650; border: none;")
        self.history_filter = QComboBox()
        self.history_filter.addItems(["All runs", "Success", "Failed", "Skipped", "Running"])
        self.history_filter.setToolTip("Filter this flow's saved run history")
        self.history_filter.currentIndexChanged.connect(self._refresh_history)
        history_heading.addWidget(history_title)
        history_heading.addStretch(1)
        history_heading.addWidget(self.history_filter)
        layout.addLayout(history_heading)

        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(["Started", "Ended", "Duration", "Attempts", "Result", "Failed step", "Error"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setShowGrid(False)
        history_header = self.history_table.horizontalHeader()
        history_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        history_header.setSectionResizeMode(6, QHeaderView.Stretch)
        self.history_table.setMinimumHeight(150)
        layout.addWidget(self.history_table, 1)

        retention_row = QHBoxLayout()
        retention_label = QLabel("Keep history")
        retention_label.setStyleSheet("color: #64748b; border: none;")
        self.history_limit_spin = QSpinBox()
        self.history_limit_spin.setRange(10, 1000)
        self.history_limit_spin.setSingleStep(10)
        self.history_limit_spin.setSuffix(" runs per flow")
        self.history_limit_spin.setValue(max(10, self.store.history_limit))
        self.history_limit_spin.setToolTip("Older records are removed when this limit is exceeded")
        self.history_limit_spin.valueChanged.connect(self._history_limit_changed)
        retention_row.addWidget(retention_label)
        retention_row.addWidget(self.history_limit_spin, 1)
        layout.addLayout(retention_row)

        self.detail_run_btn = QPushButton("Run Now")
        self.detail_run_btn.setEnabled(False)
        self.detail_run_btn.clicked.connect(self._run_selected)
        self.detail_pause_btn = QPushButton("Pause")
        self.detail_pause_btn.setEnabled(False)
        self.detail_pause_btn.clicked.connect(self._pause_selected)
        self.detail_enabled_btn = QPushButton("Enable Schedule")
        self.detail_enabled_btn.setEnabled(False)
        self.detail_enabled_btn.clicked.connect(self._enable_selected)
        layout.addWidget(self.detail_run_btn)
        layout.addWidget(self.detail_pause_btn)
        layout.addWidget(self.detail_enabled_btn)
        return panel

    def _build_footer(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        help_btn = QPushButton("Help")
        help_btn.setToolTip("Explain schedule controls")
        help_btn.clicked.connect(self._show_help)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(help_btn)
        layout.addStretch(1)
        layout.addWidget(close_btn)
        return layout

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
            if column != COLUMN_FLOW:
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

    # -- sorting and filtering -------------------------------------------

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

    def _matches_filters(self, schedule: FlowSchedule) -> bool:
        query = self.search_box.text().strip().casefold()
        if query and query not in schedule.flow_name.casefold():
            return False
        wanted = self.status_filter.currentText()
        if wanted == "All statuses":
            return True
        if wanted in ("Enabled", "Paused", "Disabled"):
            return self._state_text(schedule) == wanted
        status = schedule.last_status or ""
        return status == wanted or (wanted == "Skipped" and status.startswith("Skipped"))

    # -- building --------------------------------------------------------

    def reload(self) -> None:
        current = self._flow_name_for_row(self.table.currentRow()) if self.table.rowCount() else None
        selected_name = current or self._selected_flow_name
        self.store.load()
        self.store.remove_missing_flows()
        schedules = [self.store.get(name) for name in self.store.list_flow_names()]
        self._update_summary(schedules)
        schedules = [schedule for schedule in schedules if self._matches_filters(schedule)]
        schedules.sort(key=self._sort_key, reverse=(self._sort_order == Qt.DescendingOrder))
        self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
        self.table.setRowCount(len(schedules))
        restore_row = -1
        for row, schedule in enumerate(schedules):
            self._build_row(row, schedule)
            if schedule.flow_name == selected_name:
                restore_row = row
        is_empty = not schedules
        self.table.setVisible(not is_empty)
        self.empty_label.setVisible(is_empty)
        if schedules:
            selected_row = restore_row if restore_row >= 0 else 0
            self.table.setCurrentCell(selected_row, COLUMN_FLOW)
            # setCurrentCell does not emit when the same cell remains selected.
            # Refresh the side panel explicitly so timer reloads never leave stale data.
            selected_schedule = schedules[selected_row]
            self._selected_flow_name = selected_schedule.flow_name
            self._show_schedule_details(selected_schedule)
        else:
            self._selected_flow_name = None
            self._show_schedule_details(None)
        self.auto_refresh_label.setText("Auto-refresh on · updated just now")

    def _update_summary(self, schedules: list[FlowSchedule]) -> None:
        counts = {"Enabled": 0, "Paused": 0, "Disabled": 0, "Running": 0}
        for schedule in schedules:
            counts[self._state_text(schedule)] += 1
            if schedule.last_status == STATUS_RUNNING:
                counts["Running"] += 1
        for name, count in counts.items():
            self.summary_labels[name].setText(f"{name}  {count}")

    def _build_row(self, row: int, schedule: FlowSchedule) -> None:
        name_item = self._readonly_item(schedule.flow_name)
        name_item.setData(Qt.UserRole, schedule.flow_name)
        name_item.setToolTip(schedule.flow_name)
        name_item.setFont(QFont(name_item.font().family(), name_item.font().pointSize(), QFont.DemiBold))
        self.table.setItem(row, COLUMN_FLOW, name_item)

        self.table.setItem(row, COLUMN_STATE, self._badge_item(self._state_text(schedule)))
        interval_text = next((label for label, minutes in INTERVAL_OPTIONS if minutes == schedule.interval_minutes), f"Every {schedule.interval_minutes} min")
        self.table.setItem(row, COLUMN_INTERVAL, self._readonly_item(interval_text))

        last_run_item = self._readonly_item(self._format_time(schedule.last_run_at))
        last_run_item.setToolTip(self._exact_time(schedule.last_run_at))
        self.table.setItem(row, COLUMN_LAST_RUN, last_run_item)
        duration_item = self._readonly_item(self._format_duration(schedule.last_duration_seconds))
        duration_item.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(row, COLUMN_DURATION, duration_item)

        status_text = schedule.last_status or "-"
        status_item = self._badge_item(self._badge_name(status_text), status_text)
        if schedule.last_status == STATUS_FAILED and schedule.last_error:
            status_item.setToolTip(f"Failure reason: {schedule.last_error}")
        self.table.setItem(row, COLUMN_LAST_STATUS, status_item)

        if not schedule.enabled:
            next_run_text = "Disabled"
        elif schedule.paused:
            next_run_text = "Paused"
        else:
            next_run_text = self._format_time(schedule.next_run_at, future=True)
        next_item = self._readonly_item(next_run_text)
        next_item.setToolTip(self._exact_time(schedule.next_run_at))
        self.table.setItem(row, COLUMN_NEXT_RUN, next_item)
        self.table.setCellWidget(row, COLUMN_ACTIONS, self._build_actions_cell(schedule))

    def _build_actions_cell(self, schedule: FlowSchedule) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(4, 4, 4, 4)
        button = QPushButton("Actions ▾")
        button.setToolTip("Run or change this schedule")
        menu = QMenu(button)
        menu.addAction("Run Now", lambda name=schedule.flow_name: self.run_now_requested.emit(name))
        pause_action = menu.addAction("Resume" if schedule.paused else "Pause", lambda name=schedule.flow_name: self._toggle_pause(name))
        pause_action.setEnabled(schedule.enabled)
        menu.addAction("Enable" if not schedule.enabled else "Disable", lambda name=schedule.flow_name: self._toggle_enabled(name))
        menu.addSeparator()
        menu.addAction("Details", lambda name=schedule.flow_name: self._show_details(name))
        button.setMenu(menu)
        layout.addWidget(button)
        return wrap

    # -- display helpers -------------------------------------------------

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

    def _badge_name(self, text: str) -> str:
        return "Skipped" if text.startswith("Skipped") else text

    def _badge_item(self, badge: str, text: str | None = None) -> QTableWidgetItem:
        item = self._readonly_item(text or badge)
        background, foreground = BADGE_COLORS.get(badge, ("#f1f5f9", "#475569"))
        item.setBackground(QColor(background))
        item.setForeground(QColor(foreground))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setTextAlignment(Qt.AlignCenter)
        return item

    def _parse_time(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).astimezone()
        except (ValueError, TypeError):
            return None

    def _format_time(self, value: str | None, future: bool = False) -> str:
        dt = self._parse_time(value)
        if dt is None:
            return "-"
        now = datetime.now().astimezone()
        seconds = (dt - now).total_seconds() if future else (now - dt).total_seconds()
        if seconds < -60:
            return "overdue" if future else dt.strftime("%b %d, %H:%M")
        seconds = max(0, seconds)
        if seconds < 60:
            return "due now" if future else "just now"
        minutes = int(seconds // 60)
        if minutes < 60:
            return f"in {minutes} min" if future else f"{minutes} min ago"
        hours = int(minutes // 60)
        if hours < 24:
            return f"in {hours} hr" if future else f"{hours} hr ago"
        days = int(hours // 24)
        if days < 7:
            return f"in {days} days" if future else f"{days} days ago"
        return dt.strftime("%b %d, %Y")

    def _exact_time(self, value: str | None) -> str:
        dt = self._parse_time(value)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z") if dt else "Not available"

    def _format_duration(self, seconds: float | None) -> str:
        if seconds is None:
            return "-"
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m {secs:02d}s"

    # -- selection and details ------------------------------------------

    def _flow_name_for_row(self, row: int) -> str | None:
        item = self.table.item(row, COLUMN_FLOW) if row >= 0 else None
        return str(item.data(Qt.UserRole) or item.text()) if item is not None else None

    def _on_current_cell_changed(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        name = self._flow_name_for_row(row)
        self._selected_flow_name = name
        self._show_schedule_details(self.store.get(name) if name else None)

    def _show_schedule_details(self, schedule: FlowSchedule | None) -> None:
        self._detail_schedule = schedule
        enabled = schedule is not None
        self.detail_interval.blockSignals(True)
        self.detail_interval.setEnabled(enabled)
        self.detail_run_btn.setEnabled(enabled)
        self.detail_enabled_btn.setEnabled(enabled)
        if schedule is None:
            self.detail_name.setText("Flow details")
            self.detail_state.setText("Select a flow")
            for value in self.detail_values.values():
                value.setText("—")
            self.detail_pause_btn.setEnabled(False)
        else:
            state = self._state_text(schedule)
            self.detail_name.setText(schedule.flow_name)
            self._style_label_badge(self.detail_state, state)
            self.detail_values["last_run"].setText(self._detail_time(schedule.last_run_at))
            self.detail_values["duration"].setText(self._format_duration(schedule.last_duration_seconds))
            self.detail_values["result"].setText(schedule.last_status or "No runs yet")
            next_text = "Disabled" if not schedule.enabled else ("Paused" if schedule.paused else self._detail_time(schedule.next_run_at, True))
            self.detail_values["next_run"].setText(next_text)
            self.detail_values["error"].setText(schedule.last_error or "None")
            closest = min(range(len(INTERVAL_OPTIONS)), key=lambda i: abs(INTERVAL_OPTIONS[i][1] - schedule.interval_minutes))
            self.detail_interval.setCurrentIndex(closest)
            self.detail_pause_btn.setEnabled(schedule.enabled)
            self.detail_pause_btn.setText("Resume Schedule" if schedule.paused else "Pause Schedule")
            self.detail_enabled_btn.setText("Disable Schedule" if schedule.enabled else "Enable Schedule")
        self.detail_interval.blockSignals(False)
        self._refresh_history()

    def _refresh_history(self, _index: int | None = None) -> None:
        schedule = self._detail_schedule
        entries = list(reversed(schedule.history)) if schedule is not None else []
        wanted = self.history_filter.currentText()
        if wanted != "All runs":
            entries = [entry for entry in entries if self._history_status_matches(entry.status, wanted)]
        self.history_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            started = self._readonly_item(self._format_time(entry.started_at))
            started.setToolTip(self._exact_time(entry.started_at))
            ended = self._readonly_item(self._format_time(entry.finished_at))
            ended.setToolTip(self._exact_time(entry.finished_at))
            duration = self._readonly_item(self._format_duration(entry.duration_seconds))
            attempts = self._readonly_item(str(entry.attempts) if entry.attempts is not None else "-")
            result = self._badge_item(self._badge_name(entry.status), entry.status)
            failed_step = self._readonly_item(f"Step {entry.failed_step}" if entry.failed_step is not None else "-")
            error = self._readonly_item(entry.error or "-")
            error.setToolTip(entry.error or "No error")
            for column, item in enumerate((started, ended, duration, attempts, result, failed_step, error)):
                self.history_table.setItem(row, column, item)

    def _history_status_matches(self, status: str, wanted: str) -> bool:
        return status == wanted or (wanted == "Skipped" and status.startswith("Skipped"))

    def _history_limit_changed(self, limit: int) -> None:
        self.store.set_history_limit(limit)
        self.store.save()
        if self.settings is not None:
            self.settings.setValue("scheduler/history_limit", limit)
        if self._detail_schedule is not None:
            self._detail_schedule = self.store.get(self._detail_schedule.flow_name)
        self._refresh_history()

    def _style_label_badge(self, label: QLabel, badge: str) -> None:
        background, foreground = BADGE_COLORS.get(badge, ("#f1f5f9", "#475569"))
        label.setText(badge)
        label.setStyleSheet(f"padding: 4px 7px; background: {background}; color: {foreground}; font-weight: 600; border: none; border-radius: 5px;")

    def _detail_time(self, value: str | None, future: bool = False) -> str:
        relative = self._format_time(value, future)
        exact = self._exact_time(value)
        return relative if exact == "Not available" else f"{relative}\n{exact}"

    def _detail_interval_changed(self, index: int) -> None:
        if self._detail_schedule is not None and index >= 0:
            self._set_interval(self._detail_schedule.flow_name, int(self.detail_interval.itemData(index)))

    def _run_selected(self) -> None:
        if self._detail_schedule is not None:
            self.run_now_requested.emit(self._detail_schedule.flow_name)

    def _pause_selected(self) -> None:
        if self._detail_schedule is not None:
            self._toggle_pause(self._detail_schedule.flow_name)

    def _enable_selected(self) -> None:
        if self._detail_schedule is not None:
            self._toggle_enabled(self._detail_schedule.flow_name)

    # -- actions ---------------------------------------------------------

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
                self, "Disable Schedule",
                f"Disable the automatic schedule for '{flow_name}'?\nIt will no longer run automatically until re-enabled.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
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
        for row in range(self.table.rowCount()):
            if self._flow_name_for_row(row) == flow_name:
                self.table.setCurrentCell(row, COLUMN_FLOW)
                self.table.setFocus()
                return
        self._selected_flow_name = flow_name
        self._show_schedule_details(self.store.get(flow_name))

    def _show_help(self) -> None:
        QMessageBox.information(self, "Schedule Flows Help", HELP_TEXT)
