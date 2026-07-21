"""Daily-use schedule management for recorded flows."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rpa.scheduler import (
    STATUS_FAILED, STATUS_RUNNING, FlowSchedule, ScheduleStore,
    TASK_DISABLED, TASK_MISSING, TASK_REGISTERED, TASK_REGISTRATION_FAILED, TASK_RUNNING,
    schedule_next_run,
)
from rpa.windows_tasks import WindowsTaskRegistrar
from rpa.project_manager import ProjectManager
from ui.run_details_dialog import RunDetailsDialog
from ui.runtime_inputs_dialog import RuntimeInputsDialog

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
COLUMN_TASK = 7
COLUMN_ACTIONS = 8

SORTABLE_COLUMNS = {COLUMN_FLOW, COLUMN_LAST_RUN, COLUMN_LAST_STATUS, COLUMN_NEXT_RUN}

COLUMN_TOOLTIPS = {
    COLUMN_FLOW: "The automation's name (its flow folder).",
    COLUMN_STATE: "Whether the schedule is Enabled, Paused, or Disabled.",
    COLUMN_INTERVAL: "How often this flow runs automatically.",
    COLUMN_LAST_RUN: "When the flow last started running.",
    COLUMN_DURATION: "How long the last run took to finish.",
    COLUMN_LAST_STATUS: "Result of the last run.",
    COLUMN_NEXT_RUN: "When the flow is next scheduled to run automatically.",
    COLUMN_TASK: "Windows Task Scheduler registration state.",
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
    "Completed verified": ("#dcfce7", "#166534"),
    "Completed unverified": ("#dbeafe", "#1d4ed8"),
    "Recovered": ("#ccfbf1", "#0f766e"),
    "Requires attention": ("#ffedd5", "#c2410c"),
    "Stopped by user": ("#e2e8f0", "#475569"),
    TASK_REGISTERED: ("#dcfce7", "#166534"),
    TASK_DISABLED: ("#e2e8f0", "#475569"),
    TASK_RUNNING: ("#dbeafe", "#1d4ed8"),
    TASK_MISSING: ("#fef3c7", "#92400e"),
    TASK_REGISTRATION_FAILED: ("#fee2e2", "#b91c1c"),
}


class _ScheduleRefreshBridge(QObject):
    """Delivers filesystem and Task Scheduler reads back to the GUI thread."""

    finished = Signal(int, object, object, float, float)


class ScheduleFlowsDialog(QDialog):
    """List and manage every flow's automatic-run schedule."""

    run_now_requested = Signal(str)
    task_log = Signal(str)
    HEADERS = ["Flow", "State", "Run every", "Last run", "Duration", "Last status", "Next run", "Windows task", "Actions"]

    def __init__(
        self, store: ScheduleStore, settings: QSettings | None = None, parent=None,
        task_registrar: WindowsTaskRegistrar | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.settings = settings
        self.task_registrar = task_registrar
        self._selected_flow_name: str | None = None
        self._selected_schedule_id: str | None = None
        self._detail_schedule: FlowSchedule | None = None
        self._visible_history_entries = []
        self._schedules: list[FlowSchedule] = []
        self._rendered_schedule_ids: list[str] = []
        self._history_signature: tuple | None = None
        self._history_visible_limit = 100
        self._refresh_generation = 0
        self._refresh_in_progress = False
        self._closing = False
        self._task_status_cache: dict[str, object] = {}
        self._task_cache_updated_at = 0.0
        self._refresh_bridge = _ScheduleRefreshBridge()
        self._refresh_bridge.finished.connect(self._apply_background_refresh)
        self._opened_at = time.perf_counter()
        self.setWindowTitle("Schedule Flows")
        self.setMinimumSize(960, 600)
        self.resize(1450, 820)

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
        self._restore_splitter_state()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self.reload)
        self._refresh_timer.start()
        # Render the already-loaded store immediately, then refresh disk/task state
        # without delaying the first paint.
        self._render_schedules(self.store.cached_schedules())
        open_ms = (time.perf_counter() - self._opened_at) * 1000
        QTimer.singleShot(0, lambda: self.task_log.emit(
            f"[Scheduler timing] dialog open: {open_ms:.1f} ms"
        ))
        QTimer.singleShot(0, self.reload)

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
        self.refresh_btn.clicked.connect(lambda: self.reload(force_tasks=True))
        self.add_schedule_btn = QPushButton("Add Schedule")
        self.add_schedule_btn.setToolTip("Add another independent schedule for the selected flow")
        self.add_schedule_btn.clicked.connect(self._add_schedule)
        layout.addWidget(self.auto_refresh_label)
        layout.addWidget(self.add_schedule_btn)
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
        self.search_box.textChanged.connect(self._render_current_schedules)
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All statuses", "Enabled", "Paused", "Disabled", "Running", "Success", "Failed", "Skipped"])
        self.status_filter.setToolTip("Show only schedules with this state or result")
        self.status_filter.currentIndexChanged.connect(self._render_current_schedules)
        filters.addWidget(self.search_box, 1)
        filters.addWidget(self.status_filter)
        outer.addLayout(filters)
        return outer

    def _build_content(self) -> QSplitter:
        splitter = QSplitter(Qt.Horizontal)
        self.content_splitter = splitter
        splitter.setChildrenCollapsible(False)
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
        for column in (COLUMN_STATE, COLUMN_DURATION, COLUMN_LAST_STATUS, COLUMN_TASK, COLUMN_ACTIONS):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setMinimumSectionSize(78)
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
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([760, 640])
        return splitter

    def _build_details_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setMinimumWidth(420)
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
            ("Schedule ID", "schedule_id"), ("Task name", "task_name"),
            ("Registration", "task_status"), ("Task error", "task_error"),
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
        self.advanced_toggle = QPushButton("Advanced Schedule Settings  ▸")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolTip("Show task privileges, timeout, runtime inputs, and history retention")
        self.advanced_toggle.toggled.connect(self._toggle_advanced_settings)
        layout.addWidget(self.advanced_toggle)
        self.advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(8, 2, 0, 4)
        advanced_layout.setSpacing(8)
        self.highest_privileges_check = QCheckBox("Run with highest privileges")
        self.highest_privileges_check.setToolTip(
            "Registers only this task with elevated privileges. Windows may request UAC approval."
        )
        self.highest_privileges_check.toggled.connect(self._task_options_changed)
        advanced_layout.addWidget(self.highest_privileges_check)
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(QLabel("Execution timeout"))
        self.execution_timeout_spin = QSpinBox()
        self.execution_timeout_spin.setRange(0, 10080)
        self.execution_timeout_spin.setSpecialValueText("No timeout")
        self.execution_timeout_spin.setSuffix(" min")
        self.execution_timeout_spin.valueChanged.connect(self._task_options_changed)
        timeout_row.addWidget(self.execution_timeout_spin, 1)
        advanced_layout.addLayout(timeout_row)
        runtime_row = QHBoxLayout()
        self.runtime_inputs_label = QLabel("Runtime inputs: not configured")
        self.runtime_inputs_label.setStyleSheet("color: #64748b; border: none;")
        self.runtime_inputs_btn = QPushButton("Configure Inputs…")
        self.runtime_inputs_btn.setEnabled(False)
        self.runtime_inputs_btn.setToolTip("Save values used by unattended scheduled runs")
        self.runtime_inputs_btn.clicked.connect(self._configure_runtime_inputs)
        runtime_row.addWidget(self.runtime_inputs_label, 1)
        runtime_row.addWidget(self.runtime_inputs_btn)
        advanced_layout.addLayout(runtime_row)

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
        advanced_layout.addLayout(retention_row)
        layout.addWidget(self.advanced_panel)
        advanced_open = bool(self.settings and self.settings.value(
            "schedule_dialog/advanced_open", False, type=bool,
        ))
        self.advanced_toggle.setChecked(advanced_open)
        self._toggle_advanced_settings(advanced_open)

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

        self.history_table = QTableWidget(0, 8)
        self.history_table.setHorizontalHeaderLabels(["Started", "Source", "Ended", "Duration", "Attempts", "Result", "Failed step", "Error"])
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
        history_header.setSectionResizeMode(7, QHeaderView.Stretch)
        history_header.setMinimumSectionSize(76)
        self.history_table.verticalHeader().setDefaultSectionSize(34)
        self.history_table.setWordWrap(False)
        self.history_table.setMinimumHeight(240)
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        self.history_table.itemDoubleClicked.connect(lambda _item: self._open_selected_run_details())
        layout.addWidget(self.history_table, 1)

        history_footer = QHBoxLayout()
        self.history_count_label = QLabel("No runs")
        self.history_count_label.setStyleSheet("color: #64748b; border: none;")
        self.load_more_history_btn = QPushButton("Load More")
        self.load_more_history_btn.setVisible(False)
        self.load_more_history_btn.clicked.connect(self._load_more_history)
        self.run_details_btn = QPushButton("Run Details")
        self.run_details_btn.setEnabled(False)
        self.run_details_btn.setToolTip("Open the selected run's detailed execution report")
        self.run_details_btn.clicked.connect(self._open_selected_run_details)
        history_footer.addWidget(self.history_count_label)
        history_footer.addStretch(1)
        history_footer.addWidget(self.load_more_history_btn)
        history_footer.addWidget(self.run_details_btn)
        layout.addLayout(history_footer)

        self.detail_run_btn = QPushButton("Run Now")
        self.detail_run_btn.setEnabled(False)
        self.detail_run_btn.clicked.connect(self._run_selected)
        self.detail_pause_btn = QPushButton("Pause")
        self.detail_pause_btn.setEnabled(False)
        self.detail_pause_btn.clicked.connect(self._pause_selected)
        self.detail_enabled_btn = QPushButton("Enable Schedule")
        self.detail_enabled_btn.setEnabled(False)
        self.detail_enabled_btn.clicked.connect(self._enable_selected)
        self.test_run_btn = QPushButton("Test Run")
        self.test_run_btn.setEnabled(False)
        self.test_run_btn.clicked.connect(self._test_selected)
        self.repair_task_btn = QPushButton("Repair / Register Task")
        self.repair_task_btn.setEnabled(False)
        self.repair_task_btn.setToolTip("Create or update this schedule's Windows Task Scheduler task")
        self.repair_task_btn.clicked.connect(self._repair_selected_task)
        self.delete_schedule_btn = QPushButton("Delete Schedule")
        self.delete_schedule_btn.setEnabled(False)
        self.delete_schedule_btn.clicked.connect(self._delete_selected)
        primary_actions = QGridLayout()
        primary_actions.setHorizontalSpacing(8)
        primary_actions.setVerticalSpacing(8)
        primary_actions.addWidget(self.detail_run_btn, 0, 0)
        primary_actions.addWidget(self.detail_enabled_btn, 0, 1)
        primary_actions.addWidget(self.detail_pause_btn, 1, 0)
        primary_actions.addWidget(self.repair_task_btn, 1, 1)
        layout.addLayout(primary_actions)
        self.more_actions_btn = QPushButton("More Actions  ▾")
        more_menu = QMenu(self.more_actions_btn)
        more_menu.addAction("Test Run", self._test_selected)
        more_menu.addAction("Configure Runtime Inputs", self._configure_runtime_inputs)
        more_menu.addSeparator()
        more_menu.addAction("Delete Schedule", self._delete_selected)
        self.more_actions_btn.setMenu(more_menu)
        layout.addWidget(self.more_actions_btn)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumWidth(440)
        scroll.setWidget(panel)
        return scroll

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

    def _restore_splitter_state(self) -> None:
        if self.settings is None:
            return
        state = self.settings.value("schedule_dialog/splitter_state")
        if state:
            self.content_splitter.restoreState(state)

    def _save_splitter_state(self) -> None:
        if self.settings is not None:
            self.settings.setValue("schedule_dialog/splitter_state", self.content_splitter.saveState())

    def _toggle_advanced_settings(self, visible: bool) -> None:
        self.advanced_panel.setVisible(visible)
        self.advanced_toggle.setText(
            "Advanced Schedule Settings  ▾" if visible else "Advanced Schedule Settings  ▸"
        )
        if self.settings is not None:
            self.settings.setValue("schedule_dialog/advanced_open", visible)

    def accept(self) -> None:
        self._save_column_widths()
        self._save_splitter_state()
        self._closing = True
        super().accept()

    def reject(self) -> None:
        self._save_column_widths()
        self._save_splitter_state()
        self._closing = True
        super().reject()

    def closeEvent(self, event) -> None:
        self._save_column_widths()
        self._save_splitter_state()
        self._closing = True
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
        self._render_current_schedules()

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

    def reload(self, _checked: bool = False, *, force_tasks: bool = False) -> None:
        """Refresh disk/task state in one non-overlapping background operation."""
        # Local edits are reflected immediately; disk and Windows reads follow in
        # the worker. This keeps filters, actions, and tests deterministic.
        self._render_schedules(self.store.cached_schedules())
        if self._refresh_in_progress or self._closing:
            return
        self._refresh_in_progress = True
        self._refresh_generation += 1
        generation = self._refresh_generation
        query_tasks = bool(
            self.task_registrar is not None
            and (force_tasks or time.monotonic() - self._task_cache_updated_at >= 30.0)
        )
        root = self.store.flows_root
        limit = self.store.history_limit
        registrar = self.task_registrar

        def worker() -> None:
            load_started = time.perf_counter()
            try:
                fresh_store = ScheduleStore(root, history_limit=limit)
                fresh_store.remove_missing_flows()
                schedules = fresh_store.list_schedules()
                load_ms = (time.perf_counter() - load_started) * 1000
                task_started = time.perf_counter()
                task_results: dict[str, object] = {}
                if query_tasks and registrar is not None:
                    for schedule in schedules:
                        task_results[schedule.schedule_id] = registrar.query(schedule)
                task_ms = (time.perf_counter() - task_started) * 1000
                self._refresh_bridge.finished.emit(
                    generation, fresh_store, task_results, load_ms, task_ms,
                )
            except Exception as exc:
                self._refresh_bridge.finished.emit(
                    generation, exc, {}, (time.perf_counter() - load_started) * 1000, 0.0,
                )

        threading.Thread(target=worker, name="schedule-dialog-refresh", daemon=True).start()

    def _apply_background_refresh(
        self, generation: int, payload: object, task_results: object,
        load_ms: float, task_ms: float,
    ) -> None:
        if generation != self._refresh_generation or self._closing:
            return
        self._refresh_in_progress = False
        if isinstance(payload, Exception):
            self.task_log.emit(f"[Scheduler] Refresh failed: {payload}")
            self.auto_refresh_label.setText("Auto-refresh on · refresh failed")
            return
        fresh_store = payload
        schedules = fresh_store.cached_schedules()
        results = dict(task_results)
        if results:
            self._task_status_cache.update(results)
            self._task_cache_updated_at = time.monotonic()
        for schedule in schedules:
            result = self._task_status_cache.get(schedule.schedule_id)
            if result is not None:
                schedule.task_status = result.status
                schedule.task_error = result.error
                schedule.windows_task_name = result.task_name
        # Adopt the completed load on the GUI thread. Stale generations are
        # rejected above, so a user edit can never be overwritten by an older read.
        self.store.adopt_loaded_state(fresh_store)
        self._render_schedules(schedules)
        total_ms = load_ms + task_ms
        self.task_log.emit(
            f"[Scheduler timing] schedule load {load_ms:.1f} ms; "
            f"Windows task query {task_ms:.1f} ms; refresh {total_ms:.1f} ms"
        )
        self.auto_refresh_label.setText("Auto-refresh on · updated just now")

    def _render_current_schedules(self, _value=None) -> None:
        self._render_schedules(self._schedules or self.store.cached_schedules())

    def _render_schedules(self, all_schedules: list[FlowSchedule]) -> None:
        render_started = time.perf_counter()
        current = self._schedule_id_for_row(self.table.currentRow()) if self.table.rowCount() else None
        selected_id = current or self._selected_schedule_id
        self._schedules = list(all_schedules)
        for schedule in self._schedules:
            cached = self._task_status_cache.get(schedule.schedule_id)
            if cached is not None:
                schedule.task_status = cached.status
                schedule.task_error = cached.error
                schedule.windows_task_name = cached.task_name
        self._update_summary(self._schedules)
        schedules = [schedule for schedule in self._schedules if self._matches_filters(schedule)]
        schedules.sort(key=self._sort_key, reverse=(self._sort_order == Qt.DescendingOrder))
        self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
        new_ids = [schedule.schedule_id for schedule in schedules]
        structure_changed = new_ids != self._rendered_schedule_ids
        scroll_value = self.table.verticalScrollBar().value()
        if structure_changed:
            self.table.setRowCount(len(schedules))
        restore_row = -1
        for row, schedule in enumerate(schedules):
            self._build_row(row, schedule, rebuild_actions=structure_changed)
            if schedule.schedule_id == selected_id:
                restore_row = row
        self._rendered_schedule_ids = new_ids
        if structure_changed:
            self.table.verticalScrollBar().setValue(scroll_value)
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
            self._selected_schedule_id = selected_schedule.schedule_id
            self._show_schedule_details(selected_schedule)
        else:
            self._selected_flow_name = None
            self._selected_schedule_id = None
            self._show_schedule_details(None)
        self.task_log.emit(
            f"[Scheduler timing] table/details render: {(time.perf_counter() - render_started) * 1000:.1f} ms"
        )

    def _update_summary(self, schedules: list[FlowSchedule]) -> None:
        counts = {"Enabled": 0, "Paused": 0, "Disabled": 0, "Running": 0}
        for schedule in schedules:
            counts[self._state_text(schedule)] += 1
            if schedule.last_status == STATUS_RUNNING:
                counts["Running"] += 1
        for name, count in counts.items():
            self.summary_labels[name].setText(f"{name}  {count}")

    def _build_row(self, row: int, schedule: FlowSchedule, *, rebuild_actions: bool = True) -> None:
        name_item = self._readonly_item(schedule.flow_name)
        name_item.setData(Qt.UserRole, schedule.schedule_id)
        name_item.setData(Qt.UserRole + 1, schedule.flow_name)
        name_item.setToolTip(f"{schedule.flow_name}\nSchedule ID: {schedule.schedule_id}")
        name_item.setFont(QFont(name_item.font().family(), name_item.font().pointSize(), QFont.DemiBold))
        self._set_table_item(row, COLUMN_FLOW, name_item)

        self._set_table_item(row, COLUMN_STATE, self._badge_item(self._state_text(schedule)))
        interval_text = next((label for label, minutes in INTERVAL_OPTIONS if minutes == schedule.interval_minutes), f"Every {schedule.interval_minutes} min")
        self._set_table_item(row, COLUMN_INTERVAL, self._readonly_item(interval_text))

        last_run_item = self._readonly_item(self._format_time(schedule.last_run_at))
        last_run_item.setToolTip(self._exact_time(schedule.last_run_at))
        self._set_table_item(row, COLUMN_LAST_RUN, last_run_item)
        duration_item = self._readonly_item(self._format_duration(schedule.last_duration_seconds))
        duration_item.setTextAlignment(Qt.AlignCenter)
        self._set_table_item(row, COLUMN_DURATION, duration_item)

        status_text = schedule.last_status or "-"
        status_item = self._badge_item(self._badge_name(status_text), status_text)
        if schedule.last_status == STATUS_FAILED and schedule.last_error:
            status_item.setToolTip(f"Failure reason: {schedule.last_error}")
        self._set_table_item(row, COLUMN_LAST_STATUS, status_item)

        if not schedule.enabled:
            next_run_text = "Disabled"
        elif schedule.paused:
            next_run_text = "Paused"
        else:
            next_run_text = self._format_time(schedule.next_run_at, future=True)
        next_item = self._readonly_item(next_run_text)
        next_item.setToolTip(self._exact_time(schedule.next_run_at))
        self._set_table_item(row, COLUMN_NEXT_RUN, next_item)
        task_item = self._badge_item(schedule.task_status, schedule.task_status)
        task_item.setToolTip("\n".join(filter(None, (
            schedule.windows_task_name, schedule.task_error or schedule.task_status,
        ))))
        self._set_table_item(row, COLUMN_TASK, task_item)
        if rebuild_actions or self.table.cellWidget(row, COLUMN_ACTIONS) is None:
            self.table.setCellWidget(row, COLUMN_ACTIONS, self._build_actions_cell(schedule))

    def _set_table_item(self, row: int, column: int, source: QTableWidgetItem) -> None:
        """Update stable rows without replacing their item/model identities."""
        target = self.table.item(row, column)
        if target is None:
            self.table.setItem(row, column, source)
            return
        target.setText(source.text())
        target.setFlags(source.flags())
        target.setToolTip(source.toolTip())
        target.setFont(source.font())
        target.setTextAlignment(Qt.Alignment(source.textAlignment()))
        target.setBackground(source.background())
        target.setForeground(source.foreground())
        target.setData(Qt.UserRole, source.data(Qt.UserRole))
        target.setData(Qt.UserRole + 1, source.data(Qt.UserRole + 1))

    def _build_actions_cell(self, schedule: FlowSchedule) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(4, 4, 4, 4)
        button = QPushButton("Actions ▾")
        button.setToolTip("Run or change this schedule")
        menu = QMenu(button)
        identifier = schedule.schedule_id
        menu.aboutToShow.connect(lambda menu=menu, identifier=identifier: self._populate_actions_menu(menu, identifier))
        button.setMenu(menu)
        layout.addWidget(button)
        return wrap

    def _populate_actions_menu(self, menu: QMenu, identifier: str) -> None:
        menu.clear()
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        menu.addAction("Run Now", lambda: self._run_schedule_now(identifier))
        menu.addAction("Test Run", lambda: self._test_schedule(identifier))
        menu.addAction("Repair / Register Task", lambda: self._repair_task(identifier))
        pause_action = menu.addAction("Resume" if schedule.paused else "Pause", lambda: self._toggle_pause(identifier))
        pause_action.setEnabled(schedule.enabled)
        menu.addAction("Enable" if not schedule.enabled else "Disable", lambda: self._toggle_enabled(identifier))
        menu.addSeparator()
        menu.addAction("Details", lambda: self._show_details(identifier))
        menu.addAction("Delete Schedule", lambda: self._delete_schedule(identifier))

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
        if text.startswith("Skipped"):
            return "Skipped"
        return text.replace("_", " ").capitalize() if text.isupper() else text

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
        return str(item.data(Qt.UserRole + 1) or item.text()) if item is not None else None

    def _schedule_id_for_row(self, row: int) -> str | None:
        item = self.table.item(row, COLUMN_FLOW) if row >= 0 else None
        return str(item.data(Qt.UserRole)) if item is not None and item.data(Qt.UserRole) else None

    def _on_current_cell_changed(self, row: int, _column: int, _previous_row: int, _previous_column: int) -> None:
        name = self._flow_name_for_row(row)
        schedule_id = self._schedule_id_for_row(row)
        self._selected_flow_name = name
        self._selected_schedule_id = schedule_id
        selected = next((item for item in self._schedules if item.schedule_id == schedule_id), None)
        self._show_schedule_details(selected)

    def _show_schedule_details(self, schedule: FlowSchedule | None) -> None:
        previous_id = self._detail_schedule.schedule_id if self._detail_schedule else None
        self._detail_schedule = schedule
        enabled = schedule is not None
        self.detail_interval.blockSignals(True)
        self.detail_interval.setEnabled(enabled)
        self.detail_run_btn.setEnabled(enabled)
        self.test_run_btn.setEnabled(enabled and self.task_registrar is not None)
        self.repair_task_btn.setEnabled(enabled and self.task_registrar is not None)
        self.delete_schedule_btn.setEnabled(enabled)
        self.more_actions_btn.setEnabled(enabled)
        self.detail_enabled_btn.setEnabled(enabled)
        self.runtime_inputs_btn.setEnabled(enabled)
        if schedule is None:
            self.detail_name.setText("Flow details")
            self.detail_state.setText("Select a flow")
            for value in self.detail_values.values():
                value.setText("—")
            self.detail_pause_btn.setEnabled(False)
            self.runtime_inputs_label.setText("Runtime inputs: not configured")
            self.highest_privileges_check.setChecked(False)
            self.execution_timeout_spin.setValue(0)
        else:
            state = self._state_text(schedule)
            self.detail_name.setText(schedule.flow_name)
            self.detail_values["schedule_id"].setText(schedule.schedule_id)
            self.detail_values["task_name"].setText(schedule.windows_task_name or "Not registered")
            self.detail_values["task_status"].setText(schedule.task_status)
            self.detail_values["task_status"].setToolTip(schedule.task_error or schedule.task_status)
            self.detail_values["task_error"].setText(schedule.task_error or "None")
            self._style_label_badge(self.detail_state, state)
            self.detail_values["last_run"].setText(self._detail_time(schedule.last_run_at))
            self.detail_values["duration"].setText(self._format_duration(schedule.last_duration_seconds))
            self.detail_values["result"].setText(schedule.last_status or "No runs yet")
            next_text = "Disabled" if not schedule.enabled else ("Paused" if schedule.paused else self._detail_time(schedule.next_run_at, True))
            self.detail_values["next_run"].setText(next_text)
            self.detail_values["error"].setText(schedule.last_error or "None")
            for key in ("task_name", "task_error", "error", "schedule_id"):
                self.detail_values[key].setToolTip(self.detail_values[key].text())
            closest = min(range(len(INTERVAL_OPTIONS)), key=lambda i: abs(INTERVAL_OPTIONS[i][1] - schedule.interval_minutes))
            self.detail_interval.setCurrentIndex(closest)
            self.detail_pause_btn.setEnabled(schedule.enabled)
            self.detail_pause_btn.setText("Resume Schedule" if schedule.paused else "Pause Schedule")
            self.detail_enabled_btn.setText("Disable Schedule" if schedule.enabled else "Enable Schedule")
            count = len(schedule.runtime_inputs)
            self.runtime_inputs_label.setText(
                f"Runtime inputs: {count} saved" if count else "Runtime inputs: using flow defaults"
            )
            self.highest_privileges_check.blockSignals(True)
            self.execution_timeout_spin.blockSignals(True)
            self.highest_privileges_check.setChecked(schedule.run_with_highest_privileges)
            self.execution_timeout_spin.setValue(schedule.execution_timeout_minutes or 0)
            self.highest_privileges_check.blockSignals(False)
            self.execution_timeout_spin.blockSignals(False)
        self.detail_interval.blockSignals(False)
        signature = self._schedule_history_signature(schedule)
        if schedule is None or schedule.schedule_id != previous_id:
            self._history_visible_limit = 100
        if signature != self._history_signature:
            self._history_signature = signature
            self._refresh_history()

    def _schedule_history_signature(self, schedule: FlowSchedule | None) -> tuple | None:
        if schedule is None:
            return None
        last = schedule.history[-1] if schedule.history else None
        return (
            schedule.schedule_id, len(schedule.history),
            getattr(last, "run_id", None), getattr(last, "status", None),
            getattr(last, "finished_at", None),
        )

    def _refresh_history(self, _index: int | None = None) -> None:
        schedule = self._detail_schedule
        entries = list(reversed(schedule.history)) if schedule is not None else []
        wanted = self.history_filter.currentText()
        if wanted != "All runs":
            entries = [entry for entry in entries if self._history_status_matches(entry.status, wanted)]
        total = len(entries)
        entries = entries[:self._history_visible_limit]
        self._visible_history_entries = entries
        self.history_table.setRowCount(len(entries))
        history_started = time.perf_counter()
        for row, entry in enumerate(entries):
            started = self._readonly_item(self._format_time(entry.started_at))
            started.setToolTip(self._exact_time(entry.started_at))
            ended = self._readonly_item(self._format_time(entry.finished_at))
            ended.setToolTip(self._exact_time(entry.finished_at))
            duration = self._readonly_item(self._format_duration(entry.duration_seconds))
            source = self._readonly_item(entry.source or "Legacy run")
            attempts = self._readonly_item(str(entry.attempts) if entry.attempts is not None else "-")
            result = self._badge_item(self._badge_name(entry.status), entry.status)
            failed_step = self._readonly_item(f"Step {entry.failed_step}" if entry.failed_step is not None else "-")
            error = self._readonly_item(entry.error or "-")
            error.setToolTip(entry.error or "No error")
            for column, item in enumerate((started, source, ended, duration, attempts, result, failed_step, error)):
                self.history_table.setItem(row, column, item)
        self.run_details_btn.setEnabled(False)
        self.history_count_label.setText(
            f"Showing {len(entries)} of {total} runs" if total else "No runs"
        )
        self.load_more_history_btn.setVisible(len(entries) < total)
        self.task_log.emit(
            f"[Scheduler timing] history load ({len(entries)} rows): "
            f"{(time.perf_counter() - history_started) * 1000:.1f} ms"
        )

    def _load_more_history(self) -> None:
        self._history_visible_limit += 100
        self._refresh_history()

    def _history_selection_changed(self) -> None:
        row = self.history_table.currentRow()
        has_evidence = 0 <= row < len(self._visible_history_entries) and bool(
            self._visible_history_entries[row].evidence_path
        )
        self.run_details_btn.setEnabled(has_evidence)

    def _open_selected_run_details(self) -> None:
        row = self.history_table.currentRow()
        if not 0 <= row < len(self._visible_history_entries) or self._detail_schedule is None:
            return
        entry = self._visible_history_entries[row]
        if not entry.evidence_path:
            QMessageBox.information(
                self, "Run Details", "Detailed evidence was not recorded for this older run.",
            )
            return
        path = Path(entry.evidence_path)
        if not path.is_absolute():
            path = self.store.flows_root / self._detail_schedule.flow_name / path
        RunDetailsDialog(path, self).exec()

    def _history_status_matches(self, status: str, wanted: str) -> bool:
        return status == wanted or (wanted == "Skipped" and status.startswith("Skipped"))

    def _history_limit_changed(self, limit: int) -> None:
        self._invalidate_background_refresh()
        self.store.set_history_limit(limit)
        self.store.save()
        if self.settings is not None:
            self.settings.setValue("scheduler/history_limit", limit)
        if self._detail_schedule is not None:
            self._detail_schedule = self.store.get_by_id(self._detail_schedule.schedule_id)
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
            self._set_interval(self._detail_schedule.schedule_id, int(self.detail_interval.itemData(index)))

    def _task_options_changed(self, _value=None) -> None:
        schedule = self._detail_schedule
        if schedule is None:
            return
        schedule.run_with_highest_privileges = self.highest_privileges_check.isChecked()
        timeout = self.execution_timeout_spin.value()
        schedule.execution_timeout_minutes = timeout if timeout > 0 else None
        self.store.set(schedule)
        self.store.save()
        self._sync_task(schedule)

    def _configure_runtime_inputs(self) -> None:
        schedule = self._detail_schedule
        if schedule is None:
            return
        try:
            project = ProjectManager().load(self.store.flows_root / schedule.flow_name / "project.json")
        except Exception as exc:
            QMessageBox.warning(self, "Runtime Inputs", f"Could not load this flow:\n\n{exc}")
            return
        if not project.runtime_inputs:
            QMessageBox.information(
                self, "Runtime Inputs",
                "This flow has no Runtime Inputs. Add them from the main Variables window first.",
            )
            return
        dialog = RuntimeInputsDialog(project, schedule.runtime_inputs, parent=self)
        dialog.setWindowTitle("Scheduled Runtime Inputs")
        ok_button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok)
        if ok_button:
            ok_button.setText("Save Inputs")
        if dialog.exec() == QDialog.Accepted:
            schedule.runtime_inputs = dict(dialog.input_values)
            self.store.set(schedule)
            self.store.save()
            self._sync_task(schedule)
            self._show_schedule_details(self.store.get_by_id(schedule.schedule_id))

    def _run_selected(self) -> None:
        if self._detail_schedule is not None:
            self._run_schedule_now(self._detail_schedule.schedule_id)

    def _pause_selected(self) -> None:
        if self._detail_schedule is not None:
            self._toggle_pause(self._detail_schedule.schedule_id)

    def _enable_selected(self) -> None:
        if self._detail_schedule is not None:
            self._toggle_enabled(self._detail_schedule.schedule_id)

    def _test_selected(self) -> None:
        if self._detail_schedule is not None:
            self._test_schedule(self._detail_schedule.schedule_id)

    def _repair_selected_task(self) -> None:
        if self._detail_schedule is not None:
            self._repair_task(self._detail_schedule.schedule_id)

    def _delete_selected(self) -> None:
        if self._detail_schedule is not None:
            self._delete_schedule(self._detail_schedule.schedule_id)

    def _repair_task(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None or self.task_registrar is None:
            return
        if not self._confirm_action(
            schedule, "repair", "Repair / Register Windows Task",
            strong=True,
            detail="This changes a system-level Windows Task Scheduler registration.",
        ):
            return
        if self._sync_task(schedule):
            QMessageBox.information(
                self, "Windows Task Registered",
                f"Task Scheduler is synchronized with this schedule.\n\n{schedule.windows_task_name}",
            )
        else:
            QMessageBox.warning(
                self, "Task Registration Failed",
                schedule.task_error or "Windows rejected task registration.",
            )
        self.reload()

    # -- actions ---------------------------------------------------------

    def _confirm_action(
        self, schedule: FlowSchedule, action_key: str, action_text: str, *,
        strong: bool = False, allow_do_not_ask: bool = False, detail: str = "",
    ) -> bool:
        setting_key = f"schedule_dialog/skip_confirmation/{action_key}"
        if allow_do_not_ask and self.settings is not None and self.settings.value(
            setting_key, False, type=bool,
        ):
            return True
        box = QMessageBox(self)
        box.setWindowTitle(f"Confirm {action_text}")
        box.setIcon(QMessageBox.Warning if strong else QMessageBox.Question)
        box.setText(f"{action_text} for '{schedule.flow_name}'?")
        if detail:
            box.setInformativeText(detail)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        box.setEscapeButton(QMessageBox.Cancel)
        yes_button = box.button(QMessageBox.Yes)
        if yes_button is not None:
            yes_button.setText(action_text)
        checkbox = None
        if allow_do_not_ask:
            checkbox = QCheckBox(f"Do not ask again for {action_text}")
            box.setCheckBox(checkbox)
        confirmed = box.exec() == QMessageBox.Yes
        if confirmed and checkbox is not None and checkbox.isChecked() and self.settings is not None:
            self.settings.setValue(setting_key, True)
        return confirmed

    def _resolve_schedule(self, identifier: str) -> FlowSchedule | None:
        by_id = self.store.get_by_id(identifier)
        if by_id is not None:
            return by_id
        return self.store.get(identifier) if identifier in self.store.list_flow_names() else None

    def _project_json(self, schedule: FlowSchedule) -> Path:
        return (self.store.flows_root / schedule.flow_name / "project.json").resolve()

    def _sync_task(self, schedule: FlowSchedule) -> bool:
        self._invalidate_background_refresh()
        if self.task_registrar is None:
            return True
        result = self.task_registrar.sync(schedule, self._project_json(schedule))
        self.store.mark_task_registration_attempted(schedule.schedule_id)
        schedule.task_status = result.status
        schedule.task_error = result.error
        schedule.windows_task_name = result.task_name
        self.store.set(schedule)
        self.store.save()
        if result.ok:
            self.task_log.emit(
                f"[Scheduler] {result.status}: {result.task_name}"
            )
        else:
            self.task_log.emit(
                f"[Scheduler] Registration failed for {result.task_name}: {result.error}"
            )
        return result.ok

    def _invalidate_background_refresh(self) -> None:
        """Ignore an older read after a local authoritative edit."""
        if self._refresh_in_progress:
            self._refresh_generation += 1
            self._refresh_in_progress = False

    def _query_task_status(self, schedule: FlowSchedule) -> None:
        if self.task_registrar is None:
            return
        result = self.task_registrar.query(schedule)
        schedule.task_status = result.status
        schedule.task_error = result.error
        schedule.windows_task_name = result.task_name

    def _add_schedule(self) -> None:
        flow_name = self._selected_flow_name
        if not flow_name:
            names = self.store.list_flow_names()
            flow_name = names[0] if names else None
        if not flow_name:
            QMessageBox.information(self, "Add Schedule", "Create and save a flow before adding a schedule.")
            return
        schedule = self.store.create_schedule(flow_name)
        self.store.set(schedule)
        self.store.save()
        self._sync_task(schedule)
        self._selected_schedule_id = schedule.schedule_id
        self.reload()

    def _delete_schedule(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        if not self._confirm_action(
            schedule, "delete", "Delete Schedule", strong=True,
            detail=(f"Schedule {schedule.schedule_id} and its Windows task will be removed. "
                    "Run history stored with the flow is not deleted."),
        ):
            return
        self._invalidate_background_refresh()
        if self.task_registrar is not None:
            result = self.task_registrar.delete(schedule)
            if not result.ok:
                schedule.task_status = result.status
                schedule.task_error = result.error
                self.store.set(schedule)
                self.store.save()
                self.task_log.emit(f"[Scheduler] Task deletion failed for {result.task_name}: {result.error}")
                QMessageBox.warning(self, "Task Deletion Failed", result.error or "The Windows task could not be removed.")
                return
            self.task_log.emit(f"[Scheduler] Removed Windows task: {result.task_name}")
        self.store.remove_schedule(schedule.schedule_id)
        self.store.save()
        self._selected_schedule_id = None
        self.reload()

    def _test_schedule(self, identifier: str, *, already_confirmed: bool = False) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None or self.task_registrar is None:
            QMessageBox.information(self, "Test Run", "Windows task execution is unavailable in this session.")
            return
        if not already_confirmed and not self._confirm_action(
            schedule, "test_run", "Test Run",
            detail="This launches the exact standalone command registered for the Windows task.",
        ):
            return
        result = self.task_registrar.test_run(schedule, self._project_json(schedule))
        if result.ok:
            command = " ".join(result.command or [])
            self.task_log.emit(f"[Scheduler] Test Run launched: {command}")
            QMessageBox.information(
                self, "Test Run Started",
                "The standalone scheduled runner was started with the same command used by Windows Task Scheduler.",
            )
        else:
            self.task_log.emit(f"[Scheduler] Test Run failed: {result.error}")
            QMessageBox.warning(self, "Test Run Failed", result.error or "The runner could not be launched.")

    def _run_schedule_now(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        if not self._confirm_action(
            schedule, "run_now", "Run Now", allow_do_not_ask=True,
            detail="The flow will start immediately; its automatic schedule is unchanged.",
        ):
            return
        if self.task_registrar is not None:
            self._test_schedule(schedule.schedule_id, already_confirmed=True)
        else:
            self.run_now_requested.emit(schedule.flow_name)

    def _set_interval(self, identifier: str, minutes: int) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        schedule.interval_minutes = minutes
        if schedule.enabled and not schedule.paused:
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self._sync_task(schedule)
        self.reload()

    def _toggle_pause(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        if not schedule.enabled:
            return
        action = "Resume Schedule" if schedule.paused else "Pause Schedule"
        if not self._confirm_action(
            schedule, "pause_resume", action, allow_do_not_ask=True,
            detail=("Future automatic runs will resume." if schedule.paused
                    else "Automatic runs are suspended until this schedule is resumed."),
        ):
            return
        schedule.paused = not schedule.paused
        if not schedule.paused:
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self._sync_task(schedule)
        self.reload()

    def _toggle_enabled(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        action = "Disable Schedule" if schedule.enabled else "Enable Schedule"
        if not self._confirm_action(
            schedule, "enable_disable", action,
            detail=("It will no longer run automatically until re-enabled."
                    if schedule.enabled else "Automatic runs will be registered and resumed."),
        ):
            return
        if schedule.enabled:
            schedule.enabled = False
            schedule.paused = False
        else:
            schedule.enabled = True
            schedule.paused = False
            schedule_next_run(schedule)
        self.store.set(schedule)
        self.store.save()
        self._sync_task(schedule)
        self.reload()

    def _show_details(self, identifier: str) -> None:
        schedule = self._resolve_schedule(identifier)
        if schedule is None:
            return
        for row in range(self.table.rowCount()):
            if self._schedule_id_for_row(row) == schedule.schedule_id:
                self.table.setCurrentCell(row, COLUMN_FLOW)
                self.table.setFocus()
                return
        self._selected_flow_name = schedule.flow_name
        self._selected_schedule_id = schedule.schedule_id
        self._show_schedule_details(schedule)

    def _show_help(self) -> None:
        QMessageBox.information(self, "Schedule Flows Help", HELP_TEXT)
