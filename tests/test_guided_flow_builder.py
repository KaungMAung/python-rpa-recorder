from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

from rpa.models import ActionType, ProjectSettings, RpaAction, RpaProject
from ui.action_editor import ActionEditor
from ui.dialogs import ManualActionDialog
from ui.main_window import MainWindow


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_builder_starts_with_plain_language_intents() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {})

    assert dialog.pages.currentWidget() is dialog.intent_page
    assert [button.text() for button in dialog.intent_buttons.values()] == [
        "Click something", "Type text", "Open an application", "Wait for something",
        "Work with a window", "Work with a file", "Add a condition", "Repeat steps",
        "Run another flow", "Work with a variable", "Run a script or command",
    ]
    assert dialog.type_selector_widget.isHidden()
    assert any(
        button.text() == "Use the full step editor"
        for button in dialog.intent_page.findChildren(QPushButton)
    )
    dialog.close()


def test_guided_type_text_validates_each_stage_and_builds_existing_action() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {"CUSTOMER": "Project variable"})
    dialog.select_intent("type")
    assert dialog.pages.currentWidget() is dialog.choice_page
    assert not dialog.choice_continue.isEnabled()

    dialog.guided_type_box.setCurrentIndex(
        dialog.guided_type_box.findData(ActionType.TYPE_TEXT.value)
    )
    assert dialog.choice_continue.isEnabled()
    dialog._show_guided_details()
    assert dialog.pages.currentWidget() is dialog.details_page
    assert "Enter the text" in dialog.validation_label.text()

    dialog.text.setPlainText("Hello {{CUSTOMER}}")
    assert "Ready to add" in dialog.validation_label.text()
    assert "Hello" in dialog.summary.text()
    action = dialog.action()
    assert action.action == ActionType.TYPE_TEXT.value
    assert action.data["text"] == "Hello {{CUSTOMER}}"
    dialog.close()


def test_guided_screens_have_no_advanced_section_for_any_action_type() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {})
    assert any(
        button.text() == "Use the full step editor"
        for button in dialog.intent_page.findChildren(QPushButton)
    )
    for intent, _label, _help, choices in dialog.GUIDED_INTENTS:
        for _choice_label, action_type in choices:
            dialog.select_intent(intent, action_type)
            assert any(
                button.text() == "Use the full step editor"
                for button in dialog.choice_page.findChildren(QPushButton)
            )
            dialog._show_guided_details()
            assert not any(
                button.text().startswith("Advanced")
                for button in dialog.details_page.findChildren(QPushButton)
            ), action_type
    dialog.close()


def test_guided_utility_hides_optional_fields_and_full_editor_preserves_values() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {})
    dialog.select_intent("open", ActionType.LAUNCH_APPLICATION.value)
    dialog._show_guided_details()

    utility = dialog.utility_editor
    utility.controls["path"].setText(r"C:\Tools\worker.exe")
    assert utility.controls["path"].isVisibleTo(utility)
    assert not utility.controls["arguments"].isVisibleTo(utility)
    assert not any(
        button.text().startswith("Advanced")
        for button in utility.findChildren(QPushButton)
    )

    dialog._use_full_editor()
    assert not dialog.type_selector_widget.isHidden()
    assert dialog.type_box.count() > dialog.guided_type_box.count()
    assert utility.controls["arguments"].isVisibleTo(utility)
    assert utility.controls["path"].text() == r"C:\Tools\worker.exe"
    assert dialog.action().data["path"] == r"C:\Tools\worker.exe"
    dialog.close()


def test_full_editor_handoff_preserves_subtype_and_guided_field_values() -> None:
    app()
    dialog = ManualActionDialog(ProjectSettings(), {})
    dialog.select_intent("type", ActionType.TYPE_TEXT.value)
    full_button = next(
        button for button in dialog.choice_page.findChildren(QPushButton)
        if button.text() == "Use the full step editor"
    )
    full_button.click()
    assert dialog.type_box.currentData() == ActionType.TYPE_TEXT.value

    dialog._back_from_details()
    dialog.select_intent("type", ActionType.TYPE_TEXT.value)
    dialog._show_guided_details()
    dialog.text.setPlainText("Invoice {{NUMBER}}")
    dialog._use_full_editor()
    assert dialog.text.toPlainText() == "Invoice {{NUMBER}}"
    assert dialog.action().data["text"] == "Invoice {{NUMBER}}"
    dialog.close()


def test_actual_step_editor_still_exposes_advanced_settings() -> None:
    app()
    editor = ActionEditor()
    editor.set_action(RpaAction(ActionType.WAIT.value, {"seconds": 1}), None)
    assert editor.advanced_button.text().startswith("Advanced Settings")
    assert not editor.advanced_button.isHidden()
    editor.close()


def test_relevant_test_controls_emit_the_same_rpa_action_model(tmp_path) -> None:
    app()
    image = tmp_path / "target.png"
    image.write_bytes(b"image")
    dialog = ManualActionDialog(ProjectSettings(), {}, project_dir=tmp_path)
    dialog.select_intent("click", ActionType.CLICK_IMAGE.value)
    dialog._show_guided_details()
    dialog.image_file.setText(str(image))
    dialog.capture_image.setChecked(True)
    dialog._update_summary()

    matches = []
    tests = []
    dialog.test_match_requested.connect(matches.append)
    dialog.test_step_requested.connect(tests.append)
    dialog.test_match_button.click()
    dialog.test_step_button.click()

    assert matches and matches[0].action == ActionType.CLICK_IMAGE.value
    assert tests and tests[0].action == ActionType.CLICK_IMAGE.value
    assert matches[0].data["image"] == str(image)
    dialog.finish_step_test()
    dialog.close()


def test_draft_step_test_uses_shared_replay_path_without_inserting(monkeypatch) -> None:
    application = app()
    window = MainWindow()
    window.project = RpaProject()
    window.refresh()
    dialog = ManualActionDialog(ProjectSettings(), {}, window)
    action = RpaAction(ActionType.WAIT.value, {"seconds": 0.1})
    observed = []

    def start_replay(start, end, mode, *_args, **_kwargs):
        observed.append((start, end, mode, [item.id for item in window.project.actions]))

    monkeypatch.setattr(window, "_start_replay", start_replay)
    window._test_manual_action(dialog, action)

    assert observed == [(0, 0, "test", [action.id])]
    assert window.project.actions == []
    assert window._manual_test_action_id is None
    window.close()
    dialog.close()
    application.processEvents()
