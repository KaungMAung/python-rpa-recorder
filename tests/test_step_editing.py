from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QPointF, Qt
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from rpa.control_flow import parse_control_flow
from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.step_editing import clipboard_payload, paste_payload, reorder_steps
from rpa.validator import LEVEL_ERROR, validate_project_detailed
from ui.main_window import MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _select(window: MainWindow, rows: list[int]) -> None:
    window.table.clearSelection()
    for row in rows:
        window.table.selectionModel().select(
            window.table.model().index(row, 0),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )


def test_reorder_preserves_ids_and_failure_jump_target() -> None:
    first = RpaAction(ActionType.WAIT.value, {"seconds": 1, "failure_action": "jump", "failure_jump_step": 3})
    second = RpaAction(ActionType.TYPE_TEXT.value, {"text": "middle"})
    target = RpaAction(ActionType.TYPE_TEXT.value, {"text": "target"})

    reordered, error = reorder_steps([first, second, target], [2], 0)

    assert error is None
    assert [item.id for item in reordered] == [target.id, first.id, second.id]
    assert first.data["failure_jump_step"] == 1


def test_reorder_rejects_move_that_breaks_if_block() -> None:
    actions = [
        RpaAction(ActionType.IF_VARIABLE.value, {"variable": "X", "operator": "is_empty"}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "inside"}),
        RpaAction(ActionType.END_IF.value, {}),
    ]
    reordered, error = reorder_steps(actions, [0], 3)
    assert reordered is None
    assert error


def test_group_end_must_match_group_identity() -> None:
    flow = parse_control_flow([
        RpaAction(ActionType.GROUP_START.value, {"name": "A", "group_id": "a"}),
        RpaAction(ActionType.GROUP_END.value, {"group_id": "b"}),
    ])
    assert "different group" in flow.issues[0].reason


def test_copy_paste_uses_new_ids_and_remaps_internal_jump() -> None:
    source = RpaAction(ActionType.WAIT.value, {"seconds": 1, "failure_action": "jump", "failure_jump_step": 2})
    target = RpaAction(ActionType.TYPE_TEXT.value, {"text": "target"})
    payload, error = clipboard_payload([source, target], [0, 1])
    assert error is None

    pasted, selected, error = paste_payload([source, target], payload, 2)

    assert error is None
    assert selected == [2, 3]
    assert pasted[2].id not in {source.id, target.id}
    assert pasted[3].id not in {source.id, target.id}
    assert pasted[2].data["failure_jump_step"] == 4


