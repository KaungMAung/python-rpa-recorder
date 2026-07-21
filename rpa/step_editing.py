"""Pure, ID-aware step-list mutations used by the desktop editor."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from .control_flow import BLOCK_OPENERS, CONTROL_TYPES, parse_control_flow, range_structure_issues
from .models import ActionType, RpaAction


def jump_targets(actions: list[RpaAction]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for action in actions:
        settings = action.on_failure if isinstance(action.on_failure, dict) else action.data
        if str(settings.get("failure_action", "")).lower() != "jump":
            continue
        try:
            index = int(settings.get("failure_jump_step")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(actions):
            targets[action.id] = actions[index].id
    return targets


def restore_jump_targets(actions: list[RpaAction], targets: dict[str, str]) -> None:
    positions = {action.id: index + 1 for index, action in enumerate(actions)}
    for action in actions:
        target_id = targets.get(action.id)
        if target_id in positions:
            settings = action.on_failure if isinstance(action.on_failure, dict) else action.data
            settings["failure_jump_step"] = positions[target_id]


def validate_structure(actions: list[RpaAction]) -> str | None:
    issues = parse_control_flow(actions).issues
    return issues[0].reason if issues else None


def complete_contiguous_selection(
    actions: list[RpaAction], indices: list[int], expand_single_opener: bool = False,
) -> tuple[list[int], str | None]:
    selected = sorted(set(index for index in indices if 0 <= index < len(actions)))
    if not selected:
        return [], "Select one or more steps first."
    flow = parse_control_flow(actions)
    if expand_single_opener and len(selected) == 1 and actions[selected[0]].action in BLOCK_OPENERS and selected[0] in flow.group_ends:
        selected = list(range(selected[0], flow.group_ends[selected[0]] + 1))
    if selected != list(range(selected[0], selected[-1] + 1)):
        return [], "Select one continuous range for this command."
    range_issues = range_structure_issues(flow, selected[0], selected[-1])
    if range_issues:
        return [], range_issues[0].reason
    return selected, None


def reorder_steps(
    actions: list[RpaAction], indices: list[int], destination: int,
) -> tuple[list[RpaAction] | None, str | None]:
    selected = sorted(set(index for index in indices if 0 <= index < len(actions)))
    if not selected:
        return None, "Select one or more steps to move."
    targets = jump_targets(actions)
    moving = [actions[index] for index in selected]
    remaining = [action for index, action in enumerate(actions) if index not in set(selected)]
    insert_at = max(0, min(destination - sum(index < destination for index in selected), len(remaining)))
    prospective = remaining[:insert_at] + moving + remaining[insert_at:]
    error = validate_structure(prospective)
    if error:
        return None, error
    restore_jump_targets(prospective, targets)
    return prospective, None


def delete_steps(
    actions: list[RpaAction], indices: list[int], expand_single_opener: bool = True,
) -> tuple[list[RpaAction] | None, str | None]:
    selected = sorted(set(index for index in indices if 0 <= index < len(actions)))
    if not selected:
        return None, "Select one or more steps first."
    flow = parse_control_flow(actions)
    expanded = set(selected)
    if expand_single_opener:
        for index in selected:
            if actions[index].action in BLOCK_OPENERS and index in flow.group_ends:
                expanded.update(range(index, flow.group_ends[index] + 1))
    selected = sorted(expanded)
    targets = jump_targets(actions)
    deleted_ids = {actions[index].id for index in selected}
    external = [source for source, target in targets.items() if source not in deleted_ids and target in deleted_ids]
    if external:
        return None, "Another step jumps to this selection. Change its failure target before deleting."
    prospective = [action for index, action in enumerate(actions) if index not in set(selected)]
    error = validate_structure(prospective)
    if error:
        return None, error
    restore_jump_targets(prospective, targets)
    return prospective, None


def clipboard_payload(actions: list[RpaAction], indices: list[int]) -> tuple[dict[str, Any] | None, str | None]:
    selected = sorted(set(index for index in indices if 0 <= index < len(actions)))
    if not selected:
        return None, "Select one or more steps first."
    flow = parse_control_flow(actions)
    if len(selected) == 1 and actions[selected[0]].action in BLOCK_OPENERS and selected[0] in flow.group_ends:
        selected = list(range(selected[0], flow.group_ends[selected[0]] + 1))
    elif len(selected) == 1 and actions[selected[0]].action in CONTROL_TYPES:
        return None, "Select the opening If, Repeat, or Group row to copy the complete block."
    contiguous = selected == list(range(selected[0], selected[-1] + 1))
    if contiguous:
        issues = range_structure_issues(flow, selected[0], selected[-1])
        if issues:
            return None, issues[0].reason
    elif any(actions[index].action in CONTROL_TYPES for index in selected):
        return None, "Copy complete control/group blocks as one continuous range."
    targets = jump_targets(actions)
    return {
        "format": "python-rpa-recorder-steps",
        "version": 1,
        "actions": [
            {"action": deepcopy(actions[index].to_dict()), "jump_target_id": targets.get(actions[index].id)}
            for index in selected
        ],
    }, None


def paste_payload(
    actions: list[RpaAction], payload: dict[str, Any], insert_at: int,
) -> tuple[list[RpaAction] | None, list[int], str | None]:
    if payload.get("format") != "python-rpa-recorder-steps" or not isinstance(payload.get("actions"), list):
        return None, [], "The clipboard does not contain Python RPA Recorder steps."
    existing_targets = jump_targets(actions)
    old_to_new: dict[str, str] = {}
    group_ids: dict[str, str] = {}
    clones: list[RpaAction] = []
    entries = payload["actions"]
    try:
        for entry in entries:
            clone = RpaAction.from_dict(deepcopy(entry["action"]))
            old_id = clone.id
            clone.id = str(uuid4())
            old_to_new[old_id] = clone.id
            if clone.action == ActionType.GROUP_START.value:
                old_group = str(clone.data.get("group_id") or old_id)
                group_ids[old_group] = str(uuid4())
                clone.data["group_id"] = group_ids[old_group]
            elif clone.action == ActionType.GROUP_END.value:
                old_group = str(clone.data.get("group_id") or "")
                clone.data["group_id"] = group_ids.get(old_group, str(uuid4()))
            clones.append(clone)
    except (KeyError, TypeError, ValueError) as exc:
        return None, [], f"Clipboard step data is invalid: {exc}"
    insert_at = max(0, min(insert_at, len(actions)))
    prospective = actions[:insert_at] + clones + actions[insert_at:]
    error = validate_structure(prospective)
    if error:
        return None, [], error
    restore_jump_targets(prospective, existing_targets)
    positions = {action.id: index + 1 for index, action in enumerate(prospective)}
    for clone, entry in zip(clones, entries):
        target = entry.get("jump_target_id")
        target = old_to_new.get(str(target), str(target) if target else "")
        if target in positions:
            settings = clone.on_failure if isinstance(clone.on_failure, dict) else clone.data
            settings["failure_jump_step"] = positions[target]
        elif str((clone.on_failure or clone.data).get("failure_action", "")).lower() == "jump":
            return None, [], "A copied failure jump points to a step that is not available in this flow."
    return prospective, list(range(insert_at, insert_at + len(clones))), None
