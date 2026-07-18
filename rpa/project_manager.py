from __future__ import annotations

import json
import shutil
from pathlib import Path

from .models import ProjectSettings, RpaProject, utc_now
from .utils import ensure_project_dirs


class ProjectManager:
    def __init__(self) -> None:
        self.project_dir: Path | None = None

    def new_project(self, name: str = "Untitled Recording", settings: ProjectSettings | None = None) -> RpaProject:
        project = RpaProject()
        if settings is not None:
            project.settings = settings
        project.project.name = name
        return project

    def save(self, project: RpaProject, project_dir: Path) -> Path:
        project_dir = Path(project_dir)
        ensure_project_dirs(project_dir)
        project.project.updated_at = utc_now()
        path = project_dir / "project.json"
        path.write_text(json.dumps(project.to_dict(), indent=2), encoding="utf-8")
        self.project_dir = project_dir
        return path

    def save_as(self, project: RpaProject, source_dir: Path | None, target_dir: Path) -> Path:
        target_dir = Path(target_dir)
        ensure_project_dirs(target_dir)
        if source_dir and Path(source_dir).exists() and Path(source_dir).resolve() != target_dir.resolve():
            src = Path(source_dir) / "screenshots"
            dst = target_dir / "screenshots"
            if src.exists():
                for item in src.glob("*"):
                    if item.is_file():
                        shutil.copy2(item, dst / item.name)
        return self.save(project, target_dir)

    def load(self, project_json: Path) -> RpaProject:
        project_json = Path(project_json)
        try:
            project = RpaProject.from_dict(json.loads(project_json.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid project file: {exc}") from exc
        self.project_dir = project_json.parent
        ensure_project_dirs(self.project_dir)
        return project
