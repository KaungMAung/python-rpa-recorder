from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from rpa.models import ActionType, RpaAction, RpaProject
from rpa.project_manager import ProjectManager
from rpa.validator import LEVEL_ERROR, validate_project_detailed
from ui.main_window import MainWindow


def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def saved_window(tmp_path, monkeypatch):
    root = tmp_path / "flows"
    root.mkdir()
    monkeypatch.setattr("ui.main_window.flows_root", lambda: root)
    source = root / "Original"
    project = RpaProject(actions=[
        RpaAction(ActionType.CLICK_IMAGE.value, {
            "image": "screenshots/target.png", "use_coordinate_fallback": False,
        }),
    ])
    project.project.name = "Original"
    manager = ProjectManager()
    manager.save(project, source)
    (source / "screenshots" / "target.png").write_bytes(b"image-data")
    (source / "generated" / "helper.txt").write_text("related asset", encoding="utf-8")
    window = MainWindow()
    window.project = manager.load(source / "project.json")
    window.project_dir = source
    window.manager = manager
    window.dirty = True
    window.refresh()
    return window, root, source


def test_save_as_new_name_copies_assets_preserves_original_and_reopens(tmp_path, monkeypatch) -> None:
    app()
    window, root, source = saved_window(tmp_path, monkeypatch)
    original_json = (source / "project.json").read_bytes()
    prompt = {}

    def get_name(_parent, _title, label, _mode, text):
        prompt.update(label=label, text=text)
        return "Copied Flow", True

    monkeypatch.setattr(QInputDialog, "getText", get_name)
    window.save_as_project()

    target = root / "Copied_Flow"
    assert str(root) in prompt["label"]
    assert target.is_dir()
    assert (target / "project.json").is_file()
    assert (target / "screenshots" / "target.png").read_bytes() == b"image-data"
    assert (target / "generated" / "helper.txt").read_text(encoding="utf-8") == "related asset"
    assert (source / "project.json").read_bytes() == original_json
    assert (source / "screenshots" / "target.png").read_bytes() == b"image-data"
    assert window.project_dir == target
    assert window.project.project.name == "Copied_Flow"
    assert window.project.actions[0].data["image"] == "screenshots/target.png"
    reopened = ProjectManager().load(target / "project.json")
    assert reopened.project.name == "Copied_Flow"
    assert reopened.actions[0].data["image"] == "screenshots/target.png"
    assert not [issue for issue in validate_project_detailed(reopened, target) if issue.level == LEVEL_ERROR]
    window.close()


def test_save_as_existing_name_keeps_existing_conflict_behavior(tmp_path, monkeypatch) -> None:
    app()
    window, root, source = saved_window(tmp_path, monkeypatch)
    existing = root / "Existing"
    ProjectManager().save(RpaProject(), existing)
    before = (existing / "project.json").read_bytes()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("Existing", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.No)

    window.save_as_project()

    assert (existing / "project.json").read_bytes() == before
    assert window.project_dir == source
    window.close()


def test_save_as_can_confirm_overwrite_of_an_existing_flow(tmp_path, monkeypatch) -> None:
    app()
    window, root, source = saved_window(tmp_path, monkeypatch)
    existing = root / "Existing"
    old = RpaProject(actions=[RpaAction(ActionType.TYPE_TEXT.value, {"text": "old"})])
    ProjectManager().save(old, existing)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("Existing", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes)

    window.save_as_project()

    loaded = ProjectManager().load(existing / "project.json")
    assert loaded.project.name == "Existing"
    assert loaded.actions[0].action == ActionType.CLICK_IMAGE.value
    assert (existing / "screenshots" / "target.png").read_bytes() == b"image-data"
    assert window.project_dir == existing
    assert (source / "screenshots" / "target.png").read_bytes() == b"image-data"
    window.close()


def test_save_as_rejects_invalid_flow_name(tmp_path, monkeypatch) -> None:
    app()
    window, root, source = saved_window(tmp_path, monkeypatch)
    errors = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args: ("///", True))
    monkeypatch.setattr("ui.main_window.show_error", lambda _parent, title, message: errors.append((title, message)))

    window.save_as_project()

    assert errors and errors[0][0] == "Invalid flow name"
    assert list(root.iterdir()) == [source]
    assert window.project_dir == source
    window.close()
