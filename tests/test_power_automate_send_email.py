from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.step_editing import clipboard_payload, paste_payload
from rpa.webhook import build_email_webhook_request, execute_power_automate_email
from ui.action_editor import ActionEditor
from ui.dialogs import ManualActionDialog
from ui.webhook_action_editor import PowerAutomateEmailEditor


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def email_data(**overrides) -> dict:
    data = {
        "url": "https://example.invalid/{{hook_id}}",
        "to": "{{recipient}}",
        "cc": "",
        "subject": "Run {{run_id}} completed",
        "body": "<p>Hello {{name}}, run <strong>{{run_id}}</strong> completed.</p>",
        "timeout": 60.0,
        "output_variable": "email_response",
        "failure_action": "stop",
    }
    data.update(overrides)
    return data


class FakeResponse:
    status_code = 202
    text = "accepted"

    def json(self):
        return {"messageId": "mail-42"}


def test_email_payload_mapping_substitution_optional_cc_and_html() -> None:
    url, payload, timeout = build_email_webhook_request(email_data(), {
        "hook_id": "abc", "recipient": "person@example.com",
        "run_id": 42, "name": "Ada",
    })
    assert url == "https://example.invalid/abc"
    assert payload == {
        "to": "person@example.com",
        "cc": "",
        "subject": "Run 42 completed",
        "body": "<p>Hello Ada, run <strong>42</strong> completed.</p>",
    }
    assert list(payload) == ["to", "cc", "subject", "body"]
    assert timeout == 60.0


def test_email_post_reuses_webhook_response_storage(monkeypatch) -> None:
    observed = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr("rpa.webhook.requests.post", fake_post)
    variables = {
        "hook_id": "abc", "recipient": "person@example.com",
        "run_id": 42, "name": "Ada",
    }
    result = execute_power_automate_email(email_data(cc="audit@example.com"), variables)
    assert result == {"messageId": "mail-42"}
    assert variables["email_response"] == result
    assert observed["json"] == {
        "to": "person@example.com",
        "cc": "audit@example.com",
        "subject": "Run 42 completed",
        "body": "<p>Hello Ada, run <strong>42</strong> completed.</p>",
    }
    assert observed["timeout"] == 60.0


def test_email_action_is_available_in_guided_and_full_editors() -> None:
    app()
    guided = ManualActionDialog(RpaProject().settings, {})
    guided.select_intent("script")
    choices = [guided.guided_type_box.itemData(i) for i in range(guided.guided_type_box.count())]
    assert ActionType.POWER_AUTOMATE_SEND_EMAIL.value in choices
    assert guided.type_box.findData(ActionType.POWER_AUTOMATE_SEND_EMAIL.value) >= 0

    full = ActionEditor()
    full.set_action(RpaAction(ActionType.POWER_AUTOMATE_SEND_EMAIL.value, email_data()), None)
    editor = full.findChild(PowerAutomateEmailEditor)
    assert editor is not None
    assert editor.body.minimumHeight() >= 190


def test_email_save_reopen_duplicate_and_generated_python_preserve_fields(tmp_path) -> None:
    action = RpaAction(ActionType.POWER_AUTOMATE_SEND_EMAIL.value, email_data())
    project = RpaProject(actions=[action])
    ProjectManager().save(project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert loaded.actions[0].data == action.data

    payload, error = clipboard_payload(loaded.actions, [0])
    assert error is None
    duplicated, selected, error = paste_payload(loaded.actions, payload, 1)
    assert error is None and duplicated is not None
    duplicate = duplicated[selected[0]]
    duplicate.data["subject"] = "Independent subject"
    assert loaded.actions[0].data["subject"] == "Run {{run_id}} completed"

    generated = generate_python(RpaProject(actions=duplicated), tmp_path).read_text(encoding="utf-8")
    assert "power_automate_send_email" in generated
    assert "Independent subject" in generated
