from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import requests
from PySide6.QtWidgets import QApplication, QMessageBox

from rpa.generator import generate_python
from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.runner import ReplayActionError, ReplayRunner
from rpa.step_editing import clipboard_payload, paste_payload
from rpa.validator import LEVEL_ERROR, validate_project_detailed
from rpa.webhook import (
    builder_rows_to_object, execute_webhook, plain_json_to_object,
)
from ui.webhook_action_editor import WebhookActionEditor
from ui.action_editor import ActionEditor
from ui.dialogs import ManualActionDialog


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def webhook_data(**overrides) -> dict:
    data = {
        "url": "https://example.invalid/{{hook_id}}",
        "payload_mode": "builder",
        "payload_fields": [{"name": "message", "value": "Run {{run_id}} completed", "type": "text"}],
        "json_payload": "{}",
        "timeout": 60.0,
        "output_variable": "response",
        "failure_action": "stop",
    }
    data.update(overrides)
    return data


class FakeResponse:
    def __init__(self, status_code=200, json_value=None, text="", json_error=False) -> None:
        self.status_code = status_code
        self._json_value = json_value
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not JSON")
        return self._json_value


def test_builder_payload_converts_types_and_variables() -> None:
    rows = [
        {"name": "message", "value": "Run {{run_id}} completed", "type": "text"},
        {"name": "count", "value": "{{count}}", "type": "number"},
        {"name": "approved", "value": "{{approved}}", "type": "boolean"},
        {"name": "empty", "value": "ignored", "type": "null"},
    ]
    assert builder_rows_to_object(rows, {"run_id": "A7", "count": "12.5", "approved": "true"}) == {
        "message": "Run A7 completed", "count": 12.5, "approved": True, "empty": None,
    }
    with pytest.raises(ValueError, match="duplicated"):
        builder_rows_to_object(rows + [rows[0]], {"run_id": "A7", "count": 1, "approved": False})
    with pytest.raises(ValueError, match="valid number"):
        builder_rows_to_object([{"name": "count", "value": "many", "type": "number"}])


def test_plain_json_validates_after_variable_substitution() -> None:
    value = plain_json_to_object(
        '{"id":"{{run_id}}","message":"Run {{run_id}} completed","nested":{"ok":"{{approved}}"}}',
        {"run_id": 42, "approved": True},
    )
    assert value == {"id": 42, "message": "Run 42 completed", "nested": {"ok": True}}
    with pytest.raises(ValueError, match="Invalid JSON"):
        plain_json_to_object('{"broken": }')


def test_invalid_payload_is_rejected_before_http_post(monkeypatch) -> None:
    called = False

    def fake_post(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("rpa.webhook.requests.post", fake_post)
    with pytest.raises(ValueError, match="valid number"):
        execute_webhook(webhook_data(payload_fields=[
            {"name": "count", "value": "many", "type": "number"},
        ]), {"hook_id": "abc"})
    with pytest.raises(ValueError, match="Invalid JSON"):
        execute_webhook(webhook_data(
            payload_mode="json", json_payload='{"broken": }',
        ), {"hook_id": "abc"})
    assert not called


def test_flow_validation_reports_invalid_webhook_payload() -> None:
    project = RpaProject(actions=[RpaAction(
        ActionType.POWER_AUTOMATE_WEBHOOK.value,
        webhook_data(payload_fields=[{"name": "count", "value": "many", "type": "number"}]),
    )])
    project.variables.update({"hook_id": "abc", "run_id": 9})
    reasons = [
        issue.reason for issue in validate_project_detailed(project)
        if issue.level == LEVEL_ERROR
    ]
    assert any("valid number" in reason for reason in reasons)


def test_action_is_available_in_guided_and_full_step_editors() -> None:
    app()
    guided = ManualActionDialog(RpaProject().settings, {})
    guided.select_intent("script")
    choices = [guided.guided_type_box.itemData(i) for i in range(guided.guided_type_box.count())]
    assert ActionType.POWER_AUTOMATE_WEBHOOK.value in choices
    assert guided.type_box.findData(ActionType.POWER_AUTOMATE_WEBHOOK.value) >= 0

    action = RpaAction(ActionType.POWER_AUTOMATE_WEBHOOK.value, webhook_data())
    full = ActionEditor()
    full.set_action(action, None)
    assert full.findChild(WebhookActionEditor) is not None


def test_builder_json_mode_conversion_preserves_rows_and_rejects_nested_builder(monkeypatch) -> None:
    app()
    editor = WebhookActionEditor(webhook_data(payload_fields=[
        {"name": "count", "value": "{{count}}", "type": "number"},
        {"name": "ready", "value": "true", "type": "boolean"},
    ]))
    original = editor.payload_rows()
    editor.mode.setCurrentIndex(editor.mode.findData("json"))
    assert '"count": "{{count}}"' in editor.json_editor.toPlainText()
    editor.mode.setCurrentIndex(editor.mode.findData("builder"))
    assert editor.payload_rows() == original

    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _parent, _title, message: messages.append(message))
    editor.mode.setCurrentIndex(editor.mode.findData("json"))
    editor.json_editor.setPlainText('{"nested":{"value":1}}')
    editor.mode.setCurrentIndex(editor.mode.findData("builder"))
    assert editor.mode.currentData() == "json"
    assert editor.json_editor.toPlainText() == '{"nested":{"value":1}}'
    assert messages and "flat object" in messages[-1]


