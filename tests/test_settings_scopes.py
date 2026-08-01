from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from rpa.models import ActionType, ProjectSettings, RpaProject
from rpa.project_manager import ProjectManager
from ui.dialogs import SettingsDialog, load_system_settings, save_system_settings


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settings_store(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "system.ini"), QSettings.IniFormat)


def test_system_settings_persist(tmp_path) -> None:
    app()
    store = settings_store(tmp_path)
    dialog = SettingsDialog(
        load_system_settings(store), scope="system", system_store=store,
    )
    dialog.timeout.setValue(60)
    dialog.run_log_url.setText("https://example.test/run-log")
    dialog.run_log_timeout.setValue(45)
    dialog.action_type_checks[ActionType.TYPE_TEXT.value].setChecked(False)
    dialog.accept()

    reloaded = load_system_settings(settings_store(tmp_path))
    assert reloaded.default_timeout == 60
    assert reloaded.run_log_webhook_url == "https://example.test/run-log"
    assert reloaded.run_log_timeout_seconds == 45
    assert ActionType.TYPE_TEXT.value in reloaded.disabled_action_types


def test_new_flow_receives_independent_copy_of_system_settings(tmp_path) -> None:
    store = settings_store(tmp_path)
    system = load_system_settings(store)
    system.default_timeout = 60
    system.run_log_webhook_url = "https://example.test/default"
    system.run_log_timeout_seconds = 30
    save_system_settings(system, store)

    flow = ProjectManager().new_project("Flow A", load_system_settings(store))
    assert flow.settings.default_timeout == 60
    assert flow.settings.send_run_log_to_sharepoint is False
    assert flow.settings.run_log_webhook_url == "https://example.test/default"
    assert flow.settings.run_log_timeout_seconds == 30
    flow.settings.default_timeout = 120
    assert load_system_settings(store).default_timeout == 60


def test_existing_flow_is_unchanged_when_system_settings_change(tmp_path) -> None:
    store = settings_store(tmp_path)
    system = load_system_settings(store)
    system.default_timeout = 60
    system.run_log_webhook_url = "https://example.test/first"
    save_system_settings(system, store)
    flow = ProjectManager().new_project("Flow A", load_system_settings(store))

    system.default_timeout = 30
    system.run_log_webhook_url = "https://example.test/second"
    save_system_settings(system, store)
    assert flow.settings.default_timeout == 60
    assert flow.settings.run_log_webhook_url == "https://example.test/first"


def test_flow_settings_save_does_not_change_system_settings(tmp_path) -> None:
    app()
    store = settings_store(tmp_path)
    system = load_system_settings(store)
    system.default_timeout = 60
    save_system_settings(system, store)
    flow = ProjectManager().new_project("Flow A", system)

    dialog = SettingsDialog(
        flow.settings, project=flow, scope="flow", system_store=store,
    )
    dialog.timeout.setValue(120)
    dialog.accept()

    assert flow.settings.default_timeout == 120
    assert load_system_settings(store).default_timeout == 60


def test_reset_flow_settings_to_current_system_defaults(tmp_path, monkeypatch) -> None:
    app()
    store = settings_store(tmp_path)
    system = load_system_settings(store)
    system.default_timeout = 30
    system.run_log_webhook_url = "https://example.test/current-default"
    system.run_log_timeout_seconds = 35
    save_system_settings(system, store)
    flow = RpaProject(settings=ProjectSettings(
        default_timeout=120,
        send_run_log_to_sharepoint=True,
        run_log_webhook_url="https://example.test/old-flow",
        run_log_timeout_seconds=80,
    ))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)

    dialog = SettingsDialog(
        flow.settings, project=flow, scope="flow", system_store=store,
    )
    dialog._reset_to_system_defaults()
    dialog.accept()

    expected = load_system_settings(store)
    for key, value in expected.__dict__.items():
        if key != "disabled_action_types":
            assert getattr(flow.settings, key) == value
    assert set(flow.settings.disabled_action_types) == set(expected.disabled_action_types)


def test_legacy_flow_missing_settings_uses_safe_defaults_without_rewrite(tmp_path) -> None:
    project_path = tmp_path / "project.json"
    payload = RpaProject().to_dict()
    payload["settings"] = {"default_timeout": 27}
    original = json.dumps(payload, indent=2)
    project_path.write_text(original, encoding="utf-8")

    loaded = ProjectManager().load(project_path)

    assert loaded.settings.default_timeout == 27
    assert loaded.settings.crop_width == ProjectSettings().crop_width
    assert loaded.settings.disabled_action_types == []
    assert loaded.settings.send_run_log_to_sharepoint is False
    assert loaded.settings.run_log_webhook_url == ""
    assert loaded.settings.run_log_timeout_seconds == 30
    assert project_path.read_text(encoding="utf-8") == original
