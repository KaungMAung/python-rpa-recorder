from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.scheduler import (
    FlowSchedule, ScheduleStore, TASK_DISABLED, TASK_MISSING, TASK_REGISTERED,
    TASK_REGISTRATION_FAILED, TASK_RUNNING,
)
from rpa.windows_tasks import (
    TaskOperationResult, WindowsTaskRegistrar, sanitize_task_component,
    reconcile_schedules, standalone_runner_command, task_name, task_xml,
)


def _project(tmp_path: Path, flow_name: str = "Demo Flow") -> Path:
    flow_dir = tmp_path / flow_name
    ProjectManager().save(
        RpaProject(actions=[RpaAction(ActionType.PYTHON_CODE.value, {
            "code": "variables['scheduled_ok'] = True",
        })]),
        flow_dir,
    )
    return flow_dir / "project.json"


def test_multiple_schedules_persist_with_stable_ids_and_legacy_api(tmp_path: Path) -> None:
    project_json = _project(tmp_path, "demo")
    store = ScheduleStore(tmp_path)
    primary = store.get("demo")
    second = store.create_schedule("demo")
    second.interval_minutes = 15
    store.set(second)
    store.save()

    restored = ScheduleStore(tmp_path)
    schedules = restored.list_schedules()
    assert len(schedules) == 2
    assert restored.get("demo").schedule_id == primary.schedule_id
    assert restored.get("demo").flow_id
    assert restored.get("demo").flow_id == restored.get_by_id(second.schedule_id).flow_id
    assert restored.get_by_id(second.schedule_id).interval_minutes == 15
    assert project_json.exists()


def test_task_name_command_and_xml_match_windows_requirements(tmp_path: Path) -> None:
    project_json = _project(tmp_path, "invoice_export")
    schedule = FlowSchedule(
        "Invoice: Export / Daily", flow_id="flow-id-42", schedule_id="abc123", enabled=True,
        interval_minutes=5, execution_timeout_minutes=45,
        run_with_highest_privileges=True,
    )
    command = standalone_runner_command(project_json, schedule.schedule_id)
    xml = task_xml(schedule, command)

    assert task_name(schedule) == r"\PythonRPARecorder\Flow_flow-id-42_abc123"
    renamed = FlowSchedule("Renamed Flow", flow_id="flow-id-42", schedule_id="abc123")
    assert task_name(renamed) == task_name(schedule)
    assert command[-5:] == ["--project", str(project_json.resolve()), "--schedule-id", "abc123", "--scheduled-run"]
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<ExecutionTimeLimit>PT45M30S</ExecutionTimeLimit>" in xml
    assert "<RunLevel>HighestAvailable</RunLevel>" in xml
    assert "PT5M" in xml


def test_registrar_creates_then_disables_task_without_password(tmp_path: Path, monkeypatch) -> None:
    project_json = _project(tmp_path)
    schedule = FlowSchedule("Demo Flow", schedule_id="sched1", enabled=False)
    calls: list[list[str]] = []
    xml_text: list[str] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if "/XML" in command:
            xml_text.append(Path(command[command.index("/XML") + 1]).read_text(encoding="utf-16"))
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    import rpa.windows_tasks as task_module
    monkeypatch.setattr(task_module.os, "name", "nt")
    registrar = WindowsTaskRegistrar(command_runner=fake_run, allow_elevation=False)
    result = registrar.sync(schedule, project_json)

    assert result.ok and result.status == TASK_DISABLED
    assert calls[0][0] == "powershell.exe"
    assert calls[1][1:3] == ["/Create", "/TN"]
    assert any(call[-1] == "/DISABLE" for call in calls)
    assert sum("/Create" in call for call in calls) == 1
    assert any("/Delete" in call and "RPA Recorder" in " ".join(call) for call in calls)
    assert "Password" not in xml_text[0]
    assert "InteractiveToken" in xml_text[0]


def test_test_run_launches_exact_standalone_command(tmp_path: Path) -> None:
    project_json = _project(tmp_path)
    schedule = FlowSchedule("Demo Flow", schedule_id="run123")
    launched = []

    registrar = WindowsTaskRegistrar(
        popen=lambda command, **kwargs: launched.append((command, kwargs)),
        allow_elevation=False,
    )
    result = registrar.test_run(schedule, project_json)
    assert result.ok
    assert launched[0][0] == standalone_runner_command(project_json, "run123")
    assert launched[0][0][-5:] == [
        "--project", str(project_json.resolve()), "--schedule-id", "run123", "--scheduled-run",
    ]


def test_registration_rejects_invalid_project_path_with_clear_status(tmp_path: Path) -> None:
    schedule = FlowSchedule("Missing Flow", schedule_id="missing1", enabled=True)
    registrar = WindowsTaskRegistrar(allow_elevation=False)

    result = registrar.sync(schedule, (tmp_path / "missing" / "project.json").resolve())

    assert not result.ok
    assert result.status == TASK_REGISTRATION_FAILED
    assert "does not exist" in result.error