def test_groups_comments_save_reload_validate_and_generate(tmp_path: Path) -> None:
    group_id = "review-section"
    project = RpaProject(actions=[
        RpaAction(ActionType.GROUP_START.value, {"name": "Review invoice", "group_id": group_id, "collapsed": True}),
        RpaAction(ActionType.COMMENT.value, {"text": "Confirm the invoice total"}),
        RpaAction(ActionType.WAIT.value, {"seconds": 0}),
        RpaAction(ActionType.GROUP_END.value, {"group_id": group_id}),
    ])
    ProjectManager().save(project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    flow = parse_control_flow(loaded.actions)

    assert not flow.issues
    assert flow.group_start_end == {0: 3}
    assert loaded.actions[0].data["collapsed"] is True
    assert not [issue for issue in validate_project_detailed(loaded, tmp_path) if issue.level == LEVEL_ERROR]
    assert not [
        issue for issue in validate_project_detailed(loaded, tmp_path, start_index=2, end_index=2)
        if issue.level == LEVEL_ERROR
    ]
    script = generate_python(loaded, tmp_path).read_text(encoding="utf-8")
    assert "# region Review invoice" in script
    assert "# Note: Confirm the invoice total" in script
    compile(script, "generated_rpa.py", "exec")


def test_runner_treats_groups_and_comments_as_non_executable_metadata(tmp_path: Path, monkeypatch) -> None:
    import rpa.runner as runner_module
    from rpa.runner import ReplayRunner

    monkeypatch.setattr(runner_module, "pyautogui", SimpleNamespace(FAILSAFE=True))
    group_id = "runtime-group"
    project = RpaProject(actions=[
        RpaAction(ActionType.GROUP_START.value, {"name": "Runtime", "group_id": group_id}),
        RpaAction(ActionType.COMMENT.value, {"text": "No automation for this row"}),
        RpaAction(ActionType.WAIT.value, {"seconds": 0}),
        RpaAction(ActionType.GROUP_END.value, {"group_id": group_id}),
    ])
    project.settings.start_delay = 0
    runner = ReplayRunner(project, tmp_path, lambda _message: None)

    runner.run(include_start_delay=False)

    assert runner.total_attempts == 1
    assert project.actions[0].status == "completed"
    assert project.actions[1].status == "completed"


def test_ui_bulk_edit_group_collapse_and_undo(monkeypatch) -> None:
    app = _app()
    window = MainWindow()
    window.project = RpaProject(actions=[
        RpaAction(ActionType.WAIT.value, {"seconds": 1}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "two"}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "three"}),
    ])
    window.refresh()
    window._reset_history()
    _select(window, [0, 1])
    monkeypatch.setattr("ui.main_window.QInputDialog.getText", lambda *args, **kwargs: ("Preparation", True))

    window.group_selected_steps()

    assert [item.action for item in window.project.actions[:4]] == [
        ActionType.GROUP_START.value, ActionType.WAIT.value,
        ActionType.TYPE_TEXT.value, ActionType.GROUP_END.value,
    ]
    window.table._cell_clicked(0, 0)
    assert window.project.actions[0].data["collapsed"] is True
    window.undo()
    assert window.project.actions[0].data.get("collapsed") is False
    window.undo()
    assert len(window.project.actions) == 3
    window.redo()
    assert window.project.actions[0].action == ActionType.GROUP_START.value
    window.close()
    app.processEvents()


def test_manual_insertion_preserves_existing_jump_target() -> None:
    _app()
    source = RpaAction(ActionType.WAIT.value, {
        "seconds": 1, "failure_action": "jump", "failure_jump_step": 2,
    })
    target = RpaAction(ActionType.TYPE_TEXT.value, {"text": "target"})
    window = MainWindow()
    window.project = RpaProject(actions=[source, target])
    window.refresh()
    window.table.selectRow(0)

    assert window.insert_action(RpaAction(ActionType.COMMENT.value, {"text": "before"}), "before")

    assert source.data["failure_jump_step"] == 3
    assert window.project.actions[2].id == target.id
    window.close()


def test_filter_blocks_reorder_without_changing_order(monkeypatch) -> None:
    _app()
    window = MainWindow()
    window.project = RpaProject(actions=[
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "first"}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "second"}),
    ])
    window.refresh()
    original = [action.id for action in window.project.actions]
    window.filter_box.setText("first")
    monkeypatch.setattr("ui.main_window.show_error", lambda *args, **kwargs: None)
    window.reorder_selected_steps([0], 2)
    assert [action.id for action in window.project.actions] == original
    window.close()


