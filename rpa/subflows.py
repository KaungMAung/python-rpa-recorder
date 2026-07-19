"""Portable subflow discovery, resolution, and dependency validation."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ActionType, RpaProject
from .project_manager import ProjectManager

MAX_SUBFLOW_DEPTH = 10


@dataclass(frozen=True)
class SavedFlow:
    name: str
    project_json: Path
    reference: str


def portable_reference(parent_project_dir: Path, target_project_json: Path) -> str:
    return Path(os.path.relpath(Path(target_project_json).resolve(), Path(parent_project_dir).resolve())).as_posix()


def resolve_subflow_project(parent_project_dir: Path, reference: str) -> Path:
    value = str(reference or "").strip()
    if not value:
        raise ValueError("choose a target flow")
    path = Path(value)
    if path.is_absolute():
        raise ValueError("subflow references must be relative so the project remains portable")
    return (Path(parent_project_dir).resolve() / path).resolve()


def discover_saved_flows(parent_project_dir: Path) -> list[SavedFlow]:
    parent = Path(parent_project_dir).resolve()
    root = parent.parent
    flows: list[SavedFlow] = []
    if not root.is_dir():
        return flows
    for child in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        project_json = child / "project.json"
        if child.is_dir() and project_json.is_file() and child.resolve() != parent:
            flows.append(SavedFlow(child.name, project_json, portable_reference(parent, project_json)))
    return flows


def validate_subflow_dependencies(
    project: RpaProject, project_dir: Path, max_depth: int = MAX_SUBFLOW_DEPTH,
) -> list[tuple[int, str]]:
    """Return root-step dependency errors as ``(step_number, reason)`` tuples."""
    root_json = (Path(project_dir).resolve() / "project.json").resolve()
    issues: list[tuple[int, str]] = []

    def walk(current: RpaProject, current_dir: Path, stack: list[Path], depth: int, root_step: int) -> None:
        for index, action in enumerate(current.actions):
            if action.action != ActionType.RUN_SUBFLOW.value:
                continue
            reported_step = root_step or index + 1
            try:
                target = resolve_subflow_project(current_dir, str(action.data.get("project", "")))
            except ValueError as exc:
                issues.append((reported_step, str(exc)))
                continue
            if not target.is_file():
                issues.append((reported_step, f"subflow project is missing: {action.data.get('project') or target}"))
                continue
            if target in stack:
                chain = " -> ".join(path.parent.name for path in [*stack, target])
                issues.append((reported_step, f"circular subflow reference: {chain}"))
                continue
            if depth >= max_depth:
                issues.append((reported_step, f"subflow nesting exceeds the maximum depth of {max_depth}"))
                continue
            try:
                child = ProjectManager().load(target)
            except Exception as exc:
                issues.append((reported_step, f"subflow project cannot be loaded: {exc}"))
                continue
            walk(child, target.parent, [*stack, target], depth + 1, reported_step)

    walk(project, Path(project_dir).resolve(), [root_json], 0, 0)
    # Avoid repeating the same nested dependency message for one root step.
    return list(dict.fromkeys(issues))


def mapping_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(target).strip(): str(source).strip()
        for target, source in value.items()
        if str(target).strip() and str(source).strip()
    }