def test_access_denied_uses_only_elevated_registration_helper(tmp_path: Path, monkeypatch) -> None:
    project_json = _project(tmp_path)
    schedule = FlowSchedule("Demo Flow", schedule_id="uac1", enabled=True)
    helper_calls = []

    class TrackingRegistrar(WindowsTaskRegistrar):
        def _run_elevated_helper(self, operation, selected_schedule, selected_project):
            helper_calls.append((operation, selected_schedule.schedule_id, selected_project))
            return TaskOperationResult(True, TASK_REGISTERED, task_name(selected_schedule))

    import rpa.windows_tasks as task_module
    monkeypatch.setattr(task_module.os, "name", "nt")
    registrar = TrackingRegistrar(command_runner=lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="ERROR: Access is denied.",
    ))

    result = registrar.sync(schedule, project_json)

    assert result.ok
    assert helper_calls == [("sync", "uac1", project_json)]


def test_query_reports_deleted_windows_task_as_missing(tmp_path: Path, monkeypatch) -> None:
    schedule = FlowSchedule("Demo Flow", schedule_id="gone1", enabled=True)
    import rpa.windows_tasks as task_module
    monkeypatch.setattr(task_module.os, "name", "nt")
    registrar = WindowsTaskRegistrar(
        command_runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="ERROR: The system cannot find the file specified.",
        ),
        allow_elevation=False,
    )

    result = registrar.query(schedule)

    assert result.ok
    assert result.status == TASK_MISSING


def test_query_running_status_uses_only_explicit_state_field(monkeypatch) -> None:
    schedule = FlowSchedule("Demo Flow", flow_id="flow1", schedule_id="state1", enabled=True)
    import rpa.windows_tasks as task_module
    monkeypatch.setattr(task_module.os, "name", "nt")
    outputs = iter([
        "Status: Ready\nStop the task if it runs longer than: 1 hour",
        "Status: Running\nStop the task if it runs longer than: 1 hour",
    ])
    registrar = WindowsTaskRegistrar(command_runner=lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=next(outputs), stderr="",
    ), allow_elevation=False)
    assert registrar.query(schedule).status == TASK_REGISTERED
    assert registrar.query(schedule).status == TASK_RUNNING


def test_startup_reconciliation_registers_enabled_and_disables_saved_task(tmp_path: Path) -> None:
    _project(tmp_path, "enabled_flow")
    _project(tmp_path, "disabled_flow")
    store = ScheduleStore(tmp_path)
    enabled = store.get("enabled_flow"); enabled.enabled = True
    disabled = store.get("disabled_flow"); disabled.enabled = False
    store.set(enabled); store.set(disabled); store.save()

    class FakeRegistrar:
        def __init__(self):
            self.synced = []; self.disabled = []; self.cleaned = []

        def sync(self, schedule, _project_json):
            self.synced.append(schedule.schedule_id)
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule))

        def query(self, schedule):
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule))

        def disable(self, schedule):
            self.disabled.append(schedule.schedule_id)
            return TaskOperationResult(True, TASK_DISABLED, task_name(schedule))

        def cleanup_old_task_names(self, schedule):
            self.cleaned.append(schedule.schedule_id)

    registrar = FakeRegistrar()
    results = reconcile_schedules(store, registrar)
    restored = ScheduleStore(tmp_path)
    assert registrar.synced == [enabled.schedule_id]
    assert registrar.disabled == [disabled.schedule_id]
    assert len(results) == 2
    assert restored.get_by_id(enabled.schedule_id).windows_task_name == task_name(enabled)
    assert restored.get_by_id(disabled.schedule_id).task_status == TASK_DISABLED


