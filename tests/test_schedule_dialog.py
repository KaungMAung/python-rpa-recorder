from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from rpa.scheduler import STATUS_FAILED, STATUS_SUCCESS, ScheduleStore
from ui.schedule_dialog import (
    COLUMN_FLOW,
    COLUMN_LAST_RUN,
    ScheduleFlowsDialog,
)


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_flow(tmp_path: Path, name: str) -> None:
    flow_dir = tmp_path / name
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "project.json").write_text("{}", encoding="utf-8")


def make_dialog(tmp_path: Path, settings_name: str = "test") -> tuple[ScheduleFlowsDialog, ScheduleStore, QSettings]:
    app()
    store = ScheduleStore(tmp_path)
    settings = QSettings("PythonRPARecorderTests", settings_name)
    settings.clear()
    dialog = ScheduleFlowsDialog(store, settings)
    return dialog, store, settings


def test_dialog_lists_every_flow_with_a_project_json(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    _make_flow(tmp_path, "flow_b")
    dialog, _store, _settings = make_dialog(tmp_path)
    assert dialog.table.rowCount() == 2
    dialog.close()


def test_enabling_a_disabled_schedule_requires_no_confirmation(tmp_path: Path, monkeypatch) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    calls = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: calls.append(1) or QMessageBox.Yes))
    dialog._toggle_enabled("flow_a")
    assert store.get("flow_a").enabled is True
    assert calls == []
    dialog.close()


def test_disabling_an_enabled_schedule_asks_for_confirmation(tmp_path: Path, monkeypatch) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    store.get("flow_a").enabled = True
    store.save()

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    dialog._toggle_enabled("flow_a")
    assert store.get("flow_a").enabled is True  # declined, stays enabled

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dialog._toggle_enabled("flow_a")
    assert store.get("flow_a").enabled is False
    dialog.close()


