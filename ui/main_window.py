from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import json
import shutil
from datetime import datetime, timezone
from html import escape
import os
import time
import sys
import shiboken6
import weakref
import threading
from uuid import uuid4

from PySide6.QtCore import QItemSelectionModel, QObject, QPoint, QRect, QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeyEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QDoubleSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from rpa.generator import generate_python
from rpa.control_flow import BLOCK_OPENERS, IF_TYPES, LOOP_TYPES, CONTROL_TYPES, NON_EXECUTABLE_TYPES, parse_control_flow
from rpa.evidence import RunEvidenceSession
from rpa.execution import COMPLETED_UNVERIFIED
from rpa.execution import FAILED as EXECUTION_FAILED, STOPPED_BY_USER
from rpa.image_matcher import find_image, find_reference_matches, save_crop_from_image, screenshot_image, virtual_screen_origin
from rpa.models import ActionType, ProjectSettings, RecorderState, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.recorder import RpaRecorder
from rpa.runner import ReplayActionError, ReplayRunner, StopReplay
from rpa.scheduler import (
    STATUS_FAILED,
    STATUS_SKIPPED_BUSY,
    STATUS_SKIPPED_RUNNING,
    STATUS_STOPPED,
    STATUS_SUCCESS,
    ScheduleStore,
    is_due,
    mark_finished,
    mark_skipped,
    mark_started,
)
from rpa.validator import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    ValidationIssue,
    validate_project_detailed,
)
from rpa.variables import (
    mask_sensitive_text, prepare_runtime_variables, sensitive_variable_names,
    validate_variable_configuration,
)
from rpa.utils import foreground_elevation_mismatch
from rpa.windowing import NativeWindowBackend
from rpa.windows_tasks import WindowsTaskRegistrar, reconcile_schedules
from rpa.step_editing import (
    clipboard_payload, complete_contiguous_selection, delete_steps, jump_targets,
    paste_payload, reorder_steps, restore_jump_targets, validate_structure,
)
from ui.action_editor import ActionEditor
from ui.action_table import ActionTable
from ui.dialogs import ManualActionDialog, SettingsDialog, VariablesDialog, load_default_project_settings, show_error
from ui.recorder_toolbar import FloatingExecutionToolbar, FloatingRecorderToolbar
from ui.schedule_dialog import ScheduleFlowsDialog
from ui.run_details_dialog import RunDetailsDialog
from ui.runtime_inputs_dialog import RuntimeInputsDialog
from ui.target_capture import TargetCaptureOverlay
from ui.window_picker import WindowPickOverlay
from ui.image_match_debug import MatchHighlightOverlay, MatchResultsDialog
from ui.region_selector import RegionSelectionOverlay
from ui.debug_variables_dialog import DebugVariablesDialog


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def flows_root() -> Path:
    root = app_root() / "flows"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_flow_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "-", "_") else "_" for ch in name.strip())
    cleaned = "_".join(cleaned.split())
    return cleaned.strip("._- ")


class ReplayWorker(QObject):
    action_status = Signal(int, str)
    retry_progress = Signal(int, int, int, str)
    control_progress = Signal(int, str)
    log = Signal(str)
    finished = Signal()
    failed = Signal(int, str)
    stopped = Signal()
    debug_paused = Signal(int, str, object)
    attention_requested = Signal(object)

    def __init__(
        self,
        project: RpaProject,
        project_dir: Path,
        start_index: int = 0,
        end_index: int | None = None,
        include_start_delay: bool = True,
        respect_enabled: bool = True,
        runtime_variables: dict | None = None,
        excluded_regions: list[tuple[int, int, int, int]] | None = None,
        evidence_dir: Path | None = None,
        enable_debug: bool = False,
    ) -> None:
        super().__init__()
        self.runner = ReplayRunner(project, project_dir, self.log.emit, excluded_regions, evidence_dir)
        self.start_index = start_index
        self.end_index = end_index
        self.include_start_delay = include_start_delay
        self.respect_enabled = respect_enabled
        self.enable_debug = enable_debug
        self.runner.set_attention_callback(self.attention_requested.emit)
        if runtime_variables is not None:
            self.runner.runtime_variables = dict(runtime_variables)
            self.runner.execution_context.variables = self.runner.runtime_variables

    @Slot()
    def run(self) -> None:
        try:
            self.runner.run(
                self.action_status.emit,
                self.start_index,
                self.end_index,
                self.include_start_delay,
                self.respect_enabled,
                self.retry_progress.emit,
                self.control_progress.emit,
                self.debug_paused.emit,
                self.enable_debug,
            )
            if self.runner.had_continued_failures:
                self.failed.emit(
                    self.runner.first_failed_index if self.runner.first_failed_index is not None else -1,
                    self.runner.first_failure_error or "One or more steps failed",
                )
            else:
                self.finished.emit()
        except StopReplay:
            self.log.emit("replay stopped")
            self.stopped.emit()
        except ReplayActionError as exc:
            self.failed.emit(exc.index, str(exc))
        except Exception as exc:
            self.failed.emit(-1, str(exc))

    def stop(self) -> None:
        self.runner.request_stop()

    def debug_resume(self) -> None:
        self.runner.resume_debug()

    def debug_step_over(self) -> None:
        self.runner.step_over_debug()

    def debug_skip(self) -> None:
        self.runner.skip_debug_step()

    def debug_restart(self, index: int) -> None:
        self.runner.restart_debug_from(index)

    def debug_update_variables(self, values: dict) -> None:
        self.runner.update_debug_variables(values)

    def submit_attention_decision(self, decision: str) -> None:
        self.runner.submit_attention_decision(decision)