def test_scheduled_controller_updates_existing_history_and_evidence(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QApplication
    import rpa.runner as runner_module
    from rpa.scheduled_runner import ScheduledRunController

    app = QApplication.instance() or QApplication([])
    project_json = _project(tmp_path, "scheduled_flow")
    store = ScheduleStore(tmp_path)
    schedule = store.get("scheduled_flow")
    schedule.enabled = True
    store.set(schedule)
    store.save()
    runner_module.pyautogui = SimpleNamespace(FAILSAFE=True)

    controller = ScheduledRunController(app, project_json, schedule.schedule_id)
    monkeypatch.setattr(controller.app, "exit", lambda _code=0: None)
    assert controller._prepare() is None
    controller.runner.run(include_start_delay=False)
    controller._finish({
        "status": "Success", "error": None, "failed_step": None,
        "attempts": controller.runner.total_attempts,
        "steps": controller.runner.step_results,
    })

    restored = ScheduleStore(tmp_path).get_by_id(schedule.schedule_id)
    assert restored.history[-1].status == "Success"
    assert restored.history[-1].evidence_path
    evidence = tmp_path / "scheduled_flow" / restored.history[-1].evidence_path
    assert (evidence / "summary.json").exists()


def test_scheduled_desktop_lifecycle_restores_only_captured_recorder_windows(
    tmp_path: Path, monkeypatch,
) -> None:
    from PySide6.QtWidgets import QApplication
    import rpa.scheduled_runner as scheduled_module

    app = QApplication.instance() or QApplication([])
    project_json = _project(tmp_path, "desktop_flow")
    store = ScheduleStore(tmp_path)
    schedule = store.get("desktop_flow")
    store.set(schedule)
    store.save()
    controller = scheduled_module.ScheduledRunController(app, project_json, schedule.schedule_id)
    messages = []
    restored = []
    monkeypatch.setattr(scheduled_module, "recorder_window_handles", lambda: [101, 202])
    monkeypatch.setattr(scheduled_module, "show_windows_desktop", lambda: 6)
    monkeypatch.setattr(
        scheduled_module, "restore_recorder_windows",
        lambda handles: restored.extend(handles) or len(handles),
    )
    monkeypatch.setattr(controller, "_log", messages.append)

    controller._prepare_desktop()
    controller._restore_desktop()

    assert restored == [101, 202]
    assert controller._recorder_windows == []
    assert "minimized 6 window(s)" in messages[1]
    assert "restored 2 recorder window(s)" in messages[2]


def test_sanitized_task_component_removes_scheduler_reserved_characters() -> None:
    assert sanitize_task_component(' A\\B:C*D?E"F<G>H| ') == "A_B_C_D_E_F_G_H_"


def test_schedule_dialog_syncs_add_edit_disable_delete_and_test_run(tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.schedule_dialog import ScheduleFlowsDialog

    app = QApplication.instance() or QApplication([])
    _project(tmp_path, "dialog_flow")
    store = ScheduleStore(tmp_path)
    primary = store.get("dialog_flow")
    store.save()

    class FakeRegistrar:
        def __init__(self):
            self.synced: list[str] = []
            self.deleted: list[str] = []
            self.tested: list[str] = []

        def query(self, schedule):
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule))

        def sync(self, schedule, project_json):
            self.synced.append(schedule.schedule_id)
            return TaskOperationResult(
                True, TASK_REGISTERED if schedule.enabled else TASK_DISABLED,
                task_name(schedule), command=standalone_runner_command(project_json, schedule.schedule_id),
            )

        def delete(self, schedule):
            self.deleted.append(schedule.schedule_id)
            return TaskOperationResult(True, "Task missing", task_name(schedule))

        def test_run(self, schedule, project_json):
            self.tested.append(schedule.schedule_id)
            command = standalone_runner_command(project_json, schedule.schedule_id)
            return TaskOperationResult(True, schedule.task_status, task_name(schedule), command=command)

    fake = FakeRegistrar()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok)
    dialog = ScheduleFlowsDialog(
        store, QSettings("tests", "windows_tasks_dialog"), task_registrar=fake,
    )
    dialog._confirm_action = lambda *args, **kwargs: True
    dialog._toggle_enabled(primary.schedule_id)
    assert primary.schedule_id in fake.synced
    dialog._repair_task(primary.schedule_id)
    assert fake.synced.count(primary.schedule_id) >= 2

    dialog._selected_flow_name = "dialog_flow"
    dialog._add_schedule()
    schedules = store.list_schedules()
    extra = next(item for item in schedules if item.schedule_id != primary.schedule_id)
    assert extra.schedule_id in fake.synced
    dialog._test_schedule(extra.schedule_id)
    assert fake.tested == [extra.schedule_id]
    dialog._delete_schedule(extra.schedule_id)
    assert fake.deleted == [extra.schedule_id]
    assert store.get_by_id(extra.schedule_id) is None
    dialog.close()
    app.processEvents()


def test_legacy_saved_schedule_is_registered_once_but_missing_task_stays_visible(
    tmp_path: Path, monkeypatch,
) -> None:
    import json
    import time
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from ui.schedule_dialog import ScheduleFlowsDialog

    app = QApplication.instance() or QApplication([])
    _project(tmp_path, "legacy_flow")
    (tmp_path / "schedules.json").write_text(json.dumps({
        "legacy_flow": {"enabled": True, "interval_minutes": 5},
    }), encoding="utf-8")

    class FakeRegistrar:
        def __init__(self):
            self.synced = 0

        def sync(self, schedule, _project_json):
            self.synced += 1
            return TaskOperationResult(True, TASK_REGISTERED, task_name(schedule))

        def query(self, schedule):
            return TaskOperationResult(True, TASK_MISSING, task_name(schedule))

    registrar = FakeRegistrar()
    dialog = ScheduleFlowsDialog(
        ScheduleStore(tmp_path), QSettings("tests", "legacy_task_migration"),
        task_registrar=registrar,
    )
    # Registration migration is reconciled by the startup controller. The dialog
    # only performs cached background status reads so opening it cannot block.
    for _ in range(100):
        app.processEvents()
        if registrar.synced or dialog._task_status_cache:
            break
        time.sleep(0.005)
    assert registrar.synced == 0
    assert dialog._task_status_cache
    assert next(iter(dialog._task_status_cache.values())).status == TASK_MISSING
    dialog.close()
    app.processEvents()
