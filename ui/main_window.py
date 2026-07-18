from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import shutil
from datetime import datetime, timezone
from html import escape
import time
import sys

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QDialog,
    QHBoxLayout,
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
    QDoubleSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from rpa.generator import generate_python
from rpa.image_matcher import find_image, save_crop_from_image, screenshot_image, virtual_screen_origin
from rpa.models import ActionType, RecorderState, RpaAction, RpaProject
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
from rpa.validator import validate_project
from rpa.utils import create_file_logger, foreground_elevation_mismatch
from ui.action_editor import ActionEditor
from ui.action_table import ActionTable
from ui.dialogs import ManualActionDialog, SettingsDialog, VariablesDialog, load_default_project_settings, show_error
from ui.recorder_toolbar import FloatingExecutionToolbar, FloatingRecorderToolbar
from ui.schedule_dialog import ScheduleFlowsDialog
from ui.target_capture import TargetCaptureOverlay


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
    log = Signal(str)
    finished = Signal()
    failed = Signal(int, str)
    stopped = Signal()

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
    ) -> None:
        super().__init__()
        self.runner = ReplayRunner(project, project_dir, self.log.emit, excluded_regions)
        self.start_index = start_index
        self.end_index = end_index
        self.include_start_delay = include_start_delay
        self.respect_enabled = respect_enabled
        if runtime_variables is not None:
            self.runner.runtime_variables = dict(runtime_variables)

    @Slot()
    def run(self) -> None:
        try:
            self.runner.run(
                self.action_status.emit,
                self.start_index,
                self.end_index,
                self.include_start_delay,
                self.respect_enabled,
            )
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
        self.replay_was_maximized = False
        self.replay_thread: QThread | None = None
        self.replay_worker: ReplayWorker | None = None
        self.run_log_path: Path | None = None
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
        self.details_were_visible_before_run = True
        self.target_capture_overlay: TargetCaptureOverlay | None = None
        self.target_capture_action: RpaAction | None = None
        self.target_capture_origin = (0, 0)
        self.target_capture_was_maximized = False
        self.manual_capture_dialog: ManualActionDialog | None = None
        self.manual_capture_role = "target"
        self.settings = QSettings("PythonRPARecorder", "PythonRPARecorder")
        self.schedule_store = ScheduleStore(flows_root())
        self._scheduled_runs: dict[str, tuple[QThread, ReplayWorker]] = {}
        self._schedule_queue: list[str] = []
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(15000)
        self.schedule_timer.timeout.connect(self._check_schedules)
        self.schedule_timer.start()
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()
        self.refresh()
        self._reset_history()
        self._restore_layout_settings()

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
            ("Editing", [("Add Manual Action", "+ Add Step"), ("Insert Before", "Insert Before"), ("Insert After", "Insert After"), ("Duplicate", "⧉ Duplicate"), ("Delete Action", "Delete"), ("Move Up", "↑"), ("Move Down", "↓"), ("Deselect All", "Deselect"), ("Variables", "Variables"), ("Settings", "Settings")]),
        ]
        groups[2] = (groups[2][0], [item for item in groups[2][1] if item[0] not in ("Variables", "Settings")])
        groups[2][1].insert(7, ("Enable/Disable", "Enable/Disable"))
        # Keep the toolbar focused on the most common step actions. Insertion,
        # reordering and deselection remain available in the Step Editing menu
        # and the table context menu, where they are easier to discover without
        # turning the primary workspace into a wall of buttons.
        groups[2] = (groups[2][0], [item for item in groups[2][1] if item[0] in (
            "Add Manual Action", "Duplicate", "Delete Action", "Enable/Disable",
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
            if group_title == "Editing":
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
        self.logs.setFont(QFont("Consolas", 9))
        self.clear_logs_btn = QPushButton("Clear")
        self.copy_logs_btn = QPushButton("Copy")
        self.save_logs_btn = QPushButton("Save Log")
        self.open_log_btn = QPushButton("Open File")
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
        for btn in (self.clear_logs_btn, self.copy_logs_btn, self.save_logs_btn, self.open_log_btn, self.toggle_logs_btn):
            logs_header_layout.addWidget(btn)
        self.logs_wrap = QWidget()
        logs_layout = QVBoxLayout(self.logs_wrap)
        logs_layout.addWidget(logs_header)
        logs_layout.addWidget(self.logs)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.addWidget(table_wrap)
        self.workspace_splitter.addWidget(self.editor_scroll)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 2)
        self.vertical_splitter = QSplitter(Qt.Vertical)
        self.vertical_splitter.addWidget(self.workspace_splitter)
        self.vertical_splitter.addWidget(self.logs_wrap)
        self.vertical_splitter.setStretchFactor(0, 5)
        self.vertical_splitter.setStretchFactor(1, 1)
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
        self.vertical_splitter.setSizes([500, 280])

    def _build_menu_bar(self) -> None:
        menus = [
            ("File", ["New", "Open", "Save", "Save As"]),
            ("Record Actions", ["Record", "Pause", "Resume", "Stop"]),
            ("Execution", ["Run", "Test This Step", "Run From Here", "Run Until Here", "Stop Run", "Schedule Flows", "Generate Python"]),
            ("Step Editing", ["Undo", "Redo", "Add Manual Action", "Insert Before", "Insert After", "Duplicate", "Delete Action", "Move Up", "Move Down", "Enable/Disable", "Deselect All"]),
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
        self.menu_actions["Deselect All"].triggered.connect(self.clear_step_selection)
        self.menu_actions["Variables"].triggered.connect(self.variables_dialog)
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
        self.editor.action_changed.connect(self.mark_dirty)
        self.editor.close_requested.connect(self.clear_step_selection)
        self.editor.test_step_requested.connect(self.test_selected_step)
        self.editor.test_locator_requested.connect(self.test_target)
        self.editor.recapture_requested.connect(self.recapture_target)
        self.editor.advanced_changed.connect(lambda expanded: self.settings.setValue("advanced_expanded", expanded))
        self.clear_logs_btn.clicked.connect(self.logs.clear)
        self.copy_logs_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.logs.toPlainText()))
        self.save_logs_btn.clicked.connect(self.save_logs)
        self.open_log_btn.clicked.connect(self.open_run_log)
        self.toggle_logs_btn.clicked.connect(self.toggle_logs)
        self.log_search.returnPressed.connect(self.find_log)
        self.logs.verticalScrollBar().valueChanged.connect(self._on_log_scroll)
        self.action_recorded.connect(self._action_recorded)
        self.log_recorded.connect(self.log)
        self.recorder_failed.connect(self._recorder_failed)

    def refresh(self) -> None:
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
        self.buttons["Run"].setEnabled(bool(self.project.actions) and not recording and not paused and not preparing and not running)
        self.buttons["Record"].setEnabled(not running and not recording and not paused and not preparing)
        self.buttons["Stop Run"].setEnabled(running)
        for name in ("Pause", "Resume", "Stop", "Run", "Record", "Stop Run"):
            self.menu_actions[name].setEnabled(self.buttons[name].isEnabled())
        selected = self.table.selected_index() >= 0
        for name in ("Insert Before", "Insert After", "Duplicate", "Delete Action", "Move Up", "Move Down", "Enable/Disable", "Deselect All"):
            if name in self.buttons:
                self.buttons[name].setEnabled(selected)
            self.menu_actions[name].setEnabled(selected)
        for name in ("Test This Step", "Run From Here", "Run Until Here"):
            self.menu_actions[name].setEnabled(selected and not recording and not paused and not running)
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
                self.dirty = False
                self._reset_history()
                self.log(f"opened flow: {flow_dir}")
                return True
            except Exception as exc:
                show_error(self, "Open flow failed", str(exc))
                return False
        self.project = self.manager.new_project(flow_name, settings=load_default_project_settings())
        self.project_dir = flow_dir
        self.manager.save(self.project, self.project_dir)
        self.dirty = False
        self._reset_history()
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
        self.manager.save(self.project, self.project_dir)
        self.dirty = False
        self.log("project saved")
        self.update_status()

    def save_as_project(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Save project as")
        if not folder:
            return
        self.manager.save_as(self.project, self.project_dir, Path(folder))
        self.project_dir = Path(folder)
        self.dirty = False
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
        """Send Win+D before hooks exist, so desktop preparation is never recorded."""
        if sys.platform != "win32":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            key_up = 0x0002
            user32.keybd_event(0x5B, 0, 0, 0)
            user32.keybd_event(0x44, 0, 0, 0)
            user32.keybd_event(0x44, 0, key_up, 0)
            user32.keybd_event(0x5B, 0, key_up, 0)
        except Exception as exc:
            self.log(f"Could not show the desktop before recording: {exc}")

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

    def schedule_flows_dialog(self) -> None:
        dialog = ScheduleFlowsDialog(self.schedule_store, self.settings, self)
        dialog.run_now_requested.connect(lambda name: self._run_flow_now(name))
        dialog.exec()

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
            mark_skipped(schedule, STATUS_SKIPPED_RUNNING)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self.log(f"[{flow_name}] schedule skipped: already running")
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
            mark_skipped(schedule, STATUS_SKIPPED_BUSY)
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            self.log(f"[{flow_name}] schedule skipped: flow is open and busy")
            return
        try:
            project = ProjectManager().load(project_json)
        except Exception as exc:
            self.log(f"[{flow_name}] schedule failed to load: {exc}")
            return
        errors = validate_project(project, flow_dir)
        if errors:
            self.log(f"[{flow_name}] schedule skipped: {errors[0]}")
            schedule = self.schedule_store.get(flow_name)
            mark_finished(schedule, STATUS_FAILED, error=errors[0])
            self.schedule_store.set(schedule)
            self.schedule_store.save()
            return

        schedule = self.schedule_store.get(flow_name)
        mark_started(schedule)
        self.schedule_store.set(schedule)
        self.schedule_store.save()

        thread = QThread()
        worker = ReplayWorker(project, flow_dir, 0, len(project.actions) - 1, True, True, None, [])
        worker.flow_name = flow_name
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._scheduled_run_log)
        worker.finished.connect(self._scheduled_run_success)
        worker.stopped.connect(self._scheduled_run_stopped)
        worker.failed.connect(self._scheduled_run_failed)
        self._scheduled_runs[flow_name] = (thread, worker)
        prefix = "scheduled" if scheduled else "manual"
        self.log(f"[{flow_name}] {prefix} run starting")
        thread.start()

    def _scheduled_run_log(self, message: str) -> None:
        worker = self.sender()
        flow_name = getattr(worker, "flow_name", "")
        self.log(f"[{flow_name}] {message}" if flow_name else message)

    def _scheduled_run_success(self) -> None:
        worker = self.sender()
        self._scheduled_run_finished(getattr(worker, "flow_name", ""), STATUS_SUCCESS)

    def _scheduled_run_stopped(self) -> None:
        worker = self.sender()
        self._scheduled_run_finished(getattr(worker, "flow_name", ""), STATUS_STOPPED)

    def _scheduled_run_failed(self, index: int, message: str) -> None:
        worker = self.sender()
        self._scheduled_run_finished(getattr(worker, "flow_name", ""), STATUS_FAILED, error=message)

    def _scheduled_run_finished(self, flow_name: str, status: str, error: str | None = None) -> None:
        entry = self._scheduled_runs.pop(flow_name, None)
        if entry:
            thread, _worker = entry
            thread.quit()
            thread.wait()
        schedule = self.schedule_store.get(flow_name)
        mark_finished(schedule, status, error=error)
        self.schedule_store.set(schedule)
        self.schedule_store.save()
        self.log(f"[{flow_name}] scheduled run {status}")
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
        errors = self._validation_errors(index, index, force_enabled=True)
        if errors:
            show_error(self, "Step is not ready", "\n".join(errors))
            return
        self._start_replay(index, index, "test", False, False, validate=False)

    def _start_replay(
        self,
        start_index: int,
        end_index: int,
        mode: str,
        include_start_delay: bool,
        respect_enabled: bool,
        validate: bool = True,
        runtime_variables: dict | None = None,
    ) -> None:
        if self.replay_thread is not None or not self.project.actions:
            return
        if not self.ensure_project_dir():
            return
        if validate:
            errors = self._validation_errors(start_index, end_index)
            if errors:
                show_error(self, "Automation is not ready", "\n".join(errors))
                return
        self.file_logger, self.run_log_path = create_file_logger(self.project_dir)
        self.run_start_index = start_index
        self.run_end_index = end_index
        self.run_mode = mode
        self.run_started_at = time.monotonic()
        self._reset_action_statuses()
        self._hide_details_for_run()
        self._hide_for_replay()
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
        )
        self.replay_worker.moveToThread(self.replay_thread)
        self.replay_thread.started.connect(self.replay_worker.run)
        self.replay_worker.action_status.connect(self.set_action_status)
        self.replay_worker.log.connect(self.log)
        self.replay_worker.finished.connect(self.run_completed)
        self.replay_worker.stopped.connect(self.run_stopped)
        self.replay_worker.failed.connect(self.run_failed)
        self.replay_thread.start()
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
        errors: list[str] = []
        seen: set[str] = set()
        for index, action in enumerate(self.project.actions):
            if not action.id:
                errors.append(f"Step {index + 1} {action.friendly_name()}: id is required")
            elif action.id in seen:
                errors.append(f"Step {index + 1} {action.friendly_name()}: id must be unique")
            seen.add(action.id)
        actions = deepcopy(self.project.actions[start_index:end_index + 1])
        if force_enabled:
            for action in actions:
                action.enabled = True
        selected_project = RpaProject(
            project=self.project.project,
            settings=self.project.settings,
            variables=dict(self.project.variables),
            actions=actions,
        )
        for error in validate_project(selected_project, self.project_dir):
            if ": id " in error:
                continue
            parts = error.split(" ", 2)
            if len(parts) >= 3 and parts[0] == "Step" and parts[1].isdigit():
                local_index = int(parts[1]) - 1
                errors.append(f"Step {start_index + local_index + 1} {parts[2]}")
            elif error not in errors:
                errors.append(error)
        return errors

    def stop_run(self) -> None:
        if self.replay_worker:
            self.replay_worker.stop()
            self.log("stop replay requested")

    def run_finished(self) -> None:
        if self.replay_thread:
            self.replay_thread.quit()
            self.replay_thread.wait()
        self.replay_thread = None
        self.replay_worker = None
        self.running_action_index = None
        self._restore_details_after_run()
        self._restore_after_replay()
        self.update_buttons()
        self.update_status()

    def _hide_details_for_run(self) -> None:
        self.details_were_visible_before_run = self.editor_scroll.isVisible()
        self.editor_scroll.setVisible(False)

    def _hide_for_replay(self) -> None:
        if not self.project.settings.hide_window_during_replay:
            return
        self.replay_was_maximized = self.isMaximized()
        self.execution_floating = FloatingExecutionToolbar()
        self.execution_floating.stop_requested.connect(self.stop_run)
        self.execution_floating.show()
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            bounds = screen.availableGeometry()
            self.execution_floating.adjustSize()
            self.execution_floating.move(bounds.left() + (bounds.width() - self.execution_floating.width()) // 2, bounds.bottom() - self.execution_floating.height() - 16)
        self.hide()
        # Prepare a clean desktop before replay begins. The always-on-top stop
        # control remains available, and other windows are intentionally not
        # restored after the run.
        self._show_windows_desktop()

    def _restore_after_replay(self) -> None:
        if self.execution_floating:
            self.execution_floating.close()
            self.execution_floating = None
        if self.project.settings.hide_window_during_replay:
            self.showMaximized() if self.replay_was_maximized else self.showNormal()
            self.raise_()

    def _restore_details_after_run(self) -> None:
        self.editor_scroll.setVisible(self.details_were_visible_before_run)

    def run_completed(self) -> None:
        elapsed = time.monotonic() - self.run_started_at if self.run_started_at else 0.0
        mode = self.run_mode
        start = self.run_start_index
        end = self.run_end_index
        completed = sum(1 for action in self.project.actions[start:end + 1] if action.status == "completed")
        skipped = sum(1 for action in self.project.actions[start:end + 1] if action.status == "skipped")
        self.run_finished()
        if mode == "test":
            self.log(f"step {start + 1} test completed")
            QMessageBox.information(self, "Step Test", f"Step {start + 1} completed successfully in {elapsed:.2f} seconds.")
            return
        self.log("automation completed")
        QMessageBox.information(
            self,
            "Automation Completed",
            f"Automation completed\n\n{completed} completed\n{skipped} skipped\n0 failed\nDuration: {elapsed:.2f} seconds",
        )

    def run_stopped(self) -> None:
        elapsed = time.monotonic() - self.run_started_at if self.run_started_at else 0.0
        last_step = self.running_action_index + 1 if self.running_action_index is not None else self.run_start_index + 1
        self.run_finished()
        self.log("automation stopped by user")
        QMessageBox.information(self, "Automation Stopped", f"Execution stopped at step {last_step}.\nDuration: {elapsed:.2f} seconds")

    def run_failed(self, index: int, message: str) -> None:
        self.log(f"step failed: {message}")
        self.last_runtime_variables = dict(self.replay_worker.runner.runtime_variables) if self.replay_worker else {}
        self.run_finished()
        if index < 0 or index >= len(self.project.actions):
            show_error(self, "Automation stopped", message)
            return
        self.table.selectRow(index)
        self._show_actionable_failure(index, message)

    def _show_actionable_failure(self, index: int, message: str) -> None:
        action = self.project.actions[index]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(f"Step {index + 1} needs attention")
        box.setText(message)
        box.setInformativeText("Review the step, try a recovery option, or stop the automation.")
        test_button = None
        recapture_button = None
        original_button = None
        if action.action in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            test_button = box.addButton("Test Target", QMessageBox.ActionRole)
            recapture_button = box.addButton("Recapture Target", QMessageBox.ActionRole)
            original_button = box.addButton("Use Original Position", QMessageBox.ActionRole)
        skip_button = box.addButton("Skip Step", QMessageBox.DestructiveRole)
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
        elif clicked is skip_button:
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
        action = action if action in self.project.actions else None
        if action is None:
            index = self.table.selected_index()
            action = self.project.actions[index] if index >= 0 else None
        if not action or action.action not in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
            QMessageBox.information(self, "Test Target", "Select a Click step to test its target image.")
            return
        if not self.project_dir:
            show_error(self, "Test Target", "Open or create an automation first.")
            return
        image = self.project_dir / str(action.data.get("image", ""))
        try:
            match = find_image(
                image,
                float(action.data.get("confidence", self.project.settings.default_confidence)),
                self._image_match_exclusions(),
            )
        except Exception as exc:
            show_error(self, "Target Test Failed", str(exc))
            return
        if match.found:
            QMessageBox.information(
                self,
                "Target Found",
                f"Target found at ({match.x}, {match.y}).\nMatch: {match.confidence:.1%}\nSearch time: {match.duration:.2f} seconds",
            )
        else:
            QMessageBox.warning(
                self,
                "Target Not Found",
                f"The target is not currently visible.\nBest match: {match.confidence:.1%}\nSearch time: {match.duration:.2f} seconds\n\nTry showing the target, lowering Match Accuracy, or recapturing it.",
            )

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
            self.update_status("Running")

    def generate_python(self) -> None:
        if not self.ensure_project_dir():
            return
        errors = validate_project(self.project, self.project_dir)
        if errors:
            show_error(self, "Validation failed", "\n".join(errors))
            return
        path = generate_python(self.project, self.project_dir)
        self.log(f"Python file generated: {path}")

    def add_manual_action(self, position: str | None = None) -> None:
        dialog = ManualActionDialog(self.project.settings, self.project.variables, self)
        dialog.screen_pick_requested.connect(lambda role: self._begin_manual_target_capture(dialog, role))
        if dialog.exec() == QDialog.Accepted:
            action = dialog.action()
            self._materialize_manual_image(action)
            if action.action == ActionType.PYTHON_CODE.value and not self.confirm_python_code_warning():
                return
            self.insert_action(action, position)

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
        if self.manual_capture_dialog is not None:
            return
        self.manual_capture_dialog = dialog
        self.manual_capture_role = role
        self.target_capture_was_maximized = self.isMaximized()
        dialog.hide()
        self.hide()
        QTimer.singleShot(200, self._start_manual_target_capture)

    def _start_manual_target_capture(self) -> None:
        dialog = self.manual_capture_dialog
        if dialog is None:
            return
        try:
            captured = screenshot_image()
            self.target_capture_origin = virtual_screen_origin()
            self.target_capture_overlay = TargetCaptureOverlay(captured, self.project.settings.crop_width, self.project.settings.crop_height)
            self.target_capture_overlay.confirmed.connect(self._complete_manual_target_capture)
            self.target_capture_overlay.canceled.connect(self._cancel_manual_target_capture)
            self.target_capture_overlay.show()
        except Exception as exc:
            self._cancel_manual_target_capture()
            show_error(self, "Pick on Screen Failed", str(exc))

    def _complete_manual_target_capture(self, x: int, y: int, width: int, height: int) -> None:
        dialog, overlay = self.manual_capture_dialog, self.target_capture_overlay
        if not dialog:
            return
        image = None
        if overlay and getattr(dialog, "capture_image", None) and dialog.capture_image.isChecked() and self.project_dir:
            image = (Path("screenshots") / f"manual_target_{int(time.time() * 1000)}.png").as_posix()
            try:
                offset_x, offset_y, _width, _height = save_crop_from_image(self.project_dir / image, overlay.captured_image, x, y, width, height, *self.target_capture_origin)
                dialog.set_screen_point(self.manual_capture_role, x, y, image, (offset_x, offset_y))
            except Exception as exc:
                self.log(f"Target image was not saved: {exc}")
                dialog.set_screen_point(self.manual_capture_role, x, y)
        else:
            dialog.set_screen_point(self.manual_capture_role, x, y)
        self._restore_manual_capture_dialog()

    def _cancel_manual_target_capture(self) -> None:
        self._restore_manual_capture_dialog()

    def _restore_manual_capture_dialog(self) -> None:
        overlay, dialog = self.target_capture_overlay, self.manual_capture_dialog
        self.target_capture_overlay = None
        self.manual_capture_dialog = None
        if overlay:
            overlay.deleteLater()
        if self.target_capture_was_maximized:
            self.showMaximized()
        else:
            self.showNormal()
        if dialog:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

    def insert_action(self, action: RpaAction, position: str | None = None) -> None:
        index = self.table.selected_index()
        if index < 0:
            insert_at = len(self.project.actions)
        elif position == "before":
            insert_at = index
        else:
            insert_at = index + 1
        self.project.actions.insert(insert_at, action)
        self.mark_dirty()
        # A filtered list can otherwise make a successfully added step appear
        # to vanish. Show the new work immediately and select it for review.
        if self.filter_box.text():
            self.filter_box.clear()
        self.refresh()
        self.table.selectRow(insert_at)
        self.table.scrollToItem(self.table.item(insert_at, 0))
        self.log(f"step {insert_at + 1} added: {action.summary()}")

    def delete_action(self) -> None:
        indices = self.table.selected_indices()
        if indices:
            index = indices[0]
            for selected in reversed(indices):
                del self.project.actions[selected]
            self.mark_dirty()
            self.refresh()
            if self.project.actions:
                self.table.selectRow(min(index, len(self.project.actions) - 1))
            self.log(f"deleted {len(indices)} step{'s' if len(indices) != 1 else ''}")

    def duplicate_action(self) -> None:
        index = self.table.selected_index()
        if index < 0:
            return
        clone = RpaAction.from_dict(deepcopy(self.project.actions[index].to_dict()))
        clone.id = ""
        clone = RpaAction(clone.action, deepcopy(clone.data), name=clone.name, enabled=clone.enabled, delay_before=clone.delay_before, recorded_delay=clone.recorded_delay)
        self.project.actions.insert(index + 1, clone)
        self.mark_dirty()
        self.refresh()
        self.table.selectRow(index + 1)

    def move_action(self, delta: int) -> None:
        index = self.table.selected_index()
        target = index + delta
        if index < 0 or target < 0 or target >= len(self.project.actions):
            return
        self.project.actions[index], self.project.actions[target] = self.project.actions[target], self.project.actions[index]
        self.mark_dirty()
        self.refresh()
        self.table.selectRow(target)

    def toggle_action(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self.project.actions):
            self.project.actions[index].enabled = enabled
            self.mark_dirty()
            self.table.update_action(index, self.project.actions[index])
            self.update_buttons()

    def toggle_selected_action(self) -> None:
        indices = self.table.selected_indices()
        if not indices:
            return
        enable = not all(self.project.actions[index].enabled for index in indices)
        for index in indices:
            self.project.actions[index].enabled = enable
            self.table.update_action(index, self.project.actions[index])
        self.mark_dirty()
        self.editor.set_action(self.project.actions[indices[0]], self.project_dir)
        self.log(f"{'enabled' if enable else 'disabled'} {len(indices)} step{'s' if len(indices) != 1 else ''}")

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

    def clear_step_selection(self) -> None:
        self.table.clearSelection()
        self.editor.set_action(None, self.project_dir)
        self.table.setFocus(Qt.OtherFocusReason)
        self.update_buttons()
        self.update_status("Selection cleared")

    def close_details(self) -> None:
        self.clear_step_selection()

    def variables_dialog(self) -> None:
        dialog = VariablesDialog(self.project.variables, self)
        if dialog.exec() == QDialog.Accepted:
            self.project.variables = dialog.variables
            self.mark_dirty()

    def settings_dialog(self) -> None:
        dialog = SettingsDialog(self.project.settings, self)
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
            self.dirty = False
            self._reset_history()
            self.log(f"opened project: {path}")
            self.refresh()
        except Exception as exc:
            show_error(self, "Open failed", str(exc))

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
            ("Alt+Up", lambda: self.move_action(-1)),
            ("Alt+Down", lambda: self.move_action(1)),
            ("Ctrl+G", self.generate_python),
            ("F5", self.run_project),
            ("Shift+F5", self.stop_run),
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
            "test": self.test_selected_step,
            "run_from": self.run_from_here,
            "run_until": self.run_until_here,
            "toggle_enabled": self.toggle_selected_action,
            "add": self.add_manual_action,
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
        visible = self.logs.isVisible()
        self.logs.setVisible(not visible)
        self.toggle_logs_btn.setText("Expand Logs" if visible else "Collapse Logs")
        self.settings.setValue("logs_expanded", not visible)

    def open_run_log(self) -> None:
        if self.run_log_path and self.run_log_path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.run_log_path)))
            self.log(f"Run log: {self.run_log_path}")
        else:
            self.log("No run log has been created yet")

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
