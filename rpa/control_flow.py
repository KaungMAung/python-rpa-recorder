"""Shared structural parser for visual If/Else and Repeat blocks."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import ActionType, RpaAction

IF_TYPES = {
    ActionType.IF_IMAGE_EXISTS.value,
    ActionType.IF_IMAGE_NOT_EXISTS.value,
    ActionType.IF_WINDOW_EXISTS.value,
    ActionType.IF_PATH_EXISTS.value,
    ActionType.IF_VARIABLE.value,
}
LOOP_TYPES = {ActionType.REPEAT_COUNT.value, ActionType.REPEAT_UNTIL.value}
BLOCK_OPENERS = IF_TYPES | LOOP_TYPES
CONTROL_TYPES = BLOCK_OPENERS | {
    ActionType.ELSE.value, ActionType.END_IF.value, ActionType.END_LOOP.value,
    ActionType.BREAK_LOOP.value,
}


@dataclass(frozen=True)
class StructureIssue:
    step_number: int
    reason: str
    level: str = "Error"


@dataclass
class ControlFlowMap:
    depths: list[int]
    group_ends: dict[int, int] = field(default_factory=dict)
    if_else: dict[int, int] = field(default_factory=dict)
    else_if: dict[int, int] = field(default_factory=dict)
    end_if_start: dict[int, int] = field(default_factory=dict)
    loop_end: dict[int, int] = field(default_factory=dict)
    end_loop_start: dict[int, int] = field(default_factory=dict)
    enclosing_loops: dict[int, list[int]] = field(default_factory=dict)
    issues: list[StructureIssue] = field(default_factory=list)


def parse_control_flow(actions: list[RpaAction]) -> ControlFlowMap:
    result = ControlFlowMap(depths=[0] * len(actions))
    stack: list[dict] = []
    for index, action in enumerate(actions):
        kind = action.action
        is_closer = kind in {ActionType.ELSE.value, ActionType.END_IF.value, ActionType.END_LOOP.value}
        result.depths[index] = max(0, len(stack) - (1 if is_closer else 0))
        result.enclosing_loops[index] = [item["index"] for item in stack if item["kind"] == "loop"]
        if kind in IF_TYPES:
            stack.append({"kind": "if", "index": index, "else": None})
        elif kind in LOOP_TYPES:
            stack.append({"kind": "loop", "index": index})
        elif kind == ActionType.ELSE.value:
            if not stack or stack[-1]["kind"] != "if":
                result.issues.append(StructureIssue(index + 1, "Else must be inside an If block"))
            elif stack[-1]["else"] is not None:
                result.issues.append(StructureIssue(index + 1, "an If block can contain only one Else"))
            else:
                start = stack[-1]["index"]
                stack[-1]["else"] = index
                result.if_else[start] = index
                result.else_if[index] = start
        elif kind == ActionType.END_IF.value:
            if not stack or stack[-1]["kind"] != "if":
                result.issues.append(StructureIssue(index + 1, "End If has no matching If"))
            else:
                block = stack.pop()
                start = block["index"]
                result.group_ends[start] = index
                result.end_if_start[index] = start
                if block["else"] is not None:
                    result.group_ends[block["else"]] = index
        elif kind == ActionType.END_LOOP.value:
            if not stack or stack[-1]["kind"] != "loop":
                result.issues.append(StructureIssue(index + 1, "End Loop has no matching Repeat"))
            else:
                block = stack.pop()
                start = block["index"]
                result.group_ends[start] = index
                result.loop_end[start] = index
                result.end_loop_start[index] = start
        elif kind == ActionType.BREAK_LOOP.value and not any(item["kind"] == "loop" for item in stack):
            result.issues.append(StructureIssue(index + 1, "Break Loop must be inside a Repeat block"))
    for block in reversed(stack):
        label = "End If" if block["kind"] == "if" else "End Loop"
        result.issues.append(StructureIssue(block["index"] + 1, f"missing {label} for this block"))
    return result


def range_structure_issues(
    flow: ControlFlowMap, start_index: int, end_index: int,
) -> list[StructureIssue]:
    issues: list[StructureIssue] = []
    for opener, closer in flow.group_ends.items():
        opener_in = start_index <= opener <= end_index
        closer_in = start_index <= closer <= end_index
        if opener_in != closer_in:
            issues.append(StructureIssue(
                max(start_index, min(opener, end_index)) + 1,
                f"the selected run range cuts through the block from Step {opener + 1} to Step {closer + 1}",
            ))
    return issues