def test_successful_post_saves_json_response_and_resolves_request(monkeypatch) -> None:
    observed = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return FakeResponse(json_value={"run": "accepted"})

    monkeypatch.setattr("rpa.webhook.requests.post", fake_post)
    variables = {"hook_id": "abc", "run_id": 9}
    result = execute_webhook(webhook_data(), variables)
    assert result == {"run": "accepted"}
    assert variables["response"] == {"run": "accepted"}
    assert observed == {
        "url": "https://example.invalid/abc",
        "json": {"message": "Run 9 completed"},
        "timeout": 60.0,
    }


def test_successful_post_saves_text_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "rpa.webhook.requests.post",
        lambda *_args, **_kwargs: FakeResponse(text="accepted", json_error=True),
    )
    variables = {"hook_id": "abc", "run_id": 9}
    assert execute_webhook(webhook_data(), variables) == "accepted"
    assert variables["response"] == "accepted"


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (requests.Timeout("slow"), "timed out"),
        (requests.ConnectionError("offline"), "request failed"),
        (FakeResponse(status_code=503), "HTTP 503"),
    ],
)
def test_timeout_network_and_non_2xx_are_failures(monkeypatch, outcome, message) -> None:
    def fake_post(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("rpa.webhook.requests.post", fake_post)
    with pytest.raises((TimeoutError, RuntimeError), match=message):
        execute_webhook(webhook_data(), {"hook_id": "abc", "run_id": 9})


def test_non_2xx_uses_existing_continue_failure_policy(tmp_path, monkeypatch) -> None:
    calls = 0

    def failed_post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(status_code=500)

    monkeypatch.setattr("rpa.webhook.requests.post", failed_post)
    actions = [
        RpaAction(ActionType.POWER_AUTOMATE_WEBHOOK.value, webhook_data(
            failure_action="continue", retry_count=3,
        )),
        RpaAction(ActionType.SET_VARIABLE.value, {"variable": "continued", "value": True}),
    ]
    runner = ReplayRunner(RpaProject(actions=actions), tmp_path, lambda _message: None)
    runner.runtime_variables.update({"hook_id": "abc", "run_id": 9})
    runner.run(include_start_delay=False)
    assert runner.runtime_variables["continued"] is True
    assert runner.had_continued_failures
    assert calls == 1


def test_save_reopen_duplicate_and_generated_python_preserve_independent_payload(tmp_path) -> None:
    action = RpaAction(ActionType.POWER_AUTOMATE_WEBHOOK.value, webhook_data(payload_fields=[
        {"name": "run", "value": "{{run_id}}", "type": "number"},
    ]))
    project = RpaProject(actions=[action])
    ProjectManager().save(project, tmp_path)
    loaded = ProjectManager().load(tmp_path / "project.json")
    assert loaded.actions[0].data == action.data

    payload, error = clipboard_payload(loaded.actions, [0])
    assert error is None
    duplicated, selected, error = paste_payload(loaded.actions, payload, 1)
    assert error is None and duplicated is not None
    duplicate = duplicated[selected[0]]
    assert duplicate.id != loaded.actions[0].id
    duplicate.data["payload_fields"][0]["value"] = "99"
    assert loaded.actions[0].data["payload_fields"][0]["value"] == "{{run_id}}"

    generated = generate_python(RpaProject(actions=duplicated), tmp_path).read_text(encoding="utf-8")
    assert "power_automate_webhook" in generated
    assert "payload_fields" in generated
    assert "requests>=2.32,<3" in (tmp_path / "generated" / "requirements.txt").read_text(encoding="utf-8")
