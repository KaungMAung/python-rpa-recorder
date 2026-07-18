"""Human-readable view of a persisted execution evidence folder."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from rpa.evidence import load_run_summary


class RunDetailsDialog(QDialog):
    def __init__(self, evidence_folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.evidence_folder = Path(evidence_folder)
        self.summary, self.load_error = load_run_summary(self.evidence_folder)
        self.setWindowTitle("Run Details")
        self.resize(860, 580)
        layout = QVBoxLayout(self)

        title = QLabel("Execution report")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)
        self.summary_label = QLabel(self._summary_text())
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        buttons = QHBoxLayout()
        self.open_folder_btn = QPushButton("Open Run Folder")
        self.open_log_btn = QPushButton("Open Log")
        self.open_screenshots_btn = QPushButton("Open Screenshots")
        self.open_folder_btn.clicked.connect(lambda: self._open_path(self.evidence_folder))
        self.open_log_btn.clicked.connect(lambda: self._open_path(self.evidence_folder / "execution.log"))
        self.open_screenshots_btn.clicked.connect(lambda: self._open_path(self.evidence_folder / "screenshots"))
        for button in (self.open_folder_btn, self.open_log_btn, self.open_screenshots_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        tabs = QTabWidget()
        self.steps_table = self._table(["Step", "Name", "Status", "Duration", "Attempts", "Branch / Loop Result", "Error"])
        self.validation_table = self._table(["Level", "Step", "Step name", "Reason"])
        self.inputs_table = self._table(["Runtime input", "Value"])
        tabs.addTab(self.steps_table, "Step Results")
        tabs.addTab(self.validation_table, "Validation")
        tabs.addTab(self.inputs_table, "Runtime Inputs")
        layout.addWidget(tabs, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        self._populate()

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.Stretch)
        return table

    def _summary_text(self) -> str:
        if self.load_error:
            return self.load_error
        data = self.summary or {}
        duration = data.get("duration_seconds")
        duration_text = f"{float(duration):.2f}s" if isinstance(duration, (int, float)) else "—"
        failed = data.get("failed_step")
        error = data.get("error") or "None"
        return (
            f"{data.get('flow_name', 'Unknown flow')} · {data.get('source', 'Unknown source')} · "
            f"{data.get('status', 'Unknown')}\nStarted: {data.get('started_at', '—')} · "
            f"Duration: {duration_text} · Attempts: {data.get('attempts', 0)} · "
            f"Failed step: {failed or '—'}\nError: {error}"
        )

    def _populate(self) -> None:
        folder_exists = self.evidence_folder.is_dir()
        log_exists = (self.evidence_folder / "execution.log").is_file()
        screenshots_exists = (self.evidence_folder / "screenshots").is_dir()
        self.open_folder_btn.setEnabled(folder_exists)
        self.open_log_btn.setEnabled(log_exists)
        self.open_screenshots_btn.setEnabled(screenshots_exists)
        if not self.summary:
            return
        steps = self.summary.get("step_results") or []
        self.steps_table.setRowCount(len(steps))
        for row, step in enumerate(steps):
            duration = step.get("duration_seconds")
            values = (
                step.get("step_number", "—"), step.get("step_name", "—"), step.get("status", "—"),
                f"{float(duration):.2f}s" if isinstance(duration, (int, float)) else "—",
                step.get("attempts", 0), self._control_result_text(step.get("control_result")),
                step.get("error") or "—",
            )
            for column, value in enumerate(values):
                self.steps_table.setItem(row, column, QTableWidgetItem(str(value)))
        validation = self.summary.get("validation_results") or []
        self.validation_table.setRowCount(len(validation))
        for row, issue in enumerate(validation):
            values = (issue.get("level", "—"), issue.get("step_number", "—"), issue.get("step_name", "—"), issue.get("reason", "—"))
            for column, value in enumerate(values):
                self.validation_table.setItem(row, column, QTableWidgetItem(str(value)))
        inputs = self.summary.get("runtime_inputs") or {}
        self.inputs_table.setRowCount(len(inputs))
        for row, (name, value) in enumerate(sorted(inputs.items())):
            self.inputs_table.setItem(row, 0, QTableWidgetItem(str(name)))
            self.inputs_table.setItem(row, 1, QTableWidgetItem(str(value)))

    @staticmethod
    def _control_result_text(result) -> str:
        if not result:
            return "—"
        if not isinstance(result, dict):
            return str(result)
        values = []
        for key in ("branch", "evaluated", "iteration", "iterations", "limit", "condition", "selected", "reason"):
            if key in result:
                values.append(f"{key.replace('_', ' ')}: {result[key]}")
        return ", ".join(values) or json.dumps(result, ensure_ascii=False)

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.information(self, "Evidence unavailable", "This evidence file has been deleted or moved.")
            self._populate()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