def test_real_table_drop_renders_moved_row_immediately_and_round_trips(tmp_path: Path) -> None:
    app = _app()
    window = MainWindow()
    actions = [
        RpaAction(ActionType.WAIT.value, {"seconds": index / 10}, name=f"Named step {index + 1}")
        for index in range(14)
    ]
    moved_id = actions[2].id
    original_ids = [action.id for action in actions]
    window.project = RpaProject(actions=actions)
    window.refresh()
    window._reset_history()
    window.resize(1200, 800)
    window.show()
    app.processEvents()

    window.table.selectRow(2)
    source_indexes = [window.table.model().index(2, column) for column in range(window.table.columnCount())]
    mime_data = window.table.model().mimeData(source_indexes)
    destination_rect = window.table.visualRect(window.table.model().index(10, 0))
    drop_position = QPointF(destination_rect.center().x(), destination_rect.bottom() - 1)
    event = QDropEvent(
        drop_position, Qt.MoveAction, mime_data, Qt.LeftButton, Qt.NoModifier,
    )

    # Invoke the actual ActionTable drop path. The model mutation is deliberately
    # finalized on the next Qt turn, after QTableWidget's InternalMove cleanup.
    window.table.dropEvent(event)
    assert event.isAccepted()
    assert [action.id for action in window.project.actions] == original_ids
    app.processEvents()

    assert window.project.actions[10].id == moved_id
    assert window.table.selected_indices() == [10]
    assert window.editor.action is not None and window.editor.action.id == moved_id
    for row in range(window.table.rowCount()):
        assert window.table.item(row, 0) is not None
        assert window.table.item(row, 0).text() == str(row + 1)
        for column in range(window.table.columnCount()):
            assert window.table.item(row, column) is not None
    assert window.table.item(10, 2).text() == "Named step 3"

    window.filter_box.setText("Named step 3")
    assert not window.table.isRowHidden(10)
    assert sum(not window.table.isRowHidden(row) for row in range(window.table.rowCount())) == 1
    window.filter_box.clear()

    window.undo()
    assert [action.id for action in window.project.actions] == original_ids
    window.redo()
    assert window.project.actions[10].id == moved_id

    ProjectManager().save(window.project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert loaded.actions[10].id == moved_id
    generated = generate_python(loaded, tmp_path)
    compile(generated.read_text(encoding="utf-8"), str(generated), "exec")
    window.close()
    app.processEvents()


def test_ui_bulk_enable_wait_delete_and_clipboard_are_undoable(monkeypatch) -> None:
    _app()
    window = MainWindow()
    window.project = RpaProject(actions=[
        RpaAction(ActionType.WAIT.value, {"seconds": 1}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "two"}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "three"}),
    ])
    window.refresh()
    window._reset_history()
    _select(window, [0, 1])

    window.set_selected_enabled(False)
    assert [item.enabled for item in window.project.actions[:2]] == [False, False]
    window.undo()
    assert [item.enabled for item in window.project.actions[:2]] == [True, True]

    _select(window, [0, 1])
    monkeypatch.setattr("ui.main_window.QInputDialog.getDouble", lambda *args, **kwargs: (2.5, True))
    window.adjust_selected_wait()
    assert [item.delay_before for item in window.project.actions[:2]] == [2.5, 2.5]
    window.undo()

    _select(window, [0, 1])
    window.copy_steps()
    window.paste_steps()
    assert len(window.project.actions) == 5
    assert len({item.id for item in window.project.actions}) == 5
    window.undo()
    assert len(window.project.actions) == 3

    _select(window, [0, 2])
    window.delete_action()
    assert len(window.project.actions) == 1
    window.undo()
    assert len(window.project.actions) == 3
    window.close()


def test_move_into_and_out_of_named_group(monkeypatch) -> None:
    _app()
    group_id = "group-a"
    window = MainWindow()
    outside = RpaAction(ActionType.TYPE_TEXT.value, {"text": "outside"})
    window.project = RpaProject(actions=[
        RpaAction(ActionType.GROUP_START.value, {"name": "A", "group_id": group_id}),
        RpaAction(ActionType.TYPE_TEXT.value, {"text": "inside"}),
        RpaAction(ActionType.GROUP_END.value, {"group_id": group_id}),
        outside,
    ])
    window.refresh()
    window._reset_history()
    _select(window, [3])
    monkeypatch.setattr("ui.main_window.QInputDialog.getItem", lambda *args, **kwargs: ("A (Step 1)", True))

    window.move_selected_into_group()
    assert [item.action for item in window.project.actions] == [
        ActionType.GROUP_START.value, ActionType.TYPE_TEXT.value,
        ActionType.TYPE_TEXT.value, ActionType.GROUP_END.value,
    ]
    assert window.project.actions[2].id == outside.id

    _select(window, [2])
    window.move_selected_out_of_group()
    assert window.project.actions[-1].id == outside.id
    assert not parse_control_flow(window.project.actions).issues
    window.undo()
    assert window.project.actions[2].id == outside.id
    window.close()