class MainWindow(QMainWindow):
    action_recorded = Signal(object)
    log_recorded = Signal(str)
    recorder_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Python RPA Recorder")
        self.resize(1160, 760)
        self.setMinimumWidth(1120)
        self.manager = ProjectManager()
        self.project = self.manager.new_project(settings=load_default_project_settings())
        self.project_dir: Path | None = None
        self.dirty = False
        self.recorder: RpaRecorder | None = None
        self.floating: FloatingRecorderToolbar | None = None
        self.execution_floating: FloatingExecutionToolbar | None = None
        self._active_run_settings: ProjectSettings | None = None
        self.replay_was_maximized = False
        self.replay_thread: QThread | None = None
        self.replay_worker: ReplayWorker | None = None
        self.run_log_path: Path | None = None
        self.active_evidence: RunEvidenceSession | None = None
        self.last_evidence_folder: Path | None = None
        self._last_validation_issues: list[ValidationIssue] = []
        self._active_history_flow: str | None = None
        self._active_secret_values: set[str] = set()
        self.recording_started_at: float | None = None
        self.recording_start_action_count = 0
        self.recording_was_dirty = False
        self.recording_preparing = False
        self.recording_prepare_seconds = 3
        self._history: list[dict] = []
        self._history_index = -1
        self._restoring_history = False
        self.warned_recording_permission_pids: set[int] = set()
        self.recording_permission_timer = QTimer(self)
        self.recording_permission_timer.setInterval(1000)
        self.recording_permission_timer.timeout.connect(self._check_recording_permissions)
        self.running_action_index: int | None = None
        self.run_started_at: float | None = None
        self.run_start_index = 0
        self.run_end_index = -1
        self.run_mode = "run"
        self.file_logger = None
        self._logs_follow_tail = True
        self.last_runtime_variables: dict = {}
        self.debug_paused_index: int | None = None
        self.debug_paused_values: dict = {}
        self.debug_showed_main = False
        self.details_were_visible_before_run = True
        self.target_capture_overlay: TargetCaptureOverlay | None = None
        self.target_capture_action: RpaAction | None = None
        self.target_capture_origin = (0, 0)
        self.target_capture_was_maximized = False
        self.search_region_overlay: RegionSelectionOverlay | None = None
        self.search_region_action: RpaAction | None = None
        self.match_highlight_overlay: MatchHighlightOverlay | None = None
        self.manual_capture_dialog: ManualActionDialog | None = None
        self.manual_test_dialog: ManualActionDialog | None = None
        self._manual_test_action_id: str | None = None
        self.manual_capture_role = "target"
        self.window_pick_overlay: WindowPickOverlay | None = None
        self.manual_capture_snapshot: dict = {}
        self._manual_capture_token = 0
        self._manual_capture_timer = QTimer(self)
        self._manual_capture_timer.setSingleShot(True)
        self._manual_capture_timer.timeout.connect(self._start_pending_manual_capture)
        self.settings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        try:
            schedule_history_limit = int(self.settings.value("scheduler/history_limit", 100))
        except (TypeError, ValueError):
            schedule_history_limit = 100
        self.schedule_store = ScheduleStore(flows_root(), history_limit=schedule_history_limit)
        self.windows_task_registrar = WindowsTaskRegistrar() if sys.platform == "win32" else None
        self._scheduled_runs: dict[str, tuple[QThread, ReplayWorker]] = {}
        self._schedule_queue: list[str] = []
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(15000)
        self.schedule_timer.timeout.connect(self._check_schedules)
        if sys.platform != "win32":
            # Windows schedules are owned by Task Scheduler and must not also
            # be polled by the GUI process.
            self.schedule_timer.start()
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()
        self.refresh()
        self._reset_history()
        self._restore_layout_settings()
        self._open_last_project()

    def _build_ui(self) -> None:
        self.toolbar = QToolBar()
        self.addToolBar(self.toolbar)
        self.buttons: dict[str, QPushButton] = {}
        self.menu_actions: dict[str, QAction] = {}
        self.toolbar.setMovable(False)
        self.toolbar.setStyleSheet("QToolBar { background: #f0f3f7; spacing: 8px; padding: 6px; }")
        groups = [
            ("Recording", [("Record", "● Record"), ("Pause", "Pause"), ("Resume", "Resume"), ("Stop", "■ Stop")]),
            ("Execution", [("Run", "▶ Run"), ("Stop Run", "Stop Run"), ("Schedule Flows", "⏱"), ("Generate Python", "Generate")]),
            ("Review", [("Validate Flow", "Validate"), ("Add Manual Action", "+ Add Step"), ("Insert Before", "Insert Before"), ("Insert After", "Insert After"), ("Duplicate", "⧉ Duplicate"), ("Delete Action", "Delete"), ("Move Up", "↑"), ("Move Down", "↓"), ("Deselect All", "Deselect"), ("Variables", "Variables"), ("Settings", "Settings")]),
        ]
        groups[2] = (groups[2][0], [item for item in groups[2][1] if item[0] != "Settings"])
        groups[2][1].insert(7, ("Enable/Disable", "Enable/Disable"))
        # Keep the toolbar focused on the most common step actions. Insertion,
        # reordering and deselection remain available in the Step Editing menu
        # and the table context menu, where they are easier to discover without
        # turning the primary workspace into a wall of buttons.
        groups[2] = (groups[2][0], [item for item in groups[2][1] if item[0] in (
            "Validate Flow", "Add Manual Action", "Duplicate", "Delete Action", "Enable/Disable", "Variables",
        )])
        compact_labels = {
            "Add Manual Action": "+ Add",
            "Insert Before": "Before",
            "Insert After": "After",
            "Move Up": "Up",
            "Move Down": "Down",
            "Enable/Disable": "Enable",
        }
        for group_index, (group_title, group) in enumerate(groups):
            target_toolbar = self.toolbar
            if group_title == "Review":
                self.addToolBarBreak()
                self.edit_toolbar = QToolBar()
                self.edit_toolbar.setMovable(False)
                self.edit_toolbar.setStyleSheet("QToolBar { background: #f8fafc; spacing: 8px; padding: 4px 6px; }")
                self.addToolBar(self.edit_toolbar)
                target_toolbar = self.edit_toolbar
            elif group_index:
                target_toolbar.addSeparator()
            label = QLabel(group_title)
            label.setStyleSheet("font-weight: 700; color: #334155; margin-left: 4px; margin-right: 4px;")
            target_toolbar.addWidget(label)
            for name, text in group:
                text = compact_labels.get(name, text)
                btn = QPushButton(text)
                btn.setToolTip(name)
                btn.setAccessibleName(name)
                target_toolbar.addWidget(btn)
                self.buttons[name] = btn
        self._build_menu_bar()
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter steps")
        self.filter_box.setClearButtonEnabled(True)
        self.table = ActionTable()
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch(1)
        empty_title = QLabel("No automation recorded yet")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_title.setStyleSheet("font-size: 18px; font-weight: 700; color: #1f2937;")
        empty_subtitle = QLabel("Record your first automation, or open one you already created.")
        empty_subtitle.setAlignment(Qt.AlignCenter)
        empty_subtitle.setStyleSheet("color: #64748b;")
        empty_buttons = QHBoxLayout()
        empty_buttons.addStretch(1)
        self.empty_record_btn = QPushButton("Start Recording")
        self.empty_open_btn = QPushButton("Open Automation")
        self.empty_record_btn.setMinimumHeight(34)
        self.empty_open_btn.setMinimumHeight(34)
        self.empty_record_btn.setStyleSheet("background: #dc2626; color: white; font-weight: 700; padding: 6px 14px;")
        empty_buttons.addWidget(self.empty_record_btn)
        empty_buttons.addWidget(self.empty_open_btn)
        empty_buttons.addStretch(1)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_subtitle)
        empty_layout.addLayout(empty_buttons)
        empty_layout.addStretch(2)
        table_wrap = QWidget()
        table_layout = QVBoxLayout(table_wrap)
        table_heading = QLabel("Steps")
        table_heading.setStyleSheet("font-size: 14px; font-weight: 700;")
        table_layout.addWidget(table_heading)
        table_layout.addWidget(self.filter_box)
        table_layout.addWidget(self.table)
        table_layout.addWidget(self.empty_state)
        self.editor = ActionEditor()
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setWidget(self.editor)
        self.editor_scroll.setMinimumWidth(320)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setFont(QFont("Consolas", 10))
        self.clear_logs_btn = QPushButton("Clear")
        self.copy_logs_btn = QPushButton("Copy")
        self.save_logs_btn = QPushButton("Save Log")
        self.open_log_btn = QPushButton("Open File")
        self.run_details_btn = QPushButton("Run Details")
        self.toggle_logs_btn = QPushButton("Collapse Logs")
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("Search logs")
        self.log_search.setClearButtonEnabled(True)
        self.log_search.setMaximumWidth(220)
        logs_header = QWidget()
        logs_header_layout = QHBoxLayout(logs_header)
        logs_header_layout.setContentsMargins(0, 0, 0, 0)
        logs_header_layout.addWidget(QLabel("Logs / Status"))
        logs_header_layout.addStretch(1)
        logs_header_layout.addWidget(self.log_search)
        for btn in (self.clear_logs_btn, self.copy_logs_btn, self.save_logs_btn, self.open_log_btn, self.run_details_btn, self.toggle_logs_btn):
            logs_header_layout.addWidget(btn)
        self.logs_wrap = QWidget()
        logs_layout = QVBoxLayout(self.logs_wrap)
        logs_layout.addWidget(logs_header)
        logs_layout.addWidget(self.logs)
        self.validation_wrap = QWidget()
        validation_layout = QVBoxLayout(self.validation_wrap)
        validation_layout.setContentsMargins(6, 6, 6, 6)
        validation_header = QHBoxLayout()
        validation_title = QLabel("Flow Validation")
        validation_title.setStyleSheet("font-weight: 700;")
        self.validation_summary = QLabel("Validate the flow to check whether it is ready to run.")
        self.validation_summary.setStyleSheet("color: #64748b;")
        validation_header.addWidget(validation_title)
        validation_header.addWidget(self.validation_summary, 1)
        validation_layout.addLayout(validation_header)
        self.validation_table = QTableWidget(0, 4)
        self.validation_table.setHorizontalHeaderLabels(["Level", "Step", "Step name", "Reason"])
        self.validation_table.verticalHeader().setVisible(False)
        self.validation_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.validation_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.validation_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.validation_table.setAlternatingRowColors(True)
        self.validation_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.validation_table.setColumnWidth(0, 90)
        self.validation_table.setColumnWidth(1, 65)
        self.validation_table.setColumnWidth(2, 190)
        self.validation_table.setToolTip("Double-click a result to select its step")
        validation_layout.addWidget(self.validation_table)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.logs_wrap, "Logs / Status")
        self.bottom_tabs.addTab(self.validation_wrap, "Validation")
        self.bottom_tabs.setMinimumHeight(220)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.addWidget(table_wrap)
        self.workspace_splitter.addWidget(self.editor_scroll)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 2)
        self.vertical_splitter = QSplitter(Qt.Vertical)
        self.vertical_splitter.addWidget(self.workspace_splitter)
        self.vertical_splitter.addWidget(self.bottom_tabs)
        self.vertical_splitter.setStretchFactor(0, 3)
        self.vertical_splitter.setStretchFactor(1, 2)
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(10, 8, 10, 8)
        self.workflow_buttons: dict[str, QPushButton] = {}
        workflow = QWidget()
        workflow_layout = QHBoxLayout(workflow)
        workflow_layout.setContentsMargins(0, 0, 0, 4)
        workflow_layout.setSpacing(8)
        for number, label in enumerate(("Record", "Review", "Test", "Run"), start=1):
            button = QPushButton(f"{number}. {label}")
            button.setMinimumHeight(36)
            button.setStyleSheet(
                "QPushButton { text-align: left; padding: 7px 14px; font-weight: 600; "
                "background: #f8fafc; border: 1px solid #d8dee8; }"
            )
            workflow_layout.addWidget(button)
            self.workflow_buttons[label] = button
        self.workflow_buttons["Record"].setStyleSheet(
            "QPushButton { text-align: left; padding: 7px 14px; font-weight: 700; "
            "background: #fff1f2; color: #991b1b; border: 1px solid #fecaca; }"
        )
        self.workflow_buttons["Run"].setStyleSheet(
            "QPushButton { text-align: left; padding: 7px 14px; font-weight: 700; "
            "background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }"
        )
        layout.addWidget(workflow)
        layout.addWidget(self.vertical_splitter)
        self.setCentralWidget(main)
        self.statusBar().showMessage("Ready")
        self.vertical_splitter.setSizes([430, 330])

    def _build_menu_bar(self) -> None:
        menus = [
            ("File", ["New", "Open", "Save", "Save As"]),
            ("Record Actions", ["Record", "Pause", "Resume", "Stop"]),
            ("Execution", ["Run", "Run Until Breakpoint", "Validate Flow", "Test This Step", "Run From Here", "Run Until Here", "Stop Run", "Schedule Flows", "Generate Python"]),
            ("Step Editing", ["Undo", "Redo", "Copy", "Cut", "Paste", "Toggle Breakpoint", "Add Manual Action", "Add Comment", "Group Selected", "Move Into Group", "Move Out of Group", "Insert Before", "Insert After", "Duplicate", "Delete Action", "Move Up", "Move Down", "Enable Selected", "Disable Selected", "Adjust Wait Before", "Enable/Disable", "Deselect All"]),
            ("Project", ["Variables", "Settings"]),
        ]
        for menu_name, names in menus:
            menu = self.menuBar().addMenu(menu_name)
            for name in names:
                action = QAction(self._menu_label(name), self)
                menu.addAction(action)
                self.menu_actions[name] = action

    def _menu_label(self, name: str) -> str:
        shortcut = {
            "New": "Ctrl+N",
            "Open": "Ctrl+O",
            "Save": "Ctrl+S",
            "Run": "F5",
            "Stop Run": "Shift+F5",
            "Generate Python": "Ctrl+G",
            "Delete Action": "Delete",
            "Deselect All": "Esc",
            "Undo": "Ctrl+Z",
            "Redo": "Ctrl+Y",
            "Copy": "Ctrl+C",
            "Cut": "Ctrl+X",
            "Paste": "Ctrl+V",
            "Duplicate": "Ctrl+D",
            "Toggle Breakpoint": "F9",
        }.get(name, "")
        friendly = {
            "Add Manual Action": "Add Step",
            "Delete Action": "Delete Step",
        }.get(name, name)
        return f"{friendly}\t{shortcut}" if shortcut else friendly

    def _connect_signals(self) -> None:
        self.menu_actions["New"].triggered.connect(self.new_project)
        self.menu_actions["Open"].triggered.connect(self.open_project)
        self.menu_actions["Save"].triggered.connect(self.save_project)
        self.menu_actions["Save As"].triggered.connect(self.save_as_project)
        self.menu_actions["Undo"].triggered.connect(self.undo)
        self.menu_actions["Redo"].triggered.connect(self.redo)
        self.menu_actions["Copy"].triggered.connect(self.copy_steps)
        self.menu_actions["Cut"].triggered.connect(self.cut_steps)
        self.menu_actions["Paste"].triggered.connect(self.paste_steps)
        self.buttons["Record"].clicked.connect(self.start_recording)
        self.menu_actions["Record"].triggered.connect(self.start_recording)
        self.buttons["Pause"].clicked.connect(self.pause_recording)
        self.menu_actions["Pause"].triggered.connect(self.pause_recording)
        self.buttons["Resume"].clicked.connect(self.resume_recording)
        self.menu_actions["Resume"].triggered.connect(self.resume_recording)
        self.buttons["Stop"].clicked.connect(self.stop_recording)
        self.menu_actions["Stop"].triggered.connect(self.stop_recording)
        self.buttons["Run"].clicked.connect(self.run_project)
        self.menu_actions["Run"].triggered.connect(self.run_project)
        self.menu_actions["Run Until Breakpoint"].triggered.connect(self.run_until_breakpoint)
        self.buttons["Validate Flow"].clicked.connect(self.validate_flow)
        self.menu_actions["Validate Flow"].triggered.connect(self.validate_flow)
        self.menu_actions["Test This Step"].triggered.connect(self.test_selected_step)
        self.menu_actions["Run From Here"].triggered.connect(self.run_from_here)
        self.menu_actions["Run Until Here"].triggered.connect(self.run_until_here)
        self.buttons["Stop Run"].clicked.connect(self.stop_run)
        self.menu_actions["Stop Run"].triggered.connect(self.stop_run)
        self.buttons["Schedule Flows"].clicked.connect(self.schedule_flows_dialog)
        self.menu_actions["Schedule Flows"].triggered.connect(self.schedule_flows_dialog)
        self.buttons["Generate Python"].clicked.connect(self.generate_python)
        self.menu_actions["Generate Python"].triggered.connect(self.generate_python)
        self.buttons["Add Manual Action"].clicked.connect(self.add_manual_action)
        self.menu_actions["Add Manual Action"].triggered.connect(self.add_manual_action)
        self.menu_actions["Insert Before"].triggered.connect(lambda: self.add_manual_action("before"))
        self.menu_actions["Insert After"].triggered.connect(lambda: self.add_manual_action("after"))
        self.buttons["Duplicate"].clicked.connect(self.duplicate_action)
        self.menu_actions["Duplicate"].triggered.connect(self.duplicate_action)
        self.buttons["Delete Action"].clicked.connect(self.delete_action)
        self.menu_actions["Delete Action"].triggered.connect(self.delete_action)
        self.menu_actions["Move Up"].triggered.connect(lambda: self.move_action(-1))
        self.menu_actions["Move Down"].triggered.connect(lambda: self.move_action(1))
        self.buttons["Enable/Disable"].clicked.connect(self.toggle_selected_action)
        self.menu_actions["Enable/Disable"].triggered.connect(self.toggle_selected_action)
        self.menu_actions["Toggle Breakpoint"].triggered.connect(self.toggle_breakpoint)
        self.menu_actions["Add Comment"].triggered.connect(self.add_comment)
        self.menu_actions["Group Selected"].triggered.connect(self.group_selected_steps)
        self.menu_actions["Move Into Group"].triggered.connect(self.move_selected_into_group)
        self.menu_actions["Move Out of Group"].triggered.connect(self.move_selected_out_of_group)
        self.menu_actions["Enable Selected"].triggered.connect(lambda: self.set_selected_enabled(True))
        self.menu_actions["Disable Selected"].triggered.connect(lambda: self.set_selected_enabled(False))
        self.menu_actions["Adjust Wait Before"].triggered.connect(self.adjust_selected_wait)
        self.menu_actions["Deselect All"].triggered.connect(self.clear_step_selection)
        self.menu_actions["Variables"].triggered.connect(self.variables_dialog)
        self.buttons["Variables"].clicked.connect(self.variables_dialog)
        self.menu_actions["Settings"].triggered.connect(self.settings_dialog)
        self.empty_record_btn.clicked.connect(self.start_recording)
        self.empty_open_btn.clicked.connect(self.open_project)
        self.workflow_buttons["Record"].clicked.connect(self.start_recording)
        self.workflow_buttons["Review"].clicked.connect(lambda: self.table.setFocus(Qt.OtherFocusReason))
        self.workflow_buttons["Test"].clicked.connect(self.test_selected_step)
        self.workflow_buttons["Run"].clicked.connect(self.run_project)
        self.filter_box.textChanged.connect(self.table.apply_filter)
        self.table.itemSelectionChanged.connect(self.select_action)
        self.table.itemDoubleClicked.connect(lambda _: self.editor.focus_main_field())
        self.table.empty_area_clicked.connect(self.clear_step_selection)
        self.table.context_action_requested.connect(self.handle_table_context_action)
        self.table.reorder_requested.connect(self.reorder_selected_steps)
        self.table.structure_changed.connect(self.mark_dirty)
        self.editor.action_changed.connect(self.mark_dirty)
        self.editor.close_requested.connect(self.clear_step_selection)
        self.editor.test_step_requested.connect(self.test_selected_step)
        self.editor.test_locator_requested.connect(self.test_target)
        self.editor.recapture_requested.connect(self.recapture_target)
        self.editor.search_region_requested.connect(self.select_image_search_region)
        self.editor.open_subflow_requested.connect(self._open_referenced_subflow)
        self.editor.advanced_changed.connect(lambda expanded: self.settings.setValue("advanced_expanded", expanded))
        self.clear_logs_btn.clicked.connect(self.logs.clear)
        self.copy_logs_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.logs.toPlainText()))
        self.save_logs_btn.clicked.connect(self.save_logs)
        self.open_log_btn.clicked.connect(self.open_run_log)
        self.run_details_btn.clicked.connect(self.open_run_details)
        self.toggle_logs_btn.clicked.connect(self.toggle_logs)
        self.log_search.returnPressed.connect(self.find_log)
        self.logs.verticalScrollBar().valueChanged.connect(self._on_log_scroll)
        self.validation_table.itemDoubleClicked.connect(self._validation_result_activated)
        self.action_recorded.connect(self._action_recorded)
        self.log_recorded.connect(self.log)
        self.recorder_failed.connect(self._recorder_failed)

    def refresh(self) -> None:
        self.editor.set_available_variables(
            set(self.project.variables)
            | set(self.project.runtime_inputs)
            | set(self.project.output_variables)
            | {"RUN_DATE", "CLIPBOARD_TEXT", "LAST_CLICK_X", "LAST_CLICK_Y"}
        )
        self.table.set_actions(self.project.actions)
        self.table.apply_filter(self.filter_box.text())
        has_actions = bool(self.project.actions)
        self.table.setVisible(has_actions)
        self.empty_state.setVisible(not has_actions)
        self.select_action()
        self.update_buttons()
        self.update_status()

    def update_buttons(self) -> None:
        recording = self.recorder is not None and self.recorder.state == RecorderState.RECORDING
        paused = self.recorder is not None and self.recorder.state == RecorderState.PAUSED
        preparing = self.recording_preparing
        running = self.replay_thread is not None
        self.buttons["Pause"].setEnabled(recording)
        self.buttons["Resume"].setEnabled(paused)
        self.buttons["Stop"].setEnabled(recording or paused or preparing)
        # Only show the recording-state command that can be used now. This
        # keeps the primary toolbar compact enough for execution controls.
        self.buttons["Pause"].setVisible(recording)
        self.buttons["Resume"].setVisible(paused)
        self.buttons["Stop"].setVisible(recording or paused or preparing)
        self.buttons["Run"].setEnabled(bool(self.project.actions) and not recording and not paused and not preparing and not running)
        self.buttons["Validate Flow"].setEnabled(bool(self.project.actions) and not recording and not paused and not preparing and not running)
        self.menu_actions["Validate Flow"].setEnabled(self.buttons["Validate Flow"].isEnabled())
        self.buttons["Record"].setEnabled(not running and not recording and not paused and not preparing)
        self.buttons["Stop Run"].setEnabled(running)
        for name in ("Pause", "Resume", "Stop", "Run", "Record", "Stop Run"):
            self.menu_actions[name].setEnabled(self.buttons[name].isEnabled())
        selected = self.table.selected_index() >= 0
        for name in ("Copy", "Cut", "Insert Before", "Insert After", "Duplicate", "Delete Action", "Move Up", "Move Down", "Enable Selected", "Disable Selected", "Adjust Wait Before", "Enable/Disable", "Group Selected", "Move Into Group", "Move Out of Group", "Deselect All"):
            if name in self.buttons:
                self.buttons[name].setEnabled(selected)
            self.menu_actions[name].setEnabled(selected)
        for name in ("Test This Step", "Run From Here", "Run Until Here"):
            self.menu_actions[name].setEnabled(selected and not recording and not paused and not running)
        self.menu_actions["Run Until Breakpoint"].setEnabled(
            bool(self.project.actions) and not recording and not paused and not preparing and not running
        )
        self.menu_actions["Toggle Breakpoint"].setEnabled(selected and not recording and not paused and not running)
        self.menu_actions["Undo"].setEnabled(self._history_index > 0 and not recording and not paused and not preparing and not running)
        self.menu_actions["Redo"].setEnabled(self._history_index + 1 < len(self._history) and not recording and not paused and not preparing and not running)
        self.workflow_buttons["Record"].setEnabled(self.buttons["Record"].isEnabled())
        self.workflow_buttons["Review"].setEnabled(bool(self.project.actions))
        self.workflow_buttons["Test"].setEnabled(selected and not running and not recording and not paused)
        self.workflow_buttons["Run"].setEnabled(self.buttons["Run"].isEnabled())
        self._update_command_styles(recording, paused, running)

    def _update_command_styles(self, recording: bool, paused: bool, running: bool) -> None:
        for button in self.buttons.values():
            button.setStyleSheet("")

        if recording:
            self.toolbar.setStyleSheet("QToolBar { background: #fff1f1; border-bottom: 1px solid #e6b8b8; }")
            self.buttons["Pause"].setStyleSheet("QPushButton { background: #f59e0b; color: #111827; font-weight: 600; }")
            self.buttons["Stop"].setStyleSheet("QPushButton { background: #dc2626; color: white; font-weight: 600; }")
        elif paused:
            self.toolbar.setStyleSheet("QToolBar { background: #fff7df; border-bottom: 1px solid #e8c76e; }")
            self.buttons["Resume"].setStyleSheet("QPushButton { background: #16a34a; color: white; font-weight: 600; }")
            self.buttons["Stop"].setStyleSheet("QPushButton { background: #dc2626; color: white; font-weight: 600; }")
        elif running:
            self.toolbar.setStyleSheet("QToolBar { background: #eef6ff; border-bottom: 1px solid #b6d7f2; }")
            self.buttons["Stop Run"].setStyleSheet("QPushButton { background: #ea580c; color: white; font-weight: 600; }")
        else:
            self.toolbar.setStyleSheet("QToolBar { background: #f0f3f7; spacing: 8px; padding: 6px; }")
            self.buttons["Record"].setStyleSheet("QPushButton:enabled { background: #dc2626; color: white; font-weight: 600; }")
            self.buttons["Run"].setStyleSheet("QPushButton:enabled { background: #2563eb; color: white; font-weight: 600; }")

    def ensure_project_dir(self) -> bool:
        if self.project_dir:
            return True
        return self.create_new_flow()

    def new_project(self) -> None:
        if self.dirty and QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes?") != QMessageBox.Yes:
            return
        self.logs.clear()
        if not self.create_new_flow():
            return
        self.dirty = False
        self.refresh()

    def create_new_flow(self) -> bool:
        name, ok = QInputDialog.getText(self, "New Flow", "Flow name")
        if not ok:
            return False
        flow_name = sanitize_flow_name(name)
        if not flow_name:
            show_error(self, "Invalid flow name", "Enter a flow name using letters, numbers, spaces, dashes, or underscores.")
            return False
        flow_dir = flows_root() / flow_name
        if (flow_dir / "project.json").exists():
            if QMessageBox.question(self, "Flow exists", f"Open existing flow '{flow_name}'?") != QMessageBox.Yes:
                return False
            try:
                self.project = self.manager.load(flow_dir / "project.json")
                self.project_dir = flow_dir
                self._load_latest_evidence()
                self.dirty = False
                self._reset_history()
                self._remember_project_path()
                self.log(f"opened flow: {flow_dir}")
                return True
            except Exception as exc:
                show_error(self, "Open flow failed", str(exc))
                return False
        self.project = self.manager.new_project(flow_name, settings=load_default_project_settings())
        self.project_dir = flow_dir
        self._load_latest_evidence()
        self.manager.save(self.project, self.project_dir)
        self.dirty = False
        self._reset_history()
        self._remember_project_path()
        self.log(f"created flow: {flow_dir}")
        return True

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open project", str(flows_root()), "RPA Project (project.json)")
        if not path:
            return
        self.open_project_path(Path(path))

    def save_project(self) -> None:
        if not self.ensure_project_dir():
            return
        self.log(f"[Project Save] writing {len(self.project.actions)} steps to {self.project_dir}")
        self.manager.save(self.project, self.project_dir)
        self.dirty = False
        self._remember_project_path()
        self.log("project saved")
        self.update_status()

    def save_as_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save project as")
        if not folder:
            return
        self.manager.save_as(self.project, self.project_dir, Path(folder))
        self.project_dir = Path(folder)
        self._load_latest_evidence()
        self.dirty = False
        self._remember_project_path()
        self.log("project saved as")
        self.update_status()

    def start_recording(self) -> None:
        if not self.ensure_project_dir():
            return
        if self.recording_preparing or self.recorder:
            return
        self.recording_start_action_count = len(self.project.actions)
        self.recording_was_dirty = self.dirty
        self.recording_preparing = True
        self.floating = FloatingRecorderToolbar()
        self.floating.pause_requested.connect(self.pause_recording)
        self.floating.resume_requested.connect(self.resume_recording)
        self.floating.stop_requested.connect(self.stop_recording)
        self.floating.cancel_requested.connect(self.cancel_recording)
        self.hide()
        self.floating.show()
        self._position_floating_toolbar()
        if self.project.settings.show_desktop_before_recording:
            self._show_windows_desktop()
        self._begin_recording_countdown(self.recording_prepare_seconds)
        self.update_buttons()

    def _show_windows_desktop(self) -> None:
        """Minimize normal windows before hooks start; it never becomes an RPA step."""
        if sys.platform != "win32":
            return
        try:
            from rpa.desktop_lifecycle import show_windows_desktop

            show_windows_desktop()
        except Exception as exc:
            self.log(f"Could not show the desktop before recording or replay: {exc}")

    def _begin_recording_countdown(self, seconds: int) -> None:
        if not self.recording_preparing or not self.floating:
            return
        self.floating.set_preparing(seconds)
        self.update_status(f"Preparing recording ({seconds}s)")
        if seconds > 0:
            QTimer.singleShot(1000, lambda: self._begin_recording_countdown(seconds - 1))
            return
        try:
            self.recorder = RpaRecorder(
                self.project_dir,
                self.project.settings,
                self.action_recorded.emit,
                self.log_recorded.emit,
                on_error=self.recorder_failed.emit,
            )
            self.recorder.start()
            self.recording_preparing = False
            self.recording_started_at = time.monotonic()
            self.floating.set_recording()
            self.warned_recording_permission_pids.clear()
            self.recording_permission_timer.start()
            self.update_buttons()
            self.update_status("Recording")
        except Exception as exc:
            self.recorder = None
            self.recording_preparing = False
            self.recording_started_at = None
            self._finish_recording()
            show_error(self, "Recorder failed", str(exc))

    def pause_recording(self) -> None:
        if self.recorder:
            self.recorder.pause()
        if self.floating:
            self.floating.set_paused(True)
        self.update_buttons()
        self.update_status("Paused")

    def resume_recording(self) -> None:
        if self.recorder:
            self.recorder.resume()
        if self.floating:
            self.floating.set_paused(False)
        self.update_buttons()
        self.update_status("Recording")

    def stop_recording(self) -> None:
        if self.recording_preparing:
            self.cancel_recording()
            return
        before_count = self.recording_start_action_count
        if self.recorder:
            self.recorder.stop(True)
            self.remove_stop_click_action()
        self._finish_recording()
        captured = max(0, len(self.project.actions) - before_count)
        self.show_recording_summary(captured)

    def cancel_recording(self) -> None:
        if self.recorder:
            self.recorder.stop(False)
        self._discard_recording_session()
        self.recording_started_at = None
        self._finish_recording()

    def _discard_recording_session(self) -> None:
        if self.recording_start_action_count >= len(self.project.actions):
            return
        discarded = self.project.actions[self.recording_start_action_count:]
        if self.project_dir:
            project_root = self.project_dir.resolve()
            for action in discarded:
                image = action.data.get("image")
                if not image:
                    continue
                path = (self.project_dir / str(image)).resolve()
                if path.is_relative_to(project_root) and path.exists():
                    try:
                        path.unlink()
                    except OSError as exc:
                        self.log(f"Could not remove cancelled screenshot {path.name}: {exc}")
        del self.project.actions[self.recording_start_action_count:]
        self.dirty = self.recording_was_dirty
        self.log(f"discarded {len(discarded)} steps from cancelled recording")

    def _recorder_failed(self, message: str) -> None:
        if self.recorder:
            self.recorder.abort()
        self.log(f"recorder failed: {message}")
        self._finish_recording()
        show_error(self, "Recording Stopped", message)

    def _finish_recording(self) -> None:
        self.recording_permission_timer.stop()
        self.recording_preparing = False
        if self.floating:
            self.floating.close()
            self.floating = None
        self.recorder = None
        self.recording_started_at = None
        self.showNormal()
        self.refresh()

    def _check_recording_permissions(self) -> None:
        mismatch = foreground_elevation_mismatch()
        if not mismatch or mismatch[0] in self.warned_recording_permission_pids:
            return
        self.warned_recording_permission_pids.add(mismatch[0])
        self.log(f"Windows permission warning: {mismatch[1]}")
        QMessageBox.warning(self.floating or self, "Windows Permission Mismatch", mismatch[1])

    def show_recording_summary(self, captured: int) -> None:
        if captured <= 0:
            return
        recent = self.project.actions[-captured:]
        clicks = sum(1 for action in recent if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value, ActionType.CLICK_COORDINATE.value))
        text_inputs = sum(1 for action in recent if action.action == ActionType.TYPE_TEXT.value)
        hotkeys = sum(1 for action in recent if action.action == ActionType.HOTKEY.value)
        key_presses = sum(1 for action in recent if action.action == ActionType.PRESS_KEY.value)
        box = QMessageBox(self)
        box.setWindowTitle("Recording completed")
        box.setText(
            "Recording completed\n\n"
            f"{captured} steps captured\n"
            f"{clicks} clicks\n"
            f"{text_inputs} text inputs\n"
            f"{hotkeys} hotkeys\n"
            f"{key_presses} key presses"
        )
        review = box.addButton("Review Steps", QMessageBox.AcceptRole)
        run_now = box.addButton("Run Now", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is run_now:
            self.run_project()
        elif box.clickedButton() is review:
            self.table.setFocus(Qt.OtherFocusReason)

    def remove_stop_click_action(self) -> None:
        if not self.project.actions or not self.floating:
            return
        last = self.project.actions[-1]
        if len(self.project.actions) <= self.recording_start_action_count:
            return
        x = int(last.data.get("fallback_x", last.data.get("x", -1)))
        y = int(last.data.get("fallback_y", last.data.get("y", -1)))
        clicked_toolbar = self.floating.frameGeometry().contains(QPoint(x, y))
        if clicked_toolbar and last.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value, ActionType.CLICK_COORDINATE.value):
            removed = self.project.actions.pop()
            self.log(f"removed recorder stop action: {removed.summary()}")
            self.dirty = True

    def _action_recorded(self, action) -> None:
        self.project.actions.append(action)
        self.mark_dirty()
        self.refresh()

    def run_project(self) -> None:
        self._start_replay(0, len(self.project.actions) - 1, "run", True, True)

    def run_until_breakpoint(self) -> None:
        selected = self.table.selected_index()
        start = selected if selected >= 0 else 0
        if not any(
            action.breakpoint and action.action not in NON_EXECUTABLE_TYPES
            for action in self.project.actions[start:]
        ):
            QMessageBox.information(
                self, "Run Until Breakpoint",
                "There is no executable breakpoint at or after the selected step. Toggle one with F9 first.",
            )
            return
        self._start_replay(start, len(self.project.actions) - 1, "debug", True, True)

    def validate_flow(self) -> None:
        if not self.project.actions:
            QMessageBox.information(self, "Validate Flow", "Add at least one step before validating the flow.")
            return
        issues = self._validation_issues(0, len(self.project.actions) - 1)
        self._show_validation_results(issues)
        errors = sum(issue.level == LEVEL_ERROR for issue in issues)
        warnings = sum(issue.level == LEVEL_WARNING for issue in issues)
        if errors:
            self.update_status(f"Validation found {errors} error(s)")
        elif warnings:
            self.update_status(f"Validation passed with {warnings} warning(s)")
        else:
            self.update_status("Validation passed")

    def _validation_issues(
        self, start_index: int, end_index: int, force_enabled: bool = False,
        runtime_variables: dict | None = None,
    ) -> list[ValidationIssue]:
        issues = [
            ValidationIssue(LEVEL_ERROR, 0, "Runtime Inputs", error)
            for error in validate_variable_configuration(self.project)
        ]
        issues.extend(validate_project_detailed(
            self.project, self.project_dir, start_index, end_index, force_enabled, runtime_variables,
        ))
        return issues

    def _validate_before_execution(
        self, start_index: int, end_index: int, force_enabled: bool = False,
        runtime_variables: dict | None = None,
    ) -> bool:
        issues = self._validation_issues(start_index, end_index, force_enabled, runtime_variables)
        self._last_validation_issues = issues
        self._show_validation_results(issues)
        error_count = sum(issue.level == LEVEL_ERROR for issue in issues)
        warning_count = sum(issue.level == LEVEL_WARNING for issue in issues)
        if error_count:
            QMessageBox.critical(
                self,
                "Flow cannot run",
                f"Validation found {error_count} error(s). Fix the errors shown in the Validation panel and try again.",
            )
            return False
        if warning_count:
            reply = QMessageBox.question(
                self,
                "Run with warnings?",
                f"Validation found {warning_count} warning(s). Review them in the Validation panel.\n\nContinue running?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            return reply == QMessageBox.Yes
        return True

    def _show_validation_results(self, issues: list[ValidationIssue]) -> None:
        self.validation_table.setRowCount(len(issues))
        colors = {
            LEVEL_ERROR: QColor("#b91c1c"),
            LEVEL_WARNING: QColor("#a16207"),
            LEVEL_INFO: QColor("#2563eb"),
        }
        counts = {LEVEL_ERROR: 0, LEVEL_WARNING: 0, LEVEL_INFO: 0}
        for row, issue in enumerate(issues):
            counts[issue.level] = counts.get(issue.level, 0) + 1
            values = (issue.level, str(issue.step_number), issue.step_name, issue.reason)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, issue.step_number - 1)
                if column == 0:
                    item.setForeground(colors.get(issue.level, QColor("#475569")))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column == 1:
                    item.setTextAlignment(Qt.AlignCenter)
                item.setToolTip(issue.reason)
                self.validation_table.setItem(row, column, item)
        if issues:
            self.validation_summary.setText(
                f"{counts[LEVEL_ERROR]} Error · {counts[LEVEL_WARNING]} Warning · {counts[LEVEL_INFO]} Info"
            )
        else:
            self.validation_summary.setText("No issues found — this flow is ready to run.")
        self.bottom_tabs.setCurrentWidget(self.validation_wrap)

    def _validation_result_activated(self, item: QTableWidgetItem) -> None:
        step_index = item.data(Qt.UserRole)
        if not isinstance(step_index, int) or not 0 <= step_index < len(self.project.actions):
            return
        self.filter_box.clear()
        self.table.selectRow(step_index)
        step_item = self.table.item(step_index, 0)
        if step_item is not None:
            self.table.scrollToItem(step_item, QAbstractItemView.PositionAtCenter)
        self.table.setFocus(Qt.OtherFocusReason)

    def schedule_flows_dialog(self) -> None:
        dialog = ScheduleFlowsDialog(
            self.schedule_store, self.settings, self,
            task_registrar=self.windows_task_registrar,
        )
        dialog.run_now_requested.connect(lambda name: self._run_flow_now(name))
        dialog.task_log.connect(self.log)
        dialog.exec()

    def start_scheduler_reconciliation(self) -> None:
        """Reconcile Windows tasks after the main window is visible, without blocking Qt."""
        if self.windows_task_registrar is None:
            return
        thread = threading.Thread(
            target=self._reconcile_schedule_tasks, name="scheduler-reconciliation", daemon=True,
        )
        thread.start()

    def _reconcile_schedule_tasks(self) -> None:
        if self.windows_task_registrar is None:
            return
        try:
            store = ScheduleStore(flows_root(), history_limit=self.schedule_store.history_limit)
            results = reconcile_schedules(store, self.windows_task_registrar)
        except Exception as exc:
            self.log_recorded.emit(f"[Scheduler] Startup reconciliation failed: {exc}")
            return
        for result in results:
            if result.ok:
                self.log_recorded.emit(f"[Scheduler] {result.status}: {result.task_name}")
            else:
                self.log_recorded.emit(
                    f"[Scheduler] Registration failed for {result.task_name}: {result.error}"
                )

    def _check_schedules(self) -> None:
        self.schedule_store.load()
        self.schedule_store.remove_missing_flows()
        now = datetime.now(timezone.utc)
        for flow_name in self.schedule_store.list_flow_names():
            schedule = self.schedule_store.get(flow_name)
            if flow_name in self._scheduled_runs or flow_name in self._schedule_queue or not is_due(schedule, now):
                continue
            self._run_flow_now(flow_name, scheduled=True)

    def _run_flow_now(self, flow_name: str, scheduled: bool = False) -> None:
        flow_dir = flows_root() / flow_name
        project_json = flow_dir / "project.json"
        if not project_json.exists():
            self.log(f"[{flow_name}] schedule skipped: flow not found")
            return
        if flow_name in self._scheduled_runs:
            # The same flow's previous run has not finished yet - never overlap a flow with itself.
            schedule = self.schedule_store.get(flow_name)
            evidence = self._create_standalone_evidence(flow_dir, flow_name, "Scheduled" if scheduled else "Manual")
            reason = "A previous run of this flow is still running."
            if evidence:
                evidence.logger.info("run skipped: %s", reason)
                evidence.finalize("Skipped", error=reason)
            mark_skipped(schedule, STATUS_SKIPPED_RUNNING, source=evidence.source if evidence else None,
                         evidence_path=evidence.relative_folder if evidence else None,
                         run_id=evidence.run_id if evidence else None)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self.log(f"[{flow_name}] schedule skipped: already running")
            return
        if self.replay_thread is not None:
            if flow_name not in self._schedule_queue:
                self._schedule_queue.append(flow_name)
                self.log(f"[{flow_name}] schedule queued: waiting for the current run to finish")
            return
        if self._scheduled_runs:
            # Only one flow runs at a time; queue this one until the active run finishes.
            if flow_name not in self._schedule_queue:
                self._schedule_queue.append(flow_name)
                self.log(f"[{flow_name}] schedule queued: waiting for the current run to finish")
            else:
                self.log(f"[{flow_name}] schedule skipped: already queued")
            return
        if (
            self.project_dir
            and Path(self.project_dir).resolve() == flow_dir.resolve()
            and (self.replay_thread is not None or self.recorder is not None)
        ):
            schedule = self.schedule_store.get(flow_name)
            evidence = self._create_standalone_evidence(flow_dir, flow_name, "Scheduled" if scheduled else "Manual")
            reason = "The flow is open in the editor and is currently busy."
            if evidence:
                evidence.logger.info("run skipped: %s", reason)
                evidence.finalize("Skipped", error=reason)
            mark_skipped(schedule, STATUS_SKIPPED_BUSY, source=evidence.source if evidence else None,
                         evidence_path=evidence.relative_folder if evidence else None,
                         run_id=evidence.run_id if evidence else None)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self.log(f"[{flow_name}] schedule skipped: flow is open and busy")
            return
        try:
            project = ProjectManager().load(project_json)
        except Exception as exc:
            self.log(f"[{flow_name}] schedule failed to load: {exc}")
            schedule = self.schedule_store.get(flow_name)
            evidence = self._create_standalone_evidence(flow_dir, flow_name, "Scheduled" if scheduled else "Manual")
            error = f"Could not load flow: {exc}"
            if evidence:
                evidence.logger.error(error)
                evidence.finalize("Failed", error=error)
            mark_started(schedule, source=evidence.source if evidence else None,
                         evidence_path=evidence.relative_folder if evidence else None,
                         run_id=evidence.run_id if evidence else None)
            mark_finished(schedule, EXECUTION_FAILED, error=f"Could not load flow: {exc}", attempts=0)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            return
        source = "Scheduled" if scheduled else "Manual"
        project_meta = getattr(project, "project", None)
        project_settings = getattr(project, "settings", ProjectSettings())
        try:
            evidence = RunEvidenceSession(
                flow_dir, getattr(project_meta, "name", "") or flow_name, source,
                getattr(project_settings, "evidence_retention_runs", 100),
            )
        except OSError as exc:
            self.log(f"[{flow_name}] could not create run evidence: {exc}")
            schedule = self.schedule_store.get(flow_name)
            mark_started(schedule, source=source)
            mark_finished(schedule, EXECUTION_FAILED, error=f"Could not create run evidence: {exc}", attempts=0)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            return
        self.active_evidence = evidence
        self.last_evidence_folder = evidence.folder
        self.file_logger = evidence.logger
        self.run_log_path = evidence.log_path
        schedule = self.schedule_store.get(flow_name)
        mark_started(
            schedule, source=source, evidence_path=evidence.relative_folder, run_id=evidence.run_id,
        )
        self.schedule_store.set(schedule)
        self.schedule_store.save()
        configuration_errors = validate_variable_configuration(project)
        clipboard_text = QApplication.clipboard().text()
        if scheduled:
            supplied_inputs = dict(schedule.runtime_inputs)
            runtime_variables, input_errors = prepare_runtime_variables(
                project, supplied_inputs, clipboard_text,
            )
        elif getattr(project, "runtime_inputs", {}):
            input_dialog = RuntimeInputsDialog(
                project, schedule.runtime_inputs, clipboard_text,
                QApplication.activeModalWidget() or self,
            )
            if input_dialog.exec() != QDialog.Accepted:
                reason = "Run cancelled before runtime inputs were confirmed"
                mark_finished(schedule, "Skipped", error=reason, attempts=0)
                self.schedule_store.set(schedule)
                self.schedule_store.save()
                evidence.finalize("Skipped", error=reason)
                self.active_evidence = None
                self.file_logger = None
                return
            supplied_inputs = input_dialog.input_values
            runtime_variables = input_dialog.runtime_variables
            input_errors = []
        else:
            supplied_inputs = {}
            runtime_variables, input_errors = prepare_runtime_variables(project, clipboard_text=clipboard_text)
        sensitive_names = sensitive_variable_names(project)
        self._active_secret_values = {
            str(runtime_variables[name]) for name in sensitive_names
            if name in runtime_variables and runtime_variables[name] not in (None, "")
        }
        evidence.set_runtime_inputs(supplied_inputs, sensitive_names)
        if configuration_errors or input_errors:
            reason = (configuration_errors + input_errors)[0]
            self.log(f"[{flow_name}] run blocked by runtime inputs: {reason}")
            mark_finished(schedule, EXECUTION_FAILED, error=reason, attempts=0)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self._finalize_evidence(EXECUTION_FAILED, error=reason)
            return
        if getattr(project, "runtime_inputs", {}):
            validation_issues = validate_project_detailed(
                project, flow_dir, runtime_variables=runtime_variables,
            )
        else:
            validation_issues = validate_project_detailed(project, flow_dir)
        evidence.set_validation(validation_issues)
        errors = [issue for issue in validation_issues if issue.level == LEVEL_ERROR]
        warnings = [issue for issue in validation_issues if issue.level == LEVEL_WARNING]
        if errors:
            reason = errors[0].message()
            self.log(f"[{flow_name}] schedule blocked by validation: {reason}")
            mark_finished(schedule, EXECUTION_FAILED, error=reason, failed_step=errors[0].step_number, attempts=0)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self._finalize_evidence(EXECUTION_FAILED, failed_step=errors[0].step_number, error=reason)
            return
        for warning in warnings:
            # Scheduled flows are unattended: warnings are retained in the log,
            # while only errors block their execution.
            self.log(f"[{flow_name}] validation warning: {warning.message()}")

        thread = QThread()
        worker = ReplayWorker(
            project, flow_dir, 0, len(project.actions) - 1, True, True,
            runtime_variables, [], evidence.folder,
        )
        worker.flow_name = flow_name
        worker.evidence_session = evidence
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._scheduled_run_log)
        if hasattr(worker, "attention_requested"):
            worker.attention_requested.connect(lambda payload, active=worker: self._show_failure_attention(payload, active))
        worker.action_status.connect(self._scheduled_action_status)
        if hasattr(worker, "retry_progress"):
            worker.retry_progress.connect(self._scheduled_retry_progress)
        if hasattr(worker, "control_progress"):
            worker.control_progress.connect(self._scheduled_control_progress)
        worker.finished.connect(self._scheduled_run_success)
        worker.stopped.connect(self._scheduled_run_stopped)
        worker.failed.connect(self._scheduled_run_failed)
        self._scheduled_runs[flow_name] = (thread, worker)
        prefix = "scheduled" if scheduled else "manual"
        self.log(f"[{flow_name}] {prefix} run starting")
        try:
            self._prepare_run_environment(project_settings, f"Running {flow_name}")
        except Exception as exc:
            self.log(f"[{flow_name}] desktop preparation failed: {exc}")
            self._scheduled_run_finished(
                flow_name, STATUS_FAILED, error=f"Desktop preparation failed: {exc}", attempts=0,
            )
            return
        QTimer.singleShot(300, lambda name=flow_name, run_thread=thread: self._start_scheduled_worker(name, run_thread))

    def _start_scheduled_worker(self, flow_name: str, thread: QThread) -> None:
        entry = self._scheduled_runs.get(flow_name)
        if entry is None or entry[0] is not thread or thread.isRunning():
            return
        thread.start()

    def _scheduled_run_log(self, message: str) -> None:
        worker = self.sender()
        flow_name = getattr(worker, "flow_name", "")
        self.log(f"[{flow_name}] {message}" if flow_name else message)

    def _scheduled_action_status(self, index: int, status: str) -> None:
        worker = self.sender()
        flow_name = getattr(worker, "flow_name", "")
        if self.execution_floating:
            label = "Failed" if status == "failed" else "Running"
            self.execution_floating.set_status(f"{flow_name} · Step {index + 1} · {label}")
            self._position_execution_toolbar()
        flow_dir = flows_root() / flow_name if flow_name else None
        if flow_dir and self.project_dir and Path(self.project_dir).resolve() == flow_dir.resolve():
            self.set_action_status(index, status)

    def _scheduled_retry_progress(self, index: int, attempt: int, total: int, _reason: str) -> None:
        worker = self.sender()
        flow_name = getattr(worker, "flow_name", "")
        if self.execution_floating:
            self.execution_floating.set_status(f"{flow_name} · Step {index + 1} · Retry {attempt}/{total}")
            self._position_execution_toolbar()

    def _scheduled_control_progress(self, index: int, message: str) -> None:
        worker = self.sender()
        flow_name = getattr(worker, "flow_name", "")
        if self.execution_floating:
            self.execution_floating.set_status(f"{flow_name} · Step {index + 1} · {message}")
            self._position_execution_toolbar()

    def _scheduled_run_success(self) -> None:
        worker = self.sender()
        runner = getattr(worker, "runner", None)
        self._scheduled_run_finished(
            getattr(worker, "flow_name", ""), getattr(runner, "final_status", COMPLETED_UNVERIFIED),
            attempts=getattr(runner, "total_attempts", None),
        )

    def _scheduled_run_stopped(self) -> None:
        worker = self.sender()
        runner = getattr(worker, "runner", None)
        current_index = getattr(runner, "current_index", None)
        self._scheduled_run_finished(
            getattr(worker, "flow_name", ""), STOPPED_BY_USER,
            error="Stopped by user",
            failed_step=current_index + 1 if isinstance(current_index, int) else None,
            attempts=getattr(runner, "total_attempts", None),
        )

    def _scheduled_run_failed(self, index: int, message: str) -> None:
        worker = self.sender()
        runner = getattr(worker, "runner", None)
        self._scheduled_run_finished(
            getattr(worker, "flow_name", ""), EXECUTION_FAILED, error=message, failed_step=index + 1,
            attempts=getattr(runner, "total_attempts", None),
        )

    def _scheduled_run_finished(
        self, flow_name: str, status: str, error: str | None = None, failed_step: int | None = None,
        attempts: int | None = None,
    ) -> None:
        entry = self._scheduled_runs.pop(flow_name, None)
        worker = entry[1] if entry else None
        if entry:
            thread, _worker = entry
            thread.quit()
            thread.wait()
        schedule = self.schedule_store.get(flow_name)
        safe_error = mask_sensitive_text(error, self._active_secret_values) if error else None
        runner = getattr(worker, "runner", None)
        diagnostics = runner.run_diagnostics() if runner else {}
        safe_diagnostics = self._mask_evidence_value(diagnostics)
        mark_finished(
            schedule, status, error=safe_error, failed_step=failed_step,
            attempts=attempts, diagnostics=safe_diagnostics,
        )
        self.schedule_store.set(schedule)
        self.schedule_store.save()
        self.log(f"[{flow_name}] scheduled run {status}")
        if runner is not None:
            self.last_runtime_variables = dict(getattr(runner, "runtime_variables", {}))
        evidence = getattr(worker, "evidence_session", None)
        if evidence is not None:
            self.active_evidence = evidence
        self._finalize_evidence(
            status,
            step_results=getattr(runner, "step_results", None),
            attempts=attempts or 0,
            failed_step=failed_step,
            error=safe_error,
            diagnostics=diagnostics,
        )
        self._restore_run_environment()
        self._start_next_queued_flow()

    def _start_next_queued_flow(self) -> None:
        if self._scheduled_runs or not self._schedule_queue:
            return
        next_flow = self._schedule_queue.pop(0)
        self._run_flow_now(next_flow, scheduled=True)

    def run_from_here(self) -> None:
        index = self.table.selected_index()
        if index >= 0:
            self._start_replay(index, len(self.project.actions) - 1, "from", True, True)

    def run_until_here(self) -> None:
        index = self.table.selected_index()
        if index >= 0:
            self._start_replay(0, index, "until", True, True)

    def test_selected_step(self, action: RpaAction | None = None) -> None:
        index = self.project.actions.index(action) if action in self.project.actions else self.table.selected_index()
        if index < 0:
            QMessageBox.information(self, "Test Step", "Select a step to test first.")
            return
        self._start_replay(index, index, "test", False, False, force_validation_enabled=True)

    def _test_manual_action(self, dialog: ManualActionDialog, action: RpaAction) -> None:
        """Test a guided step through the normal replay lifecycle without saving it."""
        if self.replay_thread is not None or self._scheduled_runs:
            QMessageBox.information(self, "Test Step", "Another flow is already running.")
            dialog.finish_step_test()
            return
        if action.action in NON_EXECUTABLE_TYPES:
            QMessageBox.information(
                self, "Test Step",
                "Conditions, repeat blocks, and notes need surrounding steps, so they are tested when the flow runs.",
            )
            dialog.finish_step_test()
            return
        if action.action == ActionType.PYTHON_CODE.value and not self.confirm_python_code_warning():
            dialog.finish_step_test()
            return
        self.manual_test_dialog = dialog
        self._manual_test_action_id = action.id
        self.project.actions.append(action)
        test_index = len(self.project.actions) - 1
        self.refresh()
        self.table.selectRow(test_index)
        self.log(f"[Guided Builder] testing unsaved step: {action.summary()}")
        self._start_replay(
            test_index, test_index, "test", False, False,
            force_validation_enabled=True,
        )
        if self.replay_thread is None:
            self._cleanup_manual_test_action()

    def _start_replay(
        self,
        start_index: int,
        end_index: int,
        mode: str,
        include_start_delay: bool,
        respect_enabled: bool,
        validate: bool = True,
        runtime_variables: dict | None = None,
        force_validation_enabled: bool = False,
    ) -> None:
        if self.replay_thread is not None or self._scheduled_runs or not self.project.actions:
            return
        if not self.ensure_project_dir():
            return
        if runtime_variables is None:
            configuration_errors = validate_variable_configuration(self.project)
            if configuration_errors:
                QMessageBox.warning(
                    self, "Check Variables",
                    "Execution did not start because the variable configuration is invalid:\n\n"
                    + "\n".join(f"• {error}" for error in configuration_errors),
                )
                return
            clipboard_text = QApplication.clipboard().text()
            if self.project.runtime_inputs:
                dialog = RuntimeInputsDialog(self.project, clipboard_text=clipboard_text, parent=self)
                if dialog.exec() != QDialog.Accepted:
                    self.update_status("Run cancelled")
                    return
                runtime_variables = dialog.runtime_variables
                supplied_inputs = dialog.input_values
            else:
                runtime_variables, input_errors = prepare_runtime_variables(
                    self.project, clipboard_text=clipboard_text,
                )
                if input_errors:
                    QMessageBox.warning(self, "Check Run Inputs", "\n".join(input_errors))
                    return
                supplied_inputs = {}
        else:
            supplied_inputs = {
                name: runtime_variables.get(name) for name in self.project.runtime_inputs
                if name in runtime_variables
            }
        sensitive_names = sensitive_variable_names(self.project)
        self._active_secret_values = {
            str(runtime_variables[name]) for name in sensitive_names
            if name in runtime_variables and runtime_variables[name] not in (None, "")
        }
        source = {
            "run": "Manual", "test": "Test Step", "from": "Run From Here", "until": "Run Until Here",
            "debug": "Run Until Breakpoint",
        }.get(mode, "Manual")
        try:
            self._begin_evidence(self.project_dir, self.project, source)
        except OSError as exc:
            self._active_secret_values.clear()
            show_error(self, "Could not create run report", f"Execution did not start because its evidence folder could not be created.\n\n{exc}")
            return
        self.active_evidence.set_runtime_inputs(supplied_inputs, sensitive_names)
        if validate:
            if not self._validate_before_execution(
                start_index, end_index, force_validation_enabled, runtime_variables,
            ):
                self.active_evidence.set_validation(self._last_validation_issues)
                has_errors = any(issue.level == LEVEL_ERROR for issue in self._last_validation_issues)
                status = EXECUTION_FAILED if has_errors else "Skipped"
                error = self._last_validation_issues[0].message() if has_errors else "Run cancelled after validation warnings"
                issue_step = self._last_validation_issues[0].step_number if has_errors else 0
                failed_step = issue_step if issue_step > 0 else None
                self._finish_active_history(status, error, failed_step, 0)
                self._finalize_evidence(status, failed_step=failed_step, error=error)
                return
        self.active_evidence.set_validation(self._last_validation_issues)
        self.run_start_index = start_index
        self.run_end_index = end_index
        self.run_mode = mode
        self.run_started_at = time.monotonic()
        self._reset_action_statuses()
        self._hide_details_for_run()
        try:
            self._prepare_run_environment(self.project.settings, "Running automation")
        except Exception as exc:
            self._restore_details_after_run()
            self._restore_run_environment()
            self._finish_active_history(EXECUTION_FAILED, str(exc), None, 0)
            self._finalize_evidence(EXECUTION_FAILED, error=f"Desktop preparation failed: {exc}")
            show_error(self, "Could not prepare desktop", str(exc))
            return
        self.replay_thread = QThread()
        self.replay_worker = ReplayWorker(
            self.project,
            self.project_dir,
            start_index,
            end_index,
            include_start_delay,
            respect_enabled,
            runtime_variables,
            self._image_match_exclusions(),
            self.active_evidence.folder,
            any(
                action.breakpoint and action.action not in NON_EXECUTABLE_TYPES
                for action in self.project.actions[start_index:end_index + 1]
            ),
        )
        self.replay_worker.moveToThread(self.replay_thread)
        self.replay_thread.started.connect(self.replay_worker.run)
        self.replay_worker.action_status.connect(self.set_action_status)
        self.replay_worker.retry_progress.connect(self._retry_progress)
        self.replay_worker.control_progress.connect(self._control_progress)
        self.replay_worker.debug_paused.connect(self._debug_paused)
        self.replay_worker.attention_requested.connect(
            lambda payload, active=self.replay_worker: self._show_failure_attention(payload, active)
        )
        self.replay_worker.log.connect(self.log)
        self.replay_worker.finished.connect(self.run_completed)
        self.replay_worker.stopped.connect(self.run_stopped)
        self.replay_worker.failed.connect(self.run_failed)
        QTimer.singleShot(300, self.replay_thread.start)
        self.update_buttons()

    def _image_match_exclusions(self) -> list[tuple[int, int, int, int]]:
        exclusions: list[tuple[int, int, int, int]] = []
        if self.isVisible() and not self.isMinimized():
            rect = self.frameGeometry()
            exclusions.append((rect.x(), rect.y(), rect.width(), rect.height()))
        if self.execution_floating and self.execution_floating.isVisible():
            rect = self.execution_floating.frameGeometry()
            exclusions.append((rect.x(), rect.y(), rect.width(), rect.height()))
        return exclusions

    def _reset_action_statuses(self) -> None:
        for index, action in enumerate(self.project.actions):
            action.status = "pending"
            self.table.update_action(index, action)
        self.table.apply_filter(self.filter_box.text())

    def _validation_errors(self, start_index: int, end_index: int, force_enabled: bool = False) -> list[str]:
        return [
            issue.message()
            for issue in self._validation_issues(start_index, end_index, force_enabled)
            if issue.level == LEVEL_ERROR
        ]

    def stop_run(self) -> None:
        if self.replay_worker:
            self.replay_worker.stop()
            self.log("stop replay requested")
            return
        if self._scheduled_runs:
            _flow_name, (_thread, worker) = next(iter(self._scheduled_runs.items()))
            worker.stop()
            self.log("stop scheduled replay requested")

    def _debug_paused(self, index: int, reason: str, values: dict) -> None:
        if not self.replay_worker or not 0 <= index < len(self.project.actions):
            return
        self.debug_paused_index = index
        self.debug_paused_values = dict(values)
        action = self.project.actions[index]
        action.status = "paused"
        self.running_action_index = index
        self.table.update_action(index, action)
        self.table.selectRow(index)
        item = self.table.item(index, 0)
        if item:
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        next_index = self._next_executable_index(index + 1)
        next_text = (
            f"Next: Step {next_index + 1} - {self.project.actions[next_index].summary()}"
            if next_index is not None else "Next: end of run"
        )
        if self.execution_floating:
            label = "Breakpoint" if reason == "breakpoint" else "Step complete"
            self.execution_floating.set_debug_paused(
                f"Paused before Step {index + 1} - {action.summary()} ({label})", next_text,
            )
            self._position_execution_toolbar()
        if not self.isVisible():
            self.debug_showed_main = True
            self.showMaximized() if self.replay_was_maximized else self.showNormal()
            self.raise_()
            self.activateWindow()
        self.update_status(f"Paused before Step {index + 1}")

    def _next_executable_index(self, start: int) -> int | None:
        end = min(self.run_end_index, len(self.project.actions) - 1)
        for index in range(max(0, start), end + 1):
            action = self.project.actions[index]
            if action.action not in NON_EXECUTABLE_TYPES and action.enabled:
                return index
        return None

    def _continue_debug(self, command: str) -> None:
        worker = self.replay_worker
        if not worker or self.debug_paused_index is None:
            return
        current = self.debug_paused_index
        self.debug_paused_index = None
        self.debug_paused_values = {}
        if self.execution_floating:
            self.execution_floating.set_debug_running(f"Step {current + 1} - Debug running")
            self._position_execution_toolbar()
        if self.debug_showed_main and self._active_run_settings and self._active_run_settings.hide_window_during_replay:
            self.hide()
        self.debug_showed_main = False
        if command == "resume":
            worker.debug_resume()
        elif command == "step":
            worker.debug_step_over()
        elif command == "skip":
            worker.debug_skip()

    def debug_resume(self) -> None:
        self._continue_debug("resume")

    def debug_step_over(self) -> None:
        self._continue_debug("step")

    def debug_skip(self) -> None:
        self._continue_debug("skip")

    def debug_restart_selected(self) -> None:
        worker = self.replay_worker
        if not worker or self.debug_paused_index is None:
            return
        index = self.table.selected_index()
        if not self.run_start_index <= index <= self.run_end_index:
            QMessageBox.warning(self, "Restart Debugging", "Select a step inside the current run range.")
            return
        if self.project.actions[index].action in NON_EXECUTABLE_TYPES or not self.project.actions[index].enabled:
            QMessageBox.warning(self, "Restart Debugging", "Select an enabled executable step, not a block marker.")
            return
        current = self.debug_paused_index
        self.debug_paused_index = None
        self.debug_paused_values = {}
        if self.debug_showed_main and self._active_run_settings and self._active_run_settings.hide_window_during_replay:
            self.hide()
        self.debug_showed_main = False
        if self.execution_floating:
            self.execution_floating.set_debug_running(f"Restarting from Step {index + 1}")
        worker.debug_restart(index)
        self.log(f"[Debug] Restart requested: Step {current + 1} -> Step {index + 1}")

    def debug_variables_dialog(self) -> None:
        worker = self.replay_worker
        if not worker or self.debug_paused_index is None:
            return
        values = dict(worker.runner.runtime_variables)
        sensitive = sensitive_variable_names(self.project)
        protected = {"RUN_DATE", "CLIPBOARD_TEXT"}
        dialog = DebugVariablesDialog(self.project, values, sensitive, protected, self)
        if dialog.exec() == QDialog.Accepted:
            worker.debug_update_variables(dialog.values)
            self.debug_paused_values = dict(dialog.values)

    def _retry_progress(self, index: int, attempt: int, total: int, _reason: str) -> None:
        if self.execution_floating:
            self.execution_floating.set_status(f"Step {index + 1} · Retry {attempt}/{total}")
            self._position_execution_toolbar()

    def _show_failure_attention(self, payload: dict, worker: ReplayWorker) -> None:
        if worker is None:
            return
        was_hidden = not self.isVisible()
        if was_hidden:
            self.showMaximized() if self.replay_was_maximized else self.showNormal()
            self.raise_()
            self.activateWindow()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Flow requires attention")
        box.setText(
            f"Flow: {payload.get('flow_name') or self.project.project.name}\n"
            f"Failed step: {payload.get('step_number')} - {payload.get('step_name')}"
        )
        box.setInformativeText(str(payload.get("error") or "The step failed."))
        screenshot = str(payload.get("screenshot") or "")
        if screenshot:
            path = Path(screenshot)
            if not path.is_absolute() and self.active_evidence is not None:
                path = self.active_evidence.folder / path
            if path.is_file():
                pixmap = QPixmap(str(path))
                if not pixmap.isNull():
                    box.setIconPixmap(pixmap.scaled(420, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        retry = box.addButton("Retry", QMessageBox.AcceptRole)
        skip = box.addButton("Skip", QMessageBox.DestructiveRole)
        stop = box.addButton("Stop", QMessageBox.RejectRole)
        box.setDefaultButton(stop)
        box.exec()
        clicked = box.clickedButton()
        decision = "retry" if clicked is retry else "skip" if clicked is skip else "stop"
        self.log(f"Human escalation decision: {decision}")
        worker.submit_attention_decision(decision)
        if was_hidden and decision != "stop" and self._active_run_settings and self._active_run_settings.hide_window_during_replay:
            self.hide()

    def _control_progress(self, index: int, message: str) -> None:
        if self.execution_floating:
            self.execution_floating.set_status(f"Step {index + 1} · {message}")
            self._position_execution_toolbar()

    def run_finished(self) -> None:
        if self.replay_thread:
            self.replay_thread.quit()
            self.replay_thread.wait()
        self.replay_thread = None
        self.replay_worker = None
        self.running_action_index = None
        self.debug_paused_index = None
        self.debug_paused_values = {}
        self.debug_showed_main = False
        self._restore_details_after_run()
        self._restore_run_environment()
        self._cleanup_manual_test_action()
        self.update_buttons()
        self.update_status()

    def _cleanup_manual_test_action(self) -> None:
        action_id = self._manual_test_action_id
        dialog = self.manual_test_dialog
        self._manual_test_action_id = None
        self.manual_test_dialog = None
        if action_id:
            self.project.actions = [action for action in self.project.actions if action.id != action_id]
            self.refresh()
            self.log("[Guided Builder] unsaved test step removed after testing")
        if dialog is not None and shiboken6.isValid(dialog):
            dialog.finish_step_test()

    def _hide_details_for_run(self) -> None:
        self.details_were_visible_before_run = self.editor_scroll.isVisible()
        self.editor_scroll.setVisible(False)

    def _prepare_run_environment(self, settings: ProjectSettings, label: str) -> None:
        """Shared desktop preparation for interactive and scheduled replay."""
        self._active_run_settings = settings
        self.replay_was_maximized = self.isMaximized()
        self.execution_floating = FloatingExecutionToolbar()
        self.execution_floating.stop_requested.connect(self.stop_run)
        self.execution_floating.resume_requested.connect(self.debug_resume)
        self.execution_floating.step_over_requested.connect(self.debug_step_over)
        self.execution_floating.skip_requested.connect(self.debug_skip)
        self.execution_floating.restart_requested.connect(self.debug_restart_selected)
        self.execution_floating.variables_requested.connect(self.debug_variables_dialog)
        self.execution_floating.set_status(label)
        self.execution_floating.show()
        self._position_execution_toolbar()
        self.execution_floating.position_changed.connect(self._execution_toolbar_moved)
        if not settings.hide_window_during_replay:
            return
        self.hide()
        # Prepare a clean desktop before replay begins. The always-on-top stop
        # control remains available, and other windows are intentionally not
        # restored after the run.
        self._show_windows_desktop()

    def _hide_for_replay(self) -> None:
        """Compatibility wrapper used by existing UI tests and integrations."""
        self._prepare_run_environment(self.project.settings, "Running automation")

    def _position_execution_toolbar(self) -> None:
        toolbar = self.execution_floating
        if not toolbar:
            return
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        bounds = screen.availableGeometry()
        toolbar.adjustSize()
        saved = self.settings.value("execution_toolbar_position")
        if isinstance(saved, QPoint):
            saved_rect = QRect(saved, toolbar.size())
            saved_center = QPoint(saved_rect.center())
            saved_screen = QApplication.screenAt(saved_center)
            saved_bounds = saved_screen.availableGeometry() if saved_screen else bounds
            if saved_bounds.contains(saved_rect):
                toolbar.move(saved)
                return
        margin = 32
        default_position = QPoint(
            bounds.right() - toolbar.width() - margin + 1,
            bounds.bottom() - toolbar.height() - margin + 1,
        )
        toolbar.move(default_position)
        self.settings.setValue("execution_toolbar_position", default_position)

    def _execution_toolbar_moved(self, position: QPoint) -> None:
        toolbar = self.execution_floating
        if not toolbar:
            return
        center = QPoint(position.x() + toolbar.width() // 2, position.y() + toolbar.height() // 2)
        screen = QApplication.screenAt(center) or self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        bounds = screen.availableGeometry()
        x = min(max(position.x(), bounds.left()), bounds.right() - toolbar.width() + 1)
        y = min(max(position.y(), bounds.top()), bounds.bottom() - toolbar.height() + 1)
        safe = QPoint(x, y)
        if safe != position:
            toolbar.blockSignals(True)
            toolbar.move(safe)
            toolbar.blockSignals(False)
        self.settings.setValue("execution_toolbar_position", safe)

    def _restore_run_environment(self) -> None:
        if self.execution_floating:
            self.execution_floating.close()
            self.execution_floating = None
        settings = self._active_run_settings
        self._active_run_settings = None
        if settings and settings.hide_window_during_replay:
            self.showMaximized() if self.replay_was_maximized else self.showNormal()
            self.raise_()

    def _restore_after_replay(self) -> None:
        """Compatibility wrapper used by existing UI tests and integrations."""
        self._restore_run_environment()

    def _restore_details_after_run(self) -> None:
        self.editor_scroll.setVisible(self.details_were_visible_before_run)

    def run_completed(self) -> None:
        elapsed = time.monotonic() - self.run_started_at if self.run_started_at else 0.0
        mode = self.run_mode
        start = self.run_start_index
        end = self.run_end_index
        completed = sum(1 for action in self.project.actions[start:end + 1] if action.status == "completed")
        skipped = sum(1 for action in self.project.actions[start:end + 1] if action.status == "skipped")
        runner = self.replay_worker.runner if self.replay_worker else None
        self.last_runtime_variables = dict(getattr(runner, "runtime_variables", {}))
        if self.project.settings.persist_variable_values and self.project_dir:
            try:
                self.manager.save(self.project, self.project_dir)
            except (OSError, TypeError, ValueError) as exc:
                self.log(f"Variable persistence warning: {exc}")
        self.log("step test completed" if mode == "test" else "automation completed")
        final_status = getattr(runner, "final_status", COMPLETED_UNVERIFIED)
        diagnostics = runner.run_diagnostics() if runner else {}
        self._finish_active_history(
            final_status, None, None, getattr(runner, "total_attempts", 0), diagnostics,
        )
        self._finalize_evidence(
            final_status, getattr(runner, "step_results", None), getattr(runner, "total_attempts", 0),
            diagnostics=diagnostics,
        )
        self.run_finished()
        if mode == "test":
            QMessageBox.information(self, "Step Test", f"Step {start + 1} completed successfully in {elapsed:.2f} seconds.")
            self._start_next_queued_flow()
            return
        QMessageBox.information(
            self,
            "Automation Completed",
            f"Automation completed ({final_status})\n\n{completed} completed\n{skipped} skipped\n0 failed\nDuration: {elapsed:.2f} seconds",
        )
        self._start_next_queued_flow()

    def run_stopped(self) -> None:
        elapsed = time.monotonic() - self.run_started_at if self.run_started_at else 0.0
        last_step = self.running_action_index + 1 if self.running_action_index is not None else self.run_start_index + 1
        if self.running_action_index is not None and 0 <= self.running_action_index < len(self.project.actions):
            self.project.actions[self.running_action_index].status = "stopped"
            self.table.update_action(self.running_action_index, self.project.actions[self.running_action_index])
        self.log("automation stopped by user")
        runner = self.replay_worker.runner if self.replay_worker else None
        self.last_runtime_variables = dict(getattr(runner, "runtime_variables", {}))
        attempts = getattr(runner, "total_attempts", 0)
        diagnostics = runner.run_diagnostics() if runner else {}
        self._finish_active_history(STOPPED_BY_USER, "Stopped by user", last_step, attempts, diagnostics)
        self._finalize_evidence(
            STOPPED_BY_USER, getattr(runner, "step_results", None), attempts, last_step, "Stopped by user",
            diagnostics,
        )
        self.run_finished()
        QMessageBox.information(self, "Automation Stopped", f"Execution stopped at step {last_step}.\nDuration: {elapsed:.2f} seconds")
        self._start_next_queued_flow()

    def run_failed(self, index: int, message: str) -> None:
        display_message = mask_sensitive_text(message, self._active_secret_values)
        self.log(f"step failed: {message}")
        failure_was_deferred = bool(
            self.replay_worker and self.replay_worker.runner.had_continued_failures
        )
        self.last_runtime_variables = dict(self.replay_worker.runner.runtime_variables) if self.replay_worker else {}
        runner = self.replay_worker.runner if self.replay_worker else None
        failed_step = index + 1 if index >= 0 else None
        attempts = getattr(runner, "total_attempts", 0)
        diagnostics = runner.run_diagnostics() if runner else {}
        self._finish_active_history(EXECUTION_FAILED, message, failed_step, attempts, diagnostics)
        self._finalize_evidence(
            EXECUTION_FAILED, getattr(runner, "step_results", None), attempts, failed_step, message,
            diagnostics,
        )
        self.run_finished()
        if index < 0 or index >= len(self.project.actions):
            show_error(self, "Automation stopped", display_message)
            self._start_next_queued_flow()
            return
        self.table.selectRow(index)
        self._show_actionable_failure(index, display_message, allow_skip=not failure_was_deferred)
        self._start_next_queued_flow()

    def _show_actionable_failure(self, index: int, message: str, allow_skip: bool = True) -> None:
        action = self.project.actions[index]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Step {index + 1} needs attention")
        box.setText(message)
        box.setInformativeText(
            "Review the failed step and its recovery settings."
            if not allow_skip else
            "Review the step, try a recovery option, or stop the automation."
        )
        test_button = None
        recapture_button = None
        original_button = None
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            test_button = box.addButton("Test Target", QMessageBox.ActionRole)
            recapture_button = box.addButton("Recapture Target", QMessageBox.ActionRole)
            original_button = box.addButton("Use Original Position", QMessageBox.ActionRole)
        skip_button = box.addButton("Skip Step", QMessageBox.DestructiveRole) if allow_skip else None
        box.addButton("Stop", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is test_button:
            self.test_target(action)
        elif clicked is recapture_button:
            self.recapture_target(action)
        elif clicked is original_button:
            action.data["use_coordinate_fallback"] = True
            self.mark_dirty()
            QMessageBox.information(self, "Original Position Enabled", "This step will use its original click position when the target cannot be found.")
        elif skip_button is not None and clicked is skip_button:
            action.status = "skipped"
            self.table.update_action(index, action)
            if self.run_mode != "test" and index < self.run_end_index:
                self._start_replay(
                    index + 1,
                    self.run_end_index,
                    self.run_mode,
                    False,
                    True,
                    validate=False,
                    runtime_variables=self.last_runtime_variables,
                )

    def test_target(self, action: RpaAction | None = None) -> None:
        action = action if isinstance(action, RpaAction) else None
        if action is None:
            index = self.table.selected_index()
            action = self.project.actions[index] if index >= 0 else None
        if not action or action.action not in (
            ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value,
            ActionType.IF_IMAGE_EXISTS.value, ActionType.IF_IMAGE_NOT_EXISTS.value,
        ):
            QMessageBox.information(self, "Test Target", "Select an image-based step to test its target image.")
            return
        if not self.project_dir:
            show_error(self, "Test Target", "Open or create an automation first.")
            return
        references = [str(action.data.get("image", ""))]
        references.extend(str(item) for item in action.data.get("reference_images", []) if str(item))
        paths = [self.project_dir / item for item in references if item]
        was_visible = self.isVisible()
        was_maximized = self.isMaximized()
        if was_visible:
            self.hide()
            QApplication.processEvents()
            time.sleep(0.12)
        try:
            diagnostic = find_reference_matches(
                paths,
                float(action.data.get("confidence", self.project.settings.default_confidence)),
                excluded_regions=[],
                search_region=action.data.get("search_region"),
                grayscale=bool(action.data.get("grayscale", False)),
                match_priority=str(action.data.get("match_priority", "highest_confidence")),
                match_index=int(action.data.get("match_index", 1)),
                diagnostic_min_confidence=0.5,
            )
        except Exception as exc:
            if was_visible:
                self.showMaximized() if was_maximized else self.showNormal()
            show_error(self, "Target Test Failed", str(exc))
            return
        if was_visible:
            self.showMaximized() if was_maximized else self.showNormal()
            self.raise_()
            self.activateWindow()
        if not diagnostic.matches and not diagnostic.warnings:
            diagnostic.warnings.append(
                "No candidates reached 50%. Check display scaling, DPI, resolution, or capture a new reference."
            )
        self.log(
            f"image test: {len(diagnostic.matches)} candidate(s), "
            f"best={diagnostic.selected.confidence:.3f}, search time={diagnostic.duration:.2f}s"
        )
        dialog = MatchResultsDialog(
            diagnostic,
            (int(action.data.get("click_offset_x", 0)), int(action.data.get("click_offset_y", 0))),
            self,
        )
        dialog.highlight_requested.connect(lambda match: self._highlight_matches(diagnostic.matches, match))
        dialog.match_chosen.connect(lambda match: self._use_selected_image_match(action, match))
        dialog.exec()

    def _highlight_matches(self, matches, selected) -> None:
        if self.match_highlight_overlay:
            self.match_highlight_overlay.close()
            self.match_highlight_overlay.deleteLater()
        overlay = MatchHighlightOverlay(matches, selected)
        self.match_highlight_overlay = overlay
        overlay.show()
        QTimer.singleShot(2500, self._close_match_highlight)

    def _close_match_highlight(self) -> None:
        overlay = self.match_highlight_overlay
        self.match_highlight_overlay = None
        if overlay:
            overlay.close()
            overlay.deleteLater()

    def _use_selected_image_match(self, action: RpaAction, match) -> None:
        if action not in self.project.actions or not self.project_dir:
            return
        references = [str(action.data.get("image", ""))]
        references.extend(str(item) for item in action.data.get("reference_images", []) if str(item))
        try:
            selected_path = Path(match.reference_image).resolve()
            selected_index = next(
                index for index, reference in enumerate(references)
                if (self.project_dir / reference).resolve() == selected_path
            )
        except (StopIteration, OSError):
            selected_index = 0
        if selected_index:
            references.insert(0, references.pop(selected_index))
            action.data["image"] = references[0]
            action.data["reference_images"] = references[1:]
        action.data["match_priority"] = "match_index"
        action.data["match_index"] = int(match.match_index or 1)
        self.mark_dirty()
        self.editor.set_action(action, self.project_dir)
        self.log(
            f"selected image match #{match.match_index} from {Path(match.reference_image).name} "
            f"at ({match.x}, {match.y}), confidence={match.confidence:.3f}"
        )

    def select_image_search_region(self, action: RpaAction) -> None:
        if action not in self.project.actions or self.search_region_overlay is not None:
            return
        self.search_region_action = action
        self.target_capture_was_maximized = self.isMaximized()
        self.hide()
        QTimer.singleShot(200, self._start_image_search_region)

    def _start_image_search_region(self) -> None:
        overlay = RegionSelectionOverlay()
        self.search_region_overlay = overlay
        overlay.selected.connect(self._complete_image_search_region)
        overlay.canceled.connect(self._cancel_image_search_region)
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()

    def _complete_image_search_region(self, x: int, y: int, width: int, height: int) -> None:
        action = self.search_region_action
        if action in self.project.actions:
            action.data["search_region"] = {"x": x, "y": y, "width": width, "height": height}
            self.mark_dirty()
            self.log(f"image search area selected: ({x}, {y}), {width}x{height}")
        self._restore_after_image_search_region()
        if action in self.project.actions:
            self.editor.set_action(action, self.project_dir)

    def _cancel_image_search_region(self) -> None:
        self.log("image search-area selection cancelled")
        self._restore_after_image_search_region()

    def _restore_after_image_search_region(self) -> None:
        overlay = self.search_region_overlay
        self.search_region_overlay = None
        self.search_region_action = None
        if overlay:
            overlay.deleteLater()
        if self.target_capture_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def recapture_target(self, action: RpaAction) -> None:
        if not self.project_dir:
            show_error(self, "Recapture Target", "Open or create an automation first.")
            return
        if action not in self.project.actions or action.action not in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            show_error(self, "Recapture Target", "Select a Click step before recapturing a target.")
            return
        if self.target_capture_overlay is not None:
            return
        self.target_capture_action = action
        self.target_capture_was_maximized = self.isMaximized()
        self.hide()
        QTimer.singleShot(200, self._start_target_capture_overlay)

    def _start_target_capture_overlay(self) -> None:
        try:
            captured = screenshot_image()
            self.target_capture_origin = virtual_screen_origin()
            self.target_capture_overlay = TargetCaptureOverlay(
                captured,
                self.project.settings.crop_width,
                self.project.settings.crop_height,
            )
            self.target_capture_overlay.confirmed.connect(self._complete_target_capture)
            self.target_capture_overlay.canceled.connect(self._cancel_target_capture)
            self.target_capture_overlay.show()
        except Exception as exc:
            self._restore_main_after_target_capture()
            show_error(self, "Recapture Target Failed", str(exc))

    def _complete_target_capture(self, x: int, y: int, width: int, height: int) -> None:
        overlay = self.target_capture_overlay
        action = self.target_capture_action
        if not overlay or not action or not self.project_dir:
            self._restore_main_after_target_capture()
            return
        image = str(action.data.get("image", ""))
        if not image:
            image = (Path("screenshots") / f"recaptured_{action.id[:8]}.png").as_posix()
        try:
            offset_x, offset_y, saved_width, saved_height = save_crop_from_image(
                self.project_dir / image,
                overlay.captured_image,
                x,
                y,
                width,
                height,
                *self.target_capture_origin,
            )
            action.data.update({
                "image": image,
                "fallback_x": x,
                "fallback_y": y,
                "click_offset_x": offset_x,
                "click_offset_y": offset_y,
                "crop_width": saved_width,
                "crop_height": saved_height,
            })
            self.mark_dirty()
            self.editor.set_action(action, self.project_dir)
            self.log(f"target recaptured for {action.summary()} at ({x}, {y})")
            self._restore_main_after_target_capture()
            self._show_message_near(x, y, "Target Recaptured", "The new target image and original click position were saved.")
        except Exception as exc:
            self._restore_main_after_target_capture()
            show_error(self, "Recapture Target Failed", str(exc))

    def _cancel_target_capture(self) -> None:
        self.log("target recapture cancelled")
        self._restore_main_after_target_capture()

    def _restore_main_after_target_capture(self) -> None:
        overlay = self.target_capture_overlay
        self.target_capture_overlay = None
        self.target_capture_action = None
        if overlay:
            overlay.deleteLater()
        if self.target_capture_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self.update_status()

    def _position_floating_toolbar(self) -> None:
        if not self.floating:
            return
        # Dock the recording toolbar at the bottom-center of the current screen,
        # out of the way of whatever the user is recording, instead of leaving it
        # wherever the window manager happens to place a new top-level window.
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return
        bounds = screen.availableGeometry()
        self.floating.adjustSize()
        left = bounds.left() + (bounds.width() - self.floating.width()) // 2
        top = bounds.bottom() - self.floating.height() - 16
        self.floating.move(left, top)

    def _show_message_near(self, x: int, y: int, title: str, text: str) -> None:
        box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, self)
        box.adjustSize()
        # Clamp against the union of every screen's geometry rather than a single
        # screen: QApplication.screenAt() can miss or pick the wrong monitor when
        # multiple screens have different DPI scaling, which previously sent the
        # dialog to the main window's screen instead of near the selected target.
        bounds = QRect()
        for screen in QApplication.screens():
            bounds = bounds.united(screen.availableGeometry())
        if bounds.isNull():
            bounds = box.geometry()
        # Center the dialog on the selected point instead of offsetting below-right,
        # which used to push it toward the bottom edge of the screen.
        left = min(max(x - box.width() // 2, bounds.left()), bounds.right() - box.width())
        top = min(max(y - box.height() // 2, bounds.top()), bounds.bottom() - box.height())
        box.move(left, top)
        box.exec()

    def set_action_status(self, index: int, status: str) -> None:
        if 0 <= index < len(self.project.actions):
            self.running_action_index = index
            self.project.actions[index].status = status
            self.table.update_action(index, self.project.actions[index])
            self.table.selectRow(index)
            if status == "running":
                self.log(f"[Step {index + 1}] Running: {self.project.actions[index].summary()}")
                if self.execution_floating:
                    self.execution_floating.set_status(f"Step {index + 1} · Running")
                    self._position_execution_toolbar()
            elif status == "failed" and self.execution_floating:
                self.execution_floating.set_status(f"Step {index + 1} · Failed")
                self._position_execution_toolbar()
            self.update_status("Running")

    def generate_python(self) -> None:
        if not self.ensure_project_dir():
            return
        issues = self._validation_issues(0, len(self.project.actions) - 1)
        self._show_validation_results(issues)
        if any(issue.level == LEVEL_ERROR for issue in issues):
            show_error(self, "Validation failed", "Fix the errors shown in the Validation panel before generating Python.")
            return
        path = generate_python(self.project, self.project_dir)
        self.log(f"Python file generated: {path}")

    def add_manual_action(self, position: str | None = None) -> None:
        if not self.ensure_project_dir():
            return
        before_count = len(self.project.actions)
        self.log(f"[Add Step] opening dialog; project has {before_count} steps")
        available_variables = dict(self.project.variables)
        for name in self.project.runtime_inputs:
            available_variables[name] = "Runtime Input"
        for name in self.project.output_variables:
            available_variables[name] = "Output Variable"
        for name in ("RUN_DATE", "CLIPBOARD_TEXT", "LAST_CLICK_X", "LAST_CLICK_Y"):
            available_variables[name] = "Built-in"
        dialog = ManualActionDialog(
            self.project.settings, available_variables, self, project_dir=self.project_dir,
        )
        dialog.screen_pick_requested.connect(lambda role: self._begin_manual_target_capture(dialog, role))
        dialog.test_match_requested.connect(self.test_target)
        dialog.test_step_requested.connect(lambda action: self._test_manual_action(dialog, action))
        dialog.diagnostic.connect(self.log)
        result = dialog.exec()
        self.log(f"[Add Step] dialog result: {'accepted' if result == QDialog.Accepted else 'cancelled'}")
        if result != QDialog.Accepted:
            return
        action = dialog.action()
        self.log(f"[Add Step] action created: {action.action} ({action.summary()})")
        self._materialize_manual_image(action)
        if action.action == ActionType.PYTHON_CODE.value and not self.confirm_python_code_warning():
            return
        if not self.insert_action(action, position):
            return
        self.log(f"[Add Step] project steps: {before_count} -> {len(self.project.actions)}")
        self.update_status("Manual step added")

    def _materialize_manual_image(self, action: RpaAction) -> None:
        """Import a manually chosen image into the flow so projects stay portable."""
        image = str(action.data.get("image", ""))
        source = Path(image)
        if not image or not source.is_absolute() or not source.exists() or not self.project_dir:
            return
        destination = self.project_dir / "screenshots" / f"manual_{action.id[:8]}{source.suffix.lower() or '.png'}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        action.data["image"] = destination.relative_to(self.project_dir).as_posix()

    def _begin_manual_target_capture(self, dialog: ManualActionDialog, role: str) -> None:
        if self._valid_manual_capture_dialog() is not None or not shiboken6.isValid(dialog):
            return
        snapshot = dialog.begin_picker(role)
        if snapshot is None:
            self.log(f"[{role}] picker could not start because its form is no longer available")
            return
        self._manual_capture_token += 1
        token = self._manual_capture_token
        self.manual_capture_dialog = dialog
        self.manual_capture_role = role
        self.manual_capture_snapshot = snapshot
        owner_ref = weakref.ref(self)
        def parent_ended(_value=None, capture_token=token, ref=owner_ref) -> None:
            owner = ref()
            if owner is not None and shiboken6.isValid(owner):
                owner._manual_capture_parent_destroyed(capture_token)
        dialog.destroyed.connect(parent_ended)
        dialog.finished.connect(parent_ended)
        self.target_capture_was_maximized = self.isMaximized()
        # Hiding a modal QDialog that is inside exec() terminates its nested
        # event loop as Rejected. Keep it alive while making it invisible and
        # non-interactive behind the independent picker overlay.
        dialog.setWindowOpacity(0.0)
        self.hide()
        if role == "window_target":
            self.log("[Window Picker] opened")
        else:
            self.log("[Image Picker] opened")
        self._manual_capture_timer.start(200)

    def _valid_manual_capture_dialog(self) -> ManualActionDialog | None:
        dialog = self.manual_capture_dialog
        return dialog if dialog is not None and shiboken6.isValid(dialog) else None

    def _start_pending_manual_capture(self) -> None:
        if self.manual_capture_role == "window_target":
            self._start_manual_window_capture()
        else:
            self._start_manual_target_capture()

    def _start_manual_window_capture(self) -> None:
        dialog = self._valid_manual_capture_dialog()
        if dialog is None:
            self._abandon_manual_capture()
            return
        try:
            self.window_pick_overlay = WindowPickOverlay(parent=dialog)
            self.window_pick_overlay.picked.connect(self._complete_manual_window_capture)
            self.window_pick_overlay.canceled.connect(self._cancel_manual_target_capture)
            self.window_pick_overlay.show()
        except Exception as exc:
            self._cancel_manual_target_capture()
            show_error(self, "Pick Window Failed", str(exc))

    def _complete_manual_window_capture(self, x: int, y: int) -> None:
        dialog = self._valid_manual_capture_dialog()
        if dialog is None:
            self._abandon_manual_capture()
            return
        try:
            backend = NativeWindowBackend()
            # Native cursor coordinates stay aligned with GetWindowRect under
            # per-monitor DPI scaling; the Qt point is retained as a fallback.
            try:
                x, y = backend.cursor_position()
            except OSError:
                pass
            window = backend.window_at_point(x, y, exclude_process_id=os.getpid())
            dialog.set_window_target(window.target(), window.evidence(), (x, y))
            self.log(
                f"[Window Picker] captured: process={window.process_name or 'unknown'}, "
                f"title={window.title!r}, class={window.class_name!r}"
            )
            self._restore_manual_capture_dialog("accepted")
        except Exception as exc:
            self.log(f"[Window Picker] failed: {exc}")
            self._restore_manual_capture_dialog("rejected")
            show_error(self, "Pick Window Failed", str(exc))

    def _start_manual_target_capture(self) -> None:
        dialog = self._valid_manual_capture_dialog()
        if dialog is None:
            self._abandon_manual_capture()
            return
        try:
            captured = screenshot_image()
            self.target_capture_origin = virtual_screen_origin()
            self.target_capture_overlay = TargetCaptureOverlay(
                captured,
                self.project.settings.crop_width,
                self.project.settings.crop_height,
                parent=dialog,
            )
            self.target_capture_overlay.confirmed.connect(self._complete_manual_target_capture)
            self.target_capture_overlay.canceled.connect(self._cancel_manual_target_capture)
            self.target_capture_overlay.show()
        except Exception as exc:
            self._cancel_manual_target_capture()
            show_error(self, "Pick on Screen Failed", str(exc))

    def _complete_manual_target_capture(self, x: int, y: int, width: int, height: int) -> None:
        dialog, overlay = self._valid_manual_capture_dialog(), self.target_capture_overlay
        if dialog is None:
            self._abandon_manual_capture()
            return
        self.log("[Image Picker] confirmed")
        image = None
        capture_image = bool(self.manual_capture_snapshot.get("capture_image", False))
        if overlay and shiboken6.isValid(overlay) and capture_image and self.project_dir:
            image = (Path("screenshots") / f"manual_target_{int(time.time() * 1000)}.png").as_posix()
            try:
                offset_x, offset_y, _width, _height = save_crop_from_image(self.project_dir / image, overlay.captured_image, x, y, width, height, *self.target_capture_origin)
                dialog.set_screen_point(self.manual_capture_role, x, y, image, (offset_x, offset_y))
            except Exception as exc:
                self.log(f"Target image was not saved: {exc}")
                dialog.set_screen_point(self.manual_capture_role, x, y)
        else:
            dialog.set_screen_point(self.manual_capture_role, x, y)
        self._restore_manual_capture_dialog("accepted")

    def _cancel_manual_target_capture(self) -> None:
        self._restore_manual_capture_dialog("rejected")

    def _restore_manual_capture_dialog(self, result: str) -> None:
        self._manual_capture_timer.stop()
        overlay, dialog = self.target_capture_overlay, self._valid_manual_capture_dialog()
        self.target_capture_overlay = None
        window_overlay = self.window_pick_overlay
        self.window_pick_overlay = None
        self.manual_capture_dialog = None
        self.manual_capture_snapshot = {}
        if overlay and shiboken6.isValid(overlay):
            overlay.confirmed.disconnect(self._complete_manual_target_capture)
            overlay.canceled.disconnect(self._cancel_manual_target_capture)
            overlay.deleteLater()
        if window_overlay and shiboken6.isValid(window_overlay):
            window_overlay.picked.disconnect(self._complete_manual_window_capture)
            window_overlay.canceled.disconnect(self._cancel_manual_target_capture)
            window_overlay.deleteLater()
        if self.target_capture_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        if dialog is not None:
            dialog.finish_picker()
            dialog.setWindowOpacity(1.0)
            dialog.raise_()
            dialog.activateWindow()
            picker = "Window Picker" if self.manual_capture_role == "window_target" else "Image Picker"
            self.log(f"[{picker}] closed: {result}")
            self.log("[Add Step] still open")

    def _manual_capture_parent_destroyed(self, token: int) -> None:
        if not shiboken6.isValid(self):
            return
        if token != self._manual_capture_token or self.manual_capture_dialog is None:
            return
        self._abandon_manual_capture()

    def _abandon_manual_capture(self) -> None:
        """End a child picker without dereferencing a destroyed parent dialog."""
        if not shiboken6.isValid(self):
            return
        if shiboken6.isValid(self._manual_capture_timer):
            self._manual_capture_timer.stop()
        overlay, window_overlay = self.target_capture_overlay, self.window_pick_overlay
        self.target_capture_overlay = None
        self.window_pick_overlay = None
        self.manual_capture_dialog = None
        self.manual_capture_snapshot = {}
        if overlay is not None and shiboken6.isValid(overlay):
            overlay.confirmed.disconnect(self._complete_manual_target_capture)
            overlay.canceled.disconnect(self._cancel_manual_target_capture)
            overlay.close(); overlay.deleteLater()
        if window_overlay is not None and shiboken6.isValid(window_overlay):
            window_overlay.picked.disconnect(self._complete_manual_window_capture)
            window_overlay.canceled.disconnect(self._cancel_manual_target_capture)
            window_overlay.close(); window_overlay.deleteLater()
        if self.target_capture_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        self.log("[Picker] parent dialog closed; picker cancelled safely")

    def insert_action(self, action: RpaAction, position: str | None = None) -> bool:
        index = self.table.selected_index()
        if index < 0:
            insert_at = len(self.project.actions)
        elif position == "before":
            insert_at = index
        else:
            insert_at = index + 1
        inserted = [action]
        if action.action in IF_TYPES:
            inserted.append(RpaAction(ActionType.END_IF.value, {}))
        elif action.action in LOOP_TYPES:
            inserted.append(RpaAction(ActionType.END_LOOP.value, {}))
        targets = jump_targets(self.project.actions)
        prospective = self.project.actions[:insert_at] + inserted + self.project.actions[insert_at:]
        structure = parse_control_flow(prospective)
        if action.action in CONTROL_TYPES and structure.issues:
            reason = structure.issues[0].reason
            show_error(
                self,
                "Cannot insert control step",
                f"This would create an invalid block: {reason}. Select a position inside the matching block and try again.",
            )
            self.log(f"[Add Step] control step rejected: {reason}")
            return False
        self.project.actions[insert_at:insert_at] = inserted
        restore_jump_targets(self.project.actions, targets)
        self.mark_dirty()
        # A filtered list can otherwise make a successfully added step appear
        # to vanish. Show the new work immediately and select it for review.
        if self.filter_box.text():
            self.filter_box.clear()
        self.refresh()
        self.table.selectRow(insert_at)
        self.table.scrollToItem(self.table.item(insert_at, 0))
        self.log(f"[Add Step] table refreshed to {self.table.rowCount()} rows; selected step {insert_at + 1}")
        if len(inserted) == 2:
            self.log(f"[Add Step] added matching {inserted[1].summary()} at Step {insert_at + 2}")
        return True

    def delete_action(self) -> None:
        indices = self.table.selected_indices()
        prospective, error = delete_steps(self.project.actions, indices)
        if error:
            self._step_edit_error("Cannot Delete Steps", error)
            return
        selected = min(indices) if indices else 0
        self._apply_step_edit(prospective, [min(selected, len(prospective) - 1)] if prospective else [], "deleted selected steps")

    def duplicate_action(self) -> None:
        indices = self.table.selected_indices()
        payload, error = clipboard_payload(self.project.actions, indices)
        if error:
            self._step_edit_error("Cannot Duplicate Steps", error)
            return
        flow = parse_control_flow(self.project.actions)
        insert_at = (
            flow.group_ends[indices[0]] + 1
            if len(indices) == 1 and self.project.actions[indices[0]].action in BLOCK_OPENERS
            and indices[0] in flow.group_ends else max(indices) + 1
        )
        prospective, selected, error = paste_payload(self.project.actions, payload, insert_at)
        if error:
            self._step_edit_error("Cannot Duplicate Steps", error)
            return
        self._apply_step_edit(prospective, selected, f"duplicated {len(selected)} step(s)")

    def move_action(self, delta: int) -> None:
        indices = self.table.selected_indices()
        if not indices:
            return
        destination = (min(indices) - 1) if delta < 0 else (max(indices) + 2)
        self.reorder_selected_steps(indices, destination)

    def reorder_selected_steps(self, indices: list[int], destination: int) -> None:
        if self.filter_box.text().strip():
            self._step_edit_error("Cannot Reorder Filtered Steps", "Clear the step filter before reordering so hidden rows cannot affect placement.")
            return
        flow = parse_control_flow(self.project.actions)
        if len(indices) == 1 and self.project.actions[indices[0]].action in BLOCK_OPENERS and indices[0] in flow.group_ends:
            indices = list(range(indices[0], flow.group_ends[indices[0]] + 1))
        prospective, error = reorder_steps(self.project.actions, indices, destination)
        if error:
            self._step_edit_error("Cannot Move Steps", error)
            return
        moved_ids = {self.project.actions[index].id for index in indices}
        selected = [index for index, action in enumerate(prospective) if action.id in moved_ids]
        self._apply_step_edit(prospective, selected, f"moved {len(selected)} step(s)")

    def copy_steps(self) -> bool:
        payload, error = clipboard_payload(self.project.actions, self.table.selected_indices())
        if error:
            self._step_edit_error("Cannot Copy Steps", error)
            return False
        QApplication.clipboard().setText(json.dumps(payload))
        self.log(f"copied {len(payload['actions'])} step(s)")
        return True

    def cut_steps(self) -> None:
        if self.copy_steps():
            self.delete_action()

    def paste_steps(self) -> None:
        try:
            payload = json.loads(QApplication.clipboard().text())
        except (TypeError, json.JSONDecodeError):
            self._step_edit_error("Cannot Paste Steps", "The clipboard does not contain copied RPA steps.")
            return
        indices = self.table.selected_indices()
        insert_at = max(indices) + 1 if indices else len(self.project.actions)
        prospective, selected, error = paste_payload(self.project.actions, payload, insert_at)
        if error:
            self._step_edit_error("Cannot Paste Steps", error)
            return
        if self.filter_box.text():
            self.filter_box.clear()
        self._apply_step_edit(prospective, selected, f"pasted {len(selected)} step(s)")

    def add_comment(self) -> None:
        text, accepted = QInputDialog.getMultiLineText(self, "Add Comment", "Comment or note")
        if not accepted or not text.strip():
            return
        insert_at = max(self.table.selected_indices(), default=len(self.project.actions) - 1) + 1
        targets = jump_targets(self.project.actions)
        prospective = list(self.project.actions)
        prospective.insert(insert_at, RpaAction(ActionType.COMMENT.value, {"text": text.strip()}))
        restore_jump_targets(prospective, targets)
        self._apply_step_edit(prospective, [insert_at], "added comment")

    def group_selected_steps(self) -> None:
        selected, error = complete_contiguous_selection(self.project.actions, self.table.selected_indices())
        if error:
            self._step_edit_error("Cannot Group Steps", error)
            return
        name, accepted = QInputDialog.getText(self, "Group Steps", "Group name")
        if not accepted or not name.strip():
            return
        group_id = str(uuid4())
        targets = jump_targets(self.project.actions)
        prospective = list(self.project.actions)
        prospective.insert(selected[0], RpaAction(ActionType.GROUP_START.value, {
            "name": name.strip(), "group_id": group_id, "collapsed": False,
        }))
        prospective.insert(selected[-1] + 2, RpaAction(ActionType.GROUP_END.value, {"group_id": group_id}))
        error = validate_structure(prospective)
        if error:
            self._step_edit_error("Cannot Group Steps", error)
            return
        restore_jump_targets(prospective, targets)
        self._apply_step_edit(prospective, [selected[0]], f"grouped {len(selected)} step(s) as {name.strip()}")

    def move_selected_into_group(self) -> None:
        selected, error = complete_contiguous_selection(self.project.actions, self.table.selected_indices())
        if error:
            self._step_edit_error("Cannot Move Into Group", error)
            return
        flow = parse_control_flow(self.project.actions)
        groups = [index for index in flow.group_start_end if index not in selected]
        if not groups:
            self._step_edit_error("Cannot Move Into Group", "Create another named group first.")
            return
        labels = [f"{self.project.actions[index].summary()} (Step {index + 1})" for index in groups]
        label, accepted = QInputDialog.getItem(self, "Move Into Group", "Destination group", labels, 0, False)
        if not accepted:
            return
        group_index = groups[labels.index(label)]
        end_index = flow.group_start_end[group_index]
        if group_index < selected[0] and selected[-1] < end_index:
            self._step_edit_error("Cannot Move Into Group", "The selected steps are already inside that group.")
            return
        self.reorder_selected_steps(selected, end_index)

    def move_selected_out_of_group(self) -> None:
        selected, error = complete_contiguous_selection(self.project.actions, self.table.selected_indices())
        if error:
            self._step_edit_error("Cannot Move Out of Group", error)
            return
        flow = parse_control_flow(self.project.actions)
        enclosing = [
            (start, end) for start, end in flow.group_start_end.items()
            if start < selected[0] and selected[-1] < end
        ]
        if not enclosing:
            self._step_edit_error("Cannot Move Out of Group", "The selected range is not inside a named group.")
            return
        _start, end = max(enclosing, key=lambda pair: pair[0])
        self.reorder_selected_steps(selected, end + 1)

    def adjust_selected_wait(self) -> None:
        indices = [index for index in self.table.selected_indices() if self.project.actions[index].action not in NON_EXECUTABLE_TYPES]
        if not indices:
            return
        current = self.project.actions[indices[0]].delay_before
        seconds, accepted = QInputDialog.getDouble(
            self, "Set Wait Before", "Seconds before each selected step", current, 0.0, 86400.0, 2,
        )
        if not accepted:
            return
        for index in indices:
            self.project.actions[index].delay_before = seconds
        self.mark_dirty()
        self.refresh()
        self._select_step_rows(indices)
        self.log(f"set Wait Before to {seconds:.2f}s for {len(indices)} step(s)")

    def _apply_step_edit(self, actions: list[RpaAction] | None, selected: list[int], message: str) -> None:
        if actions is None:
            return
        self.project.actions = actions
        self.mark_dirty()
        self.refresh()
        self._select_step_rows(selected)
        self.log(message)

    def _select_step_rows(self, rows: list[int]) -> None:
        self.table.clearSelection()
        for row in rows:
            if 0 <= row < self.table.rowCount():
                self.table.selectionModel().select(
                    self.table.model().index(row, 0),
                    QItemSelectionModel.Select | QItemSelectionModel.Rows,
                )
        if rows and 0 <= rows[0] < self.table.rowCount():
            self.table.setCurrentCell(rows[0], 0, QItemSelectionModel.NoUpdate)
            self.table.scrollToItem(self.table.item(rows[0], 0))

    def _step_edit_error(self, title: str, reason: str) -> None:
        show_error(self, title, reason)
        self.log(f"step editing rejected: {reason}")

    def toggle_action(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self.project.actions):
            if self.project.actions[index].action in NON_EXECUTABLE_TYPES:
                show_error(self, "Control steps stay enabled", "If, Else, End, Repeat, and Break markers cannot be disabled because they define the flow structure.")
                return
            self.project.actions[index].enabled = enabled
            self.mark_dirty()
            self.table.update_action(index, self.project.actions[index])
            self.update_buttons()

    def toggle_selected_action(self) -> None:
        indices = self.table.selected_indices()
        if not indices:
            return
        executable = [index for index in indices if self.project.actions[index].action not in NON_EXECUTABLE_TYPES]
        if not executable:
            return
        enable = not all(self.project.actions[index].enabled for index in executable)
        self.set_selected_enabled(enable)

    def set_selected_enabled(self, enabled: bool) -> None:
        indices = [
            index for index in self.table.selected_indices()
            if self.project.actions[index].action not in NON_EXECUTABLE_TYPES
        ]
        if not indices:
            self._step_edit_error("No Executable Steps Selected", "Groups, comments, and control markers do not have an enabled state.")
            return
        for index in indices:
            self.project.actions[index].enabled = enabled
            self.table.update_action(index, self.project.actions[index])
        self.mark_dirty()
        self.editor.set_action(self.project.actions[indices[0]], self.project_dir)
        self.log(f"{'enabled' if enabled else 'disabled'} {len(indices)} step{'s' if len(indices) != 1 else ''}")

    def select_action(self) -> None:
        index = self.table.selected_index()
        action = self.project.actions[index] if 0 <= index < len(self.project.actions) else None
        self.editor.set_action(action, self.project_dir)
        if action:
            sizes = self.workspace_splitter.sizes()
            if len(sizes) == 2 and sizes[1] == 0:
                self.workspace_splitter.setSizes([700, 420])
        self.update_buttons()
        self.update_status()

    def toggle_breakpoint(self) -> None:
        indices = self.table.selected_indices()
        if not indices:
            return
        executable = [index for index in indices if self.project.actions[index].action not in NON_EXECUTABLE_TYPES]
        if not executable:
            show_error(self, "Breakpoint Not Available", "Breakpoints can be set only on executable steps, not block markers.")
            return
        enable = not all(self.project.actions[index].breakpoint for index in executable)
        for index in executable:
            self.project.actions[index].breakpoint = enable
            self.table.update_action(index, self.project.actions[index])
        self.mark_dirty()
        self.log(
            f"{'set' if enable else 'cleared'} breakpoint on "
            + ", ".join(f"Step {index + 1}" for index in executable)
        )

    def clear_step_selection(self) -> None:
        self.table.clearSelection()
        self.editor.set_action(None, self.project_dir)
        self.table.setFocus(Qt.OtherFocusReason)
        self.update_buttons()
        self.update_status("Selection cleared")

    def close_details(self) -> None:
        self.clear_step_selection()

    def variables_dialog(self) -> None:
        current = {}
        if self.replay_worker:
            current = dict(self.replay_worker.runner.runtime_variables)
        elif self.last_runtime_variables:
            current = dict(self.last_runtime_variables)
        dialog = VariablesDialog(self.project, current, self)
        if dialog.exec() == QDialog.Accepted:
            self.project.variables = dialog.variables
            self.project.variable_definitions = dialog.variable_definitions
            self.project.runtime_inputs = dialog.runtime_inputs
            self.project.output_variables = dialog.output_variables
            self.mark_dirty()

    def settings_dialog(self) -> None:
        dialog = SettingsDialog(self.project.settings, self, self.project)
        if dialog.exec() == QDialog.Accepted:
            self.mark_dirty()

    def mark_dirty(self) -> None:
        self.dirty = True
        self._record_history()
        index = self.table.selected_index()
        if 0 <= index < len(self.project.actions):
            self.table.update_action(index, self.project.actions[index])
            self.table.apply_filter(self.filter_box.text())
        self.update_status()

    def _reset_history(self) -> None:
        self._history = [self.project.to_dict()]
        self._history_index = 0

    def _record_history(self) -> None:
        if self._restoring_history or self.recorder is not None:
            return
        snapshot = self.project.to_dict()
        if self._history and self._history[self._history_index] == snapshot:
            return
        del self._history[self._history_index + 1:]
        self._history.append(snapshot)
        self._history_index = len(self._history) - 1
        # Keep editing history useful without allowing a long recording session
        # to grow memory without bound.
        if len(self._history) > 100:
            self._history.pop(0)
            self._history_index -= 1
        self.update_buttons()

    def undo(self) -> None:
        if self._history_index <= 0:
            return
        self._restore_history(self._history_index - 1, "Undo")

    def redo(self) -> None:
        if self._history_index + 1 >= len(self._history):
            return
        self._restore_history(self._history_index + 1, "Redo")

    def _restore_history(self, index: int, label: str) -> None:
        selected = self.table.selected_index()
        self._restoring_history = True
        try:
            self.project = RpaProject.from_dict(self._history[index])
            self._history_index = index
            self.dirty = True
            self.refresh()
            if 0 <= selected < len(self.project.actions):
                self.table.selectRow(selected)
            self.log(f"{label.lower()} applied")
        finally:
            self._restoring_history = False

    def log(self, message: str) -> None:
        message = mask_sensitive_text(message, self._active_secret_values)
        level, color = self._log_style(message)
        self.logs.append(f'<span style="color:{color}">[{level}] {escape(str(message))}</span>')
        if self._logs_follow_tail:
            bar = self.logs.verticalScrollBar()
            bar.setValue(bar.maximum())
        if self.file_logger:
            self.file_logger.info(message)

    def _log_style(self, message: str) -> tuple[str, str]:
        lowered = str(message).lower()
        if any(word in lowered for word in ("failed", "error", "exception", "stopped")):
            return "Error", "#dc2626"
        if any(word in lowered for word in ("warning", "skipped", "could not")):
            return "Warning", "#ca8a04"
        if any(word in lowered for word in ("completed", "success", "saved", "created")):
            return "Success", "#16a34a"
        return "Info", "#334155"

    def _on_log_scroll(self, value: int) -> None:
        bar = self.logs.verticalScrollBar()
        self._logs_follow_tail = value >= bar.maximum() - 2

    def find_log(self) -> None:
        text = self.log_search.text().strip()
        if text and not self.logs.find(text):
            cursor = self.logs.textCursor()
            cursor.movePosition(cursor.Start)
            self.logs.setTextCursor(cursor)
            self.logs.find(text)

    def save_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "rpa-run.log", "Log files (*.log *.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self.logs.toPlainText(), encoding="utf-8")
            self.statusBar().showMessage(f"Log saved: {path}", 4000)
        except OSError as exc:
            show_error(self, "Save Log Failed", str(exc))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if Path(url.toLocalFile()).suffix.lower() == ".json":
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".json":
                self.open_project_path(path)
                event.acceptProposedAction()
                return
        event.ignore()

    def open_project_path(self, path: Path) -> None:
        try:
            self.project = self.manager.load(path)
            self.project_dir = path.parent
            self._load_latest_evidence()
            self.dirty = False
            self._reset_history()
            self._remember_project_path()
            self.log(f"opened project: {path}")
            self.refresh()
        except Exception as exc:
            show_error(self, "Open failed", str(exc))

    def _open_referenced_subflow(self, reference: str) -> None:
        if not self.project_dir:
            return
        target = (Path(self.project_dir) / reference).resolve()
        if not target.is_file():
            show_error(self, "Open Subflow", f"The referenced flow is missing:\n{reference}")
            return
        if self.dirty:
            answer = QMessageBox.question(
                self, "Save current flow?",
                "Save changes before opening the referenced flow?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save:
                self.save_project()
                if self.dirty:
                    return
        self.open_project_path(target)

    def _remember_project_path(self) -> None:
        if self.project_dir:
            self.settings.setValue("last_project_path", str(self.project_dir / "project.json"))

    def _load_latest_evidence(self) -> None:
        self.last_evidence_folder = None
        self.run_log_path = None
        if not self.project_dir:
            return
        runs_root = Path(self.project_dir) / "runs"
        try:
            folders = sorted((path for path in runs_root.iterdir() if path.is_dir()), reverse=True)
        except OSError:
            return
        if folders:
            self.last_evidence_folder = folders[0]
            log_path = folders[0] / "execution.log"
            self.run_log_path = log_path if log_path.is_file() else None

    def _open_last_project(self) -> None:
        value = self.settings.value("last_project_path", "", type=str)
        if not value:
            return
        path = Path(value)
        if not path.is_file():
            self.settings.remove("last_project_path")
            return
        try:
            self.project = self.manager.load(path)
            self.project_dir = path.parent
            self._load_latest_evidence()
            self.dirty = False
            self._reset_history()
            self.log(f"reopened last project: {path}")
            self.refresh()
        except Exception as exc:
            self.settings.remove("last_project_path")
            self.log(f"could not reopen last project: {exc}")

    def closeEvent(self, event) -> None:
        self._save_layout_settings()
        if self.recorder:
            if QMessageBox.question(self, "Recording active", "Stop recording and close?") != QMessageBox.Yes:
                event.ignore()
                return
            self.recorder.stop(True)
        if self.replay_worker:
            if QMessageBox.question(self, "Replay active", "Stop replay and close?") != QMessageBox.Yes:
                event.ignore()
                return
            self.replay_worker.stop()
        self.schedule_timer.stop()
        self._schedule_queue.clear()
        for flow_name, (thread, worker) in list(self._scheduled_runs.items()):
            worker.stop()
            thread.quit()
            thread.wait()
            self._scheduled_runs.pop(flow_name, None)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape and self.editor_scroll.isVisible() and not self._focus_is_editor():
            self.clear_step_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _install_shortcuts(self) -> None:
        for key, handler in [
            ("Ctrl+N", self.new_project),
            ("Ctrl+O", self.open_project),
            ("Ctrl+S", self.save_project),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Esc", self.clear_step_selection),
            ("Delete", self.delete_action),
            ("Ctrl+Insert", lambda: self.add_manual_action("before")),
            ("Shift+Insert", lambda: self.add_manual_action("after")),
            ("Ctrl+D", self.duplicate_action),
            ("Ctrl+C", self.copy_steps),
            ("Ctrl+X", self.cut_steps),
            ("Ctrl+V", self.paste_steps),
            ("Alt+Up", lambda: self.move_action(-1)),
            ("Alt+Down", lambda: self.move_action(1)),
            ("Ctrl+G", self.generate_python),
            ("F5", self.run_project),
            ("Shift+F5", self.stop_run),
            ("F9", self.toggle_breakpoint),
        ]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda h=handler: self._run_shortcut(h))

    def _run_shortcut(self, handler) -> None:
        if self._focus_is_editor():
            return
        handler()

    def _focus_is_editor(self) -> bool:
        return isinstance(QApplication.focusWidget(), (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QTextEdit))

    def handle_table_context_action(self, action: str) -> None:
        handlers = {
            "toggle_breakpoint": self.toggle_breakpoint,
            "test": self.test_selected_step,
            "run_from": self.run_from_here,
            "run_until": self.run_until_here,
            "toggle_enabled": self.toggle_selected_action,
            "enable": lambda: self.set_selected_enabled(True),
            "disable": lambda: self.set_selected_enabled(False),
            "adjust_wait": self.adjust_selected_wait,
            "copy": self.copy_steps,
            "cut": self.cut_steps,
            "paste": self.paste_steps,
            "add": self.add_manual_action,
            "comment": self.add_comment,
            "group": self.group_selected_steps,
            "move_into_group": self.move_selected_into_group,
            "move_out_group": self.move_selected_out_of_group,
            "insert_before": lambda: self.add_manual_action("before"),
            "insert_after": lambda: self.add_manual_action("after"),
            "duplicate": self.duplicate_action,
            "delete": self.delete_action,
            "move_up": lambda: self.move_action(-1),
            "move_down": lambda: self.move_action(1),
            "deselect": self.clear_step_selection,
        }
        handler = handlers.get(action)
        if handler:
            handler()

    def confirm_python_code_warning(self) -> bool:
        if self.settings.value("python_code_warning_dont_show", False, type=bool):
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Python Code Warning")
        box.setText("Python Code can read files, modify files, start programs, and access the system. Only run trusted code.")
        continue_btn = box.addButton("Continue", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        dont_show = box.addButton("Do Not Show Again", QMessageBox.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is dont_show:
            self.settings.setValue("python_code_warning_dont_show", True)
            return True
        return clicked is continue_btn

    def toggle_logs(self) -> None:
        expanded = not self.logs.isHidden()
        self.logs.setVisible(not expanded)
        self.toggle_logs_btn.setText("Expand Logs" if expanded else "Collapse Logs")
        self.settings.setValue("logs_expanded", not expanded)
        if not expanded:
            self.bottom_tabs.setCurrentWidget(self.logs_wrap)

    def open_run_log(self) -> None:
        if self.run_log_path and self.run_log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_log_path)))
            self.log(f"Run log: {self.run_log_path}")
        else:
            self.log("No run log has been created yet")

    def open_run_details(self) -> None:
        folder = self.active_evidence.folder if self.active_evidence else self.last_evidence_folder
        if folder is None:
            QMessageBox.information(self, "Run Details", "No detailed run report has been created yet.")
            return
        RunDetailsDialog(folder, self).exec()

    def _begin_evidence(self, project_dir: Path, project: RpaProject, source: str) -> RunEvidenceSession:
        evidence = RunEvidenceSession(
            project_dir,
            project.project.name or Path(project_dir).name,
            source,
            project.settings.evidence_retention_runs,
        )
        self.active_evidence = evidence
        self.last_evidence_folder = evidence.folder
        self.file_logger = evidence.logger
        self.run_log_path = evidence.log_path
        self._last_validation_issues = []
        self._active_history_flow = None
        try:
            root = flows_root().resolve()
            directory = Path(project_dir).resolve()
            if directory.parent == root:
                self._active_history_flow = directory.name
                schedule = self.schedule_store.get(directory.name)
                mark_started(
                    schedule, source=source, evidence_path=evidence.relative_folder, run_id=evidence.run_id,
                )
                self.schedule_store.set(schedule)
                self.schedule_store.save()
        except OSError as exc:
            self.log(f"Could not update run history: {exc}")
        return evidence

    def _create_standalone_evidence(
        self, project_dir: Path, flow_name: str, source: str,
    ) -> RunEvidenceSession | None:
        """Create evidence for a run attempt that never owns the main execution UI."""
        try:
            return RunEvidenceSession(project_dir, flow_name, source, 100)
        except OSError as exc:
            self.log(f"Could not create run evidence: {exc}")
            return None

    def _finish_active_history(
        self, status: str, error: str | None, failed_step: int | None, attempts: int,
        diagnostics: dict | None = None,
    ) -> None:
        flow_name = self._active_history_flow
        self._active_history_flow = None
        if not flow_name:
            return
        schedule = self.schedule_store.get(flow_name)
        safe_error = mask_sensitive_text(error, self._active_secret_values) if error else None
        safe_diagnostics = self._mask_evidence_value(diagnostics or {})
        mark_finished(
            schedule, status, error=safe_error, failed_step=failed_step, attempts=attempts,
            diagnostics=safe_diagnostics,
        )
        self.schedule_store.set(schedule)
        self.schedule_store.save()

    def _finalize_evidence(
        self,
        status: str,
        step_results: list[dict] | None = None,
        attempts: int = 0,
        failed_step: int | None = None,
        error: str | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        evidence = self.active_evidence
        if evidence is None:
            return
        try:
            safe_error = mask_sensitive_text(error, self._active_secret_values) if error else None
            safe_steps = self._mask_evidence_value(step_results) if step_results else step_results
            safe_diagnostics = self._mask_evidence_value(diagnostics or {})
            evidence.finalize(status, safe_steps, attempts, failed_step, safe_error, safe_diagnostics)
        except Exception as exc:
            # The UI and persisted scheduler result must survive a report write
            # failure (for example, a deleted or read-only runs folder).
            self.logs.append(f'<span style="color:#ca8a04">[Warning] Could not finalize run evidence: {escape(str(exc))}</span>')
            evidence.close()
        finally:
            self.last_evidence_folder = evidence.folder
            self.active_evidence = None
            if self.file_logger is evidence.logger:
                self.file_logger = None
            self._active_secret_values.clear()

    def _mask_evidence_value(self, value):
        if isinstance(value, str):
            return mask_sensitive_text(value, self._active_secret_values)
        if isinstance(value, list):
            return [self._mask_evidence_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._mask_evidence_value(item) for key, item in value.items()}
        return value

    def update_status(self, prefix: str = "Ready") -> None:
        recording = self.recorder is not None and self.recorder.state == RecorderState.RECORDING
        paused = self.recorder is not None and self.recorder.state == RecorderState.PAUSED
        running = self.replay_thread is not None
        if recording or paused:
            elapsed = int(time.monotonic() - self.recording_started_at) if self.recording_started_at else 0
            state = "Paused" if paused else "Recording"
            self.statusBar().showMessage(f"{state} | {len(self.project.actions)} steps | {elapsed // 60:02d}:{elapsed % 60:02d} elapsed")
            return
        if running and self.running_action_index is not None and 0 <= self.running_action_index < len(self.project.actions):
            action = self.project.actions[self.running_action_index]
            self.statusBar().showMessage(f"Running | Step {self.running_action_index + 1} of {len(self.project.actions)} | {action.friendly_name()}")
            return
        selected = self.table.selected_index()
        selected_text = f"Step {selected + 1} selected" if selected >= 0 else "No step selected"
        folder = str(self.project_dir) if self.project_dir else "No recording loaded"
        modified = "Modified" if self.dirty else "Saved"
        self.statusBar().showMessage(f"{prefix} | {len(self.project.actions)} steps | {selected_text} | {folder} | {modified}")

    def _restore_layout_settings(self) -> None:
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)
        workspace = self.settings.value("workspace_splitter")
        vertical = self.settings.value("vertical_splitter")
        if workspace:
            self.workspace_splitter.restoreState(workspace)
        if vertical:
            self.vertical_splitter.restoreState(vertical)
        widths = self.settings.value("table_column_widths")
        if isinstance(widths, list):
            for index, width in enumerate(widths):
                if index < self.table.columnCount():
                    self.table.setColumnWidth(index, int(width))
        advanced = self.settings.value("advanced_expanded", False, type=bool)
        self.editor.set_advanced_expanded(advanced)
        logs_expanded = self.settings.value("logs_expanded", True, type=bool)
        self.logs.setVisible(logs_expanded)
        self.toggle_logs_btn.setText("Collapse Logs" if logs_expanded else "Expand Logs")

    def _save_layout_settings(self) -> None:
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("workspace_splitter", self.workspace_splitter.saveState())
        self.settings.setValue("vertical_splitter", self.vertical_splitter.saveState())
        self.settings.setValue("table_column_widths", [self.table.columnWidth(i) for i in range(self.table.columnCount())])
