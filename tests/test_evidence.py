from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import rpa.runner as runner_module
from rpa.evidence import RunEvidenceSession, load_run_summary
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.runner import ReplayActionError, ReplayRunner

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_evidence_summary_contains_run_validation_and_step_results(tmp_path: Path) -> None:
    session = RunEvidenceSession(tmp_path, "Invoices", "Manual", retention_runs=10)
    issue = SimpleNamespace(level="Warning", step_number=2, step_name="Open file", reason="Review this path")
    session.set_validation([issue])
    session.logger.info("execution message")
    summary = session.finalize(
        "Success",
        [{"step_number": 1, "step_name": "Wait", "status": "Success", "duration_seconds": 0.01}],
        attempts=1,
    )

    saved = json.loads(session.summary_path.read_text(encoding="utf-8"))
    assert saved == summary
    assert saved["flow_name"] == "Invoices"
    assert saved["source"] == "Manual"
    assert saved["status"] == "Success"
    assert saved["validation_results"][0]["step_number"] == 2
    assert saved["step_results"][0]["status"] == "Success"
    assert "execution message" in session.log_path.read_text(encoding="utf-8")


def test_evidence_retention_removes_only_old_run_folders(tmp_path: Path) -> None:
    unrelated = tmp_path / "runs" / "keep.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("keep", encoding="utf-8")
    folders = []
    for _ in range(3):
        session = RunEvidenceSession(tmp_path, "Flow", "Test Step", retention_runs=2)
        folders.append(session.folder)
        session.finalize("Success")
    retained = [path for path in (tmp_path / "runs").iterdir() if path.is_dir()]
    assert len(retained) == 2
    assert folders[2].exists()
    assert unrelated.exists()


def test_missing_or_corrupt_evidence_is_reported_without_exception(tmp_path: Path) -> None:
    summary, error = load_run_summary(tmp_path / "deleted")
    assert summary is None and "deleted or moved" in error
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "summary.json").write_text("not json", encoding="utf-8")
    summary, error = load_run_summary(folder)
    assert summary is None and "could not be read" in error


def test_runner_records_attempts_and_evidence_screenshots(tmp_path: Path, monkeypatch) -> None:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    monkeypatch.setattr(runner_module, "screenshot_image", lambda: Image.new("RGB", (8, 8), "blue"))
    evidence_dir = tmp_path / "runs" / "one"
    evidence_dir.mkdir(parents=True)
    action = RpaAction(ActionType.PYTHON_CODE.value, {
        "code": "variables['attempt'] = variables.get('attempt', 0) + 1\nif variables['attempt'] < 2: raise RuntimeError('again')",
        "retry_count": 1,
        "retry_delay": 0,
        "capture_before": True,
        "capture_after": True,
    })
    runner = ReplayRunner(RpaProject(actions=[action]), tmp_path, lambda _message: None, evidence_dir=evidence_dir)
    runner.run(include_start_delay=False)
    result = runner.step_results[0]
    assert result["status"] == "Success"
    assert result["attempts"] == 2
    assert result["retry_attempts"][0]["attempt"] == 2
    assert (evidence_dir / result["screenshots"]["before"]).is_file()
    assert (evidence_dir / result["screenshots"]["after"]).is_file()


def test_final_failure_always_captures_screenshot_in_evidence_folder(tmp_path: Path, monkeypatch) -> None:
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)
    monkeypatch.setattr(runner_module, "screenshot_image", lambda: Image.new("RGB", (8, 8), "red"))
    evidence_dir = tmp_path / "runs" / "failed"
    evidence_dir.mkdir(parents=True)
    action = RpaAction(ActionType.PYTHON_CODE.value, {"code": "raise RuntimeError('boom')"})
    runner = ReplayRunner(RpaProject(actions=[action]), tmp_path, lambda _message: None, evidence_dir=evidence_dir)
    with pytest.raises(ReplayActionError):
        runner.run(include_start_delay=False)
    result = runner.step_results[0]
    assert result["status"] == "Failed"
    assert "boom" in result["error"]
    assert (evidence_dir / result["screenshots"]["failure"]).is_file()


def test_run_details_view_handles_present_and_deleted_evidence(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication
    from ui.run_details_dialog import RunDetailsDialog

    app = QApplication.instance() or QApplication([])
    session = RunEvidenceSession(tmp_path, "Flow", "Scheduled")
    session.finalize("COMPLETED_VERIFIED", [{
        "step_number": 1, "step_name": "Wait", "status": "Success",
        "duration_seconds": 0.1, "attempts": 1,
    }], attempts=1, diagnostics={
        "retry_count": 1,
        "fallback_executed": True,
        "completion_criteria_result": {"passed": True},
    })
    dialog = RunDetailsDialog(session.folder)
    assert dialog.steps_table.rowCount() == 1
    assert dialog.diagnostics_table.item(0, 1).text() == "1"
    assert dialog.diagnostics_table.item(1, 1).text() == "Yes"
    assert dialog.diagnostics_table.item(3, 1).text() == "Passed"
    assert dialog.open_folder_btn.isEnabled()
    assert dialog.open_log_btn.isEnabled()
    dialog.close()

    missing = RunDetailsDialog(tmp_path / "deleted")
    assert "deleted or moved" in missing.summary_label.text()
    assert not missing.open_folder_btn.isEnabled()
    missing.close()
    app.processEvents()
