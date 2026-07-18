from __future__ import annotations

import os
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QSettings, QTimer, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPlainTextEdit

from rpa.models import ActionType, ProjectSettings, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from ui.dialogs import ManualActionDialog
from ui.main_window import MainWindow
from ui.main_window import sanitize_flow_name
from ui.target_capture import TargetCaptureOverlay


def app() -> QApplication:
    existing = QApplication.instance()
    return existing or QApplication([])


def window_with_actions() -> MainWindow:
    app()
    window = MainWindow()
    window.project.actions = [
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "first", "interval": 0.01}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "second", "interval": 0.01}),
    ]
    window.refresh()
    window.show()
    app().processEvents()
    return window


def test_select_step_keeps_table_visible_and_shows_properties() -> None:
    window = window_with_actions()
    window.table.selectRow(0)
    app().processEvents()

    assert window.table.isVisible()
    assert window.editor_scroll.isVisible()
    assert window.table.selected_index() == 0


def test_edit_then_select_another_step_preserves_current_edit() -> None:
    window = window_with_actions()
    window.table.selectRow(0)
    app().processEvents()

    text_editor = window.editor.findChild(QPlainTextEdit)
    assert text_editor is not None
    text_editor.setPlainText("edited first")
    app().processEvents()
    window.table.selectRow(1)
    app().processEvents()

    assert window.project.actions[0].data["text"] == "edited first"
    assert window.table.selected_index() == 1
    assert window.editor.action is window.project.actions[1]


def test_close_details_returns_focus_to_action_table() -> None:
    window = window_with_actions()
    window.table.selectRow(0)
    app().processEvents()

    window.close_details()
    app().processEvents()

    assert window.table.selected_index() == -1
    assert window.editor_scroll.isVisible()
    assert window.editor.action is None
    assert window.focusWidget() is window.table


def test_escape_closes_detail_panel() -> None:
    window = window_with_actions()
    window.table.selectRow(0)
    app().processEvents()

    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    window.keyPressEvent(event)
    app().processEvents()

    assert window.table.selected_index() == -1
    assert window.editor_scroll.isVisible()
    assert window.editor.action is None
    assert window.table.isVisible()
    assert window.buttons["Run"].isVisible()
    assert window.buttons["Record"].isVisible()
    assert window.buttons["Generate Python"].isVisible()


def test_sanitize_flow_name_for_folder() -> None:
    assert sanitize_flow_name(" My New Flow ") == "My_New_Flow"
    assert sanitize_flow_name("Invoice/Run:01") == "Invoice_Run_01"


def test_step_details_hide_during_run_and_restore_afterward() -> None:
    window = window_with_actions()
    window.table.selectRow(0)
    app().processEvents()
    assert window.editor_scroll.isVisible()
    window._hide_details_for_run()
    assert not window.editor_scroll.isVisible()
    assert window.table.isVisible()
    window._restore_details_after_run()
    assert window.editor_scroll.isVisible()
    assert window.table.selected_index() == 0


def test_undo_redo_restores_step_edits() -> None:
    window = window_with_actions()
    window._reset_history()
    window.project.actions[0].data["text"] = "changed"
    window.mark_dirty()

    window.undo()
    assert window.project.actions[0].data["text"] == "first"

    window.redo()
    assert window.project.actions[0].data["text"] == "changed"


def test_manual_drag_form_creates_picked_positions() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {}, None)
    dialog.type_box.setCurrentIndex(dialog.type_box.findData(ActionType.DRAG.value))
    dialog.set_screen_point("start", -120, 30)
    dialog.set_screen_point("end", 240, 160)
    action = dialog.action()
    assert action.action == ActionType.DRAG.value
    assert action.data["start_x"] == -120
    assert action.data["end_y"] == 160


def test_manual_dialog_visible_confirmation_returns_accepted() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {}, None)
    QTimer.singleShot(0, lambda: QTest.mouseClick(dialog.confirm_button, Qt.LeftButton))
    assert dialog.exec() == QDialog.DialogCode.Accepted


def test_log_viewer_adds_level_and_step_context() -> None:
    window = window_with_actions()
    window.log("warning: target is not visible")
    window.set_action_status(0, "running")
    text = window.logs.toPlainText()
    assert "[Warning] warning: target is not visible" in text
    assert "[Step 1] Running" in text


def test_inserted_step_clears_filter_and_is_visible() -> None:
    window = window_with_actions()
    window.filter_box.setText("first")
    window.insert_action(RpaAction(ActionType.WAIT.value, {"seconds": 1}))
    assert window.filter_box.text() == ""
    assert window.table.rowCount() == 3
    assert not window.table.isRowHidden(2)
    assert window.table.selected_index() == 2