def test_pause_keeps_enabled_and_interval_configuration(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.enabled = True
    schedule.interval_minutes = 120
    store.set(schedule)
    store.save()

    dialog._toggle_pause("flow_a")
    paused_schedule = store.get("flow_a")
    assert paused_schedule.paused is True
    assert paused_schedule.enabled is True
    assert paused_schedule.interval_minutes == 120

    dialog._toggle_pause("flow_a")
    assert store.get("flow_a").paused is False
    dialog.close()


def test_pause_button_disabled_when_schedule_is_not_enabled(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    dialog._toggle_pause("flow_a")
    assert store.get("flow_a").paused is False
    dialog.close()


def test_sorting_by_flow_name_orders_rows_alphabetically(tmp_path: Path) -> None:
    _make_flow(tmp_path, "zebra")
    _make_flow(tmp_path, "alpha")
    dialog, _store, _settings = make_dialog(tmp_path)
    # Flow name ascending is the default sort order, so no header click needed.
    names = [dialog.table.item(row, COLUMN_FLOW).text() for row in range(dialog.table.rowCount())]
    assert names == sorted(names)
    dialog.close()


def test_sorting_twice_on_same_column_reverses_order(tmp_path: Path) -> None:
    _make_flow(tmp_path, "zebra")
    _make_flow(tmp_path, "alpha")
    dialog, _store, _settings = make_dialog(tmp_path)
    dialog._on_header_clicked(COLUMN_FLOW)
    ascending = [dialog.table.item(row, COLUMN_FLOW).text() for row in range(dialog.table.rowCount())]
    dialog._on_header_clicked(COLUMN_FLOW)
    descending = [dialog.table.item(row, COLUMN_FLOW).text() for row in range(dialog.table.rowCount())]
    assert descending == list(reversed(ascending))
    dialog.close()


def test_sort_column_and_order_persist_across_dialog_instances(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, _store, settings = make_dialog(tmp_path, settings_name="persist_sort")
    dialog._on_header_clicked(COLUMN_LAST_RUN)
    dialog.close()

    dialog2 = ScheduleFlowsDialog(ScheduleStore(tmp_path), settings)
    assert dialog2._sort_column == COLUMN_LAST_RUN
    dialog2.close()


def test_column_widths_persist_across_dialog_instances(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, settings = make_dialog(tmp_path, settings_name="persist_width")
    dialog.table.setColumnWidth(2, 250)
    dialog.accept()

    dialog2 = ScheduleFlowsDialog(store, settings)
    assert dialog2.table.columnWidth(2) == 250
    dialog2.close()


def test_last_status_tooltip_shows_failure_reason(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.last_status = STATUS_FAILED
    schedule.last_error = "image not found"
    store.set(schedule)
    store.save()
    dialog.reload()
    item = dialog.table.item(0, 5)
    assert "image not found" in item.toolTip()
    dialog.close()


def test_duration_is_formatted_for_display(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.last_status = STATUS_SUCCESS
    schedule.last_duration_seconds = 125.4
    store.set(schedule)
    store.save()
    dialog.reload()
    item = dialog.table.item(0, 4)
    assert item.text() == "2m 05s"
    dialog.close()


def test_summary_counts_schedule_states_and_running_result(tmp_path: Path) -> None:
    for name in ("enabled", "paused", "disabled", "running"):
        _make_flow(tmp_path, name)
    dialog, store, _settings = make_dialog(tmp_path)
    enabled = store.get("enabled")
    enabled.enabled = True
    paused = store.get("paused")
    paused.enabled = True
    paused.paused = True
    running = store.get("running")
    running.enabled = True
    running.last_status = "Running"
    for schedule in (enabled, paused, running):
        store.set(schedule)
    store.save()

    dialog.reload()
    assert dialog.summary_labels["Enabled"].text() == "Enabled  2"
    assert dialog.summary_labels["Paused"].text() == "Paused  1"
    assert dialog.summary_labels["Disabled"].text() == "Disabled  1"
    assert dialog.summary_labels["Running"].text() == "Running  1"
    dialog.close()


def test_search_and_status_filters_narrow_the_table(tmp_path: Path) -> None:
    _make_flow(tmp_path, "invoice_export")
    _make_flow(tmp_path, "daily_backup")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("invoice_export")
    schedule.enabled = True
    store.set(schedule)
    store.save()

    dialog.search_box.setText("invoice")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, COLUMN_FLOW).text() == "invoice_export"
    dialog.search_box.clear()
    dialog.status_filter.setCurrentText("Disabled")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, COLUMN_FLOW).text() == "daily_backup"
    dialog.close()


def test_empty_state_is_shown_when_filter_has_no_matches(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, _store, _settings = make_dialog(tmp_path)
    dialog.search_box.setText("missing")
    assert dialog.table.rowCount() == 0
    assert not dialog.empty_label.isHidden()
    assert dialog.table.isHidden()
    dialog.close()


def test_selecting_row_populates_editable_details_panel(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.enabled = True
    schedule.interval_minutes = 120
    schedule.last_error = "sample error"
    store.set(schedule)
    store.save()

    dialog.reload()
    dialog.table.setCurrentCell(0, COLUMN_FLOW)
    assert dialog.detail_name.text() == "flow_a"
    assert dialog.detail_state.text() == "Enabled"
    assert dialog.detail_interval.currentData() == 120
    assert dialog.detail_values["error"].text() == "sample error"
    dialog.close()


def test_times_use_friendly_relative_text(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.enabled = True
    schedule.last_run_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    schedule.next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=12)).isoformat()
    store.set(schedule)
    store.save()

    dialog.reload()
    assert dialog.table.item(0, 3).text() in {"1 min ago", "2 min ago"}
    assert dialog.table.item(0, 6).text() in {"in 11 min", "in 12 min"}
    dialog.close()


def test_five_minute_interval_is_available_and_persists(tmp_path: Path) -> None:
    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    five_minute_index = dialog.detail_interval.findData(5)
    assert five_minute_index >= 0
    dialog.detail_interval.setCurrentIndex(five_minute_index)
    assert store.get("flow_a").interval_minutes == 5
    dialog.close()


def test_history_panel_lists_and_filters_persisted_results(tmp_path: Path) -> None:
    from rpa.scheduler import RunHistoryEntry

    _make_flow(tmp_path, "flow_a")
    dialog, store, _settings = make_dialog(tmp_path)
    schedule = store.get("flow_a")
    schedule.history = [
        RunHistoryEntry("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:02+00:00", 2, "Success"),
        RunHistoryEntry("2026-01-02T00:00:00+00:00", "2026-01-02T00:00:03+00:00", 3, "Failed", 2, "boom"),
        RunHistoryEntry("2026-01-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00", 0, "Skipped (Already Running)"),
    ]
    schedule.last_status = "Failed"
    schedule.last_error = "boom"
    store.set(schedule)
    store.save()

    dialog.reload()
    assert dialog.history_table.rowCount() == 3
    assert dialog.table.item(0, 5).text() == "Failed"
    dialog.history_filter.setCurrentText("Failed")
    assert dialog.history_table.rowCount() == 1
    assert dialog.history_table.item(0, 4).text() == "Step 2"
    assert dialog.history_table.item(0, 5).text() == "boom"
    dialog.history_filter.setCurrentText("Skipped")
    assert dialog.history_table.rowCount() == 1
    dialog.close()


def test_history_limit_setting_trims_and_persists(tmp_path: Path) -> None:
    from rpa.scheduler import RunHistoryEntry

    _make_flow(tmp_path, "flow_a")
    dialog, store, settings = make_dialog(tmp_path, settings_name="history_limit")
    schedule = store.get("flow_a")
    schedule.history = [RunHistoryEntry(f"2026-01-{day:02d}T00:00:00+00:00", status="Success") for day in range(1, 13)]
    store.set(schedule)
    dialog.history_limit_spin.setValue(10)
    assert len(store.get("flow_a").history) == 10
    assert settings.value("scheduler/history_limit", type=int) == 10
    dialog.close()
