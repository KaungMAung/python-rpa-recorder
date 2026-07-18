from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from rpa.models import ActionType, ProjectSettings, RpaAction
from ui.dialogs import ManualActionDialog
from ui.main_window import MainWindow
from ui.main_window import sanitize_flow_name


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