def test_toolbar_add_step_updates_and_persists_active_flow(tmp_path) -> None:
    from rpa.generator import generate_python
    from rpa.project_manager import ProjectManager

    window = window_with_actions()
    window.project.actions.extend(RpaAction(ActionType.WAIT.value, {"seconds": 1}) for _ in range(7))
    window.project_dir = tmp_path
    ProjectManager().save(window.project, tmp_path)
    window.open_project_path(tmp_path / "project.json")
    assert len(window.project.actions) == 9
    def accept_real_dialog() -> None:
        dialogs = [widget for widget in QApplication.topLevelWidgets() if isinstance(widget, ManualActionDialog) and widget.isVisible()]
        if not dialogs:
            QTimer.singleShot(10, accept_real_dialog)
            return
        button_box = dialogs[0].findChild(QDialogButtonBox)
        QTest.mouseClick(button_box.button(QDialogButtonBox.Ok), Qt.LeftButton)

    QTimer.singleShot(10, accept_real_dialog)
    QTest.mouseClick(window.buttons["Add Manual Action"], Qt.LeftButton)

    assert len(window.project.actions) == 10
    assert window.table.rowCount() == 10
    assert window.table.selected_index() == 9
    assert window.dirty

    window.undo()
    assert len(window.project.actions) == 9
    window.redo()
    assert len(window.project.actions) == 10

    window.save_project()
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert len(loaded.actions) == 10
    assert loaded.actions[-1].action == ActionType.CLICK_COORDINATE.value
    generated = generate_python(loaded, tmp_path).read_text(encoding="utf-8")
    assert "pyautogui.click(0, 0" in generated


def test_click_image_picker_keeps_parent_open_until_add_step(tmp_path, monkeypatch) -> None:
    import ui.main_window as main_window_module

    window = window_with_actions()
    window.project.actions.extend(RpaAction(ActionType.WAIT.value, {"seconds": 1}) for _ in range(7))
    window.project_dir = tmp_path
    ProjectManager().save(window.project, tmp_path)
    window.open_project_path(tmp_path / "project.json")
    monkeypatch.setattr(main_window_module, "screenshot_image", lambda: Image.new("RGB", (800, 600), "white"))
    monkeypatch.setattr(main_window_module, "virtual_screen_origin", lambda: (0, 0))
    observed: dict[str, bool] = {}

    def confirm_parent() -> None:
        dialog = next(widget for widget in QApplication.topLevelWidgets() if isinstance(widget, ManualActionDialog))
        observed["parent_still_open"] = dialog.isVisible() and dialog.result() != QDialog.DialogCode.Accepted
        QTest.mouseClick(dialog.confirm_button, Qt.LeftButton)

    def confirm_picker() -> None:
        overlay = next(widget for widget in QApplication.topLevelWidgets() if isinstance(widget, TargetCaptureOverlay) and widget.isVisible())
        QTest.mouseClick(overlay, Qt.LeftButton, pos=QPoint(300, 250))
        QTest.mouseClick(overlay.confirm_button, Qt.LeftButton)
        QTimer.singleShot(10, confirm_parent)

    def open_picker() -> None:
        dialog = next(widget for widget in QApplication.topLevelWidgets() if isinstance(widget, ManualActionDialog) and widget.isVisible())
        dialog.type_box.setCurrentIndex(dialog.type_box.findData(ActionType.CLICK_IMAGE.value))
        QTest.mouseClick(dialog.target_pick_button, Qt.LeftButton)
        QTimer.singleShot(250, confirm_picker)

    QTimer.singleShot(10, open_picker)
    QTest.mouseClick(window.buttons["Add Manual Action"], Qt.LeftButton)

    assert observed["parent_still_open"]
    assert len(window.project.actions) == 10
    assert window.project.actions[-1].action == ActionType.CLICK_IMAGE.value
    log_text = window.logs.toPlainText()
    assert "[Image Picker] closed: accepted" in log_text
    assert log_text.index("[Add Step] still open") < log_text.index("[Add Step] confirmation clicked")


def test_startup_reopens_last_saved_flow(tmp_path) -> None:
    project = RpaProject(actions=[RpaAction(ActionType.WAIT.value, {"seconds": 1})])
    ProjectManager().save(project, tmp_path)
    settings = QSettings("PythonRPARecorder", "PythonRPARecorder")
    previous = settings.value("last_project_path")
    settings.setValue("last_project_path", str(tmp_path / "project.json"))
    try:
        window = MainWindow()
        assert window.project_dir == tmp_path
        assert len(window.project.actions) == 1
    finally:
        if previous is None:
            settings.remove("last_project_path")
        else:
            settings.setValue("last_project_path", previous)
