from __future__ import annotations

import subprocess
import os
import shutil
import sys
import threading
import time
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable

from .image_matcher import find_image, screenshot_image, wait_for_image, wait_for_references
from .models import ActionStatus, ActionType, RpaAction, RpaProject, condition_summary
from .project_manager import ProjectManager
from .subflows import MAX_SUBFLOW_DEPTH, mapping_dict, resolve_subflow_project
from .control_flow import CONTROL_TYPES, IF_TYPES, LOOP_TYPES, METADATA_TYPES, parse_control_flow
from .utils import MissingPlaceholderError, foreground_elevation_mismatch, resolve_placeholders_strict
from .variables import (
    json_compatible_runtime_values, mask_sensitive_text, prepare_runtime_variables,
    sensitive_variable_names,
)
from .windowing import (
    WindowResolver, WindowTargetError, describe_window_target, normalize_window_target,
)
from .native_utilities import (
    CommandTimeoutError, command_arguments, copy_path, delete_path, move_path, process_window,
    read_clipboard_text, run_command_interruptible, show_notification,
    wait_for_process, write_clipboard_text,
)
from .builtin_tools import create_builtin_registry
from .execution import (
    COMPLETED_UNVERIFIED, COMPLETED_VERIFIED, FAILED, RECOVERED,
    REQUIRES_ATTENTION, STOPPED_BY_USER, ExecutionContext,
)
from .tools import ToolRegistry
from .verification import VerificationEngine, VerificationResult

pyautogui = None


class StopReplay(Exception):
    pass


class ReplayActionError(RuntimeError):
    def __init__(self, index: int, action: RpaAction, cause: Exception) -> None:
        self.index = index
        self.action = action
        self.cause = cause
        super().__init__(f"Step {index + 1} {action.summary()}: {cause}")


class ReplayRunner:
    def __init__(
        self,
        project: RpaProject,
        project_dir: Path,
        log: Callable[[str], None],
        excluded_regions: list[tuple[int, int, int, int]] | None = None,
        evidence_dir: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        verification_engine: VerificationEngine | None = None,
    ) -> None:
        self.project = project
        self.project_dir = Path(project_dir)
        self._log_sink = log
        self._stop_event = threading.Event()
        self.runtime_variables, _ = prepare_runtime_variables(project, validate_paths=False)
        self.log = self._safe_log
        self.excluded_regions = list(excluded_regions or [])
        self.total_attempts = 0
        self.current_index: int | None = None
        self.had_continued_failures = False
        self.first_failed_index: int | None = None
        self.first_failure_error: str | None = None
        self._step_deadline: float | None = None
        self._external_deadline: float | None = None
        self._best_image_confidence = 0.0
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.step_results: list[dict[str, Any]] = []
        self.selected_window_target: dict[str, Any] | None = None
        self.window_resolver: WindowResolver | None = None
        self._last_window_result: dict[str, Any] | None = None
        self._last_image_result: dict[str, Any] | None = None
        self._last_subflow_result: dict[str, Any] | None = None
        self._last_utility_result: dict[str, Any] | None = None
        self._active_subrunner: ReplayRunner | None = None
        self.subflow_stack: list[Path] = [(self.project_dir / "project.json").resolve()]
        self._debug_condition = threading.Condition()
        self._debug_command: tuple[str, int | None] | None = None
        self._debug_pause_next = False
        self._debug_paused_index: int | None = None
        self._debug_events: dict[int, list[dict[str, Any]]] = {}
        self.tool_registry = tool_registry or create_builtin_registry()
        self.verification_engine = verification_engine or VerificationEngine()
        self.execution_context = ExecutionContext(
            project=self.project,
            project_dir=self.project_dir,
            variables=self.runtime_variables,
            log=self.log,
            flow_metadata={"flow_id": self.project.project.id, "flow_name": self.project.project.name},
            helpers={
                "get_gui": get_pyautogui,
                "click_image": self._click_image,
                "sleep": self.sleep_checked,
                "check_stop": self._check_stop_for_code,
                "set_last_click": self._set_last_click,
                "store_output": self._store_output,
                "window_action": self._run_window_action,
                "python": self.run_python_code,
                "variable_action": self._run_variable_action,
                "subflow": self._run_subflow,
                "native_utility": self._run_native_utility,
                "window_titles": lambda: get_pyautogui().getAllTitles(),
            },
        )
        self.final_status = COMPLETED_UNVERIFIED
        self.completion_result: dict[str, Any] | None = None
        self.recovered = False
        self.requires_attention = False
        self.fallback_count = 0
        self.user_interventions: list[dict[str, Any]] = []
        self._attention_condition = threading.Condition()
        self._attention_callback: Callable[[dict[str, Any]], None] | None = None
        self._attention_decision: str | None = None

    def _safe_log(self, message: str) -> None:
        names = sensitive_variable_names(self.project)
        secrets = [
            self.runtime_variables[name] for name in names
            if name in self.runtime_variables and self.runtime_variables[name] not in (None, "")
        ]
        self._log_sink(mask_sensitive_text(message, secrets))

    def request_stop(self) -> None:
        self._stop_event.set()
        self.final_status = STOPPED_BY_USER
        if self._active_subrunner is not None:
            self._active_subrunner.request_stop()
        with self._debug_condition:
            self._debug_condition.notify_all()
        with self._attention_condition:
            self._attention_condition.notify_all()

    def resume_debug(self) -> None:
        self._send_debug_command("resume")

    def step_over_debug(self) -> None:
        self._send_debug_command("step")

    def skip_debug_step(self) -> None:
        self._send_debug_command("skip")

    def restart_debug_from(self, index: int) -> None:
        self._send_debug_command("restart", int(index))

    def update_debug_variables(self, values: dict[str, Any]) -> None:
        with self._debug_condition:
            if self._debug_paused_index is not None:
                changed = [name for name, value in values.items() if self.runtime_variables.get(name) != value]
                self.runtime_variables.update(values)
                if changed:
                    self.log(f"[Debug] Updated {len(changed)} variable value(s) while paused")
                    self._record_debug_event(
                        self._debug_paused_index, "variables_updated",
                        f"Updated {len(changed)} editable variable value(s)",
                    )

    def _send_debug_command(self, command: str, index: int | None = None) -> None:
        with self._debug_condition:
            if self._debug_paused_index is None:
                return
            self._debug_command = (command, index)
            self._debug_condition.notify_all()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def sleep_checked(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_requested():
                raise StopReplay()
            if self._step_timed_out():
                raise TimeoutError("step timed out")
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _step_timed_out(self) -> bool:
        now = time.monotonic()
        return (
            (self._step_deadline is not None and now >= self._step_deadline)
            or (self._external_deadline is not None and now >= self._external_deadline)
        )

    def _poll_cancelled(self) -> bool:
        return self.stop_requested() or self._step_timed_out()

    def run(
        self,
        action_callback: Callable[[int, str], None] | None = None,
        start_index: int = 0,
        end_index: int | None = None,
        include_start_delay: bool = True,
        respect_enabled: bool = True,
        retry_callback: Callable[[int, int, int, str], None] | None = None,
        control_callback: Callable[[int, str], None] | None = None,
        debug_callback: Callable[[int, str, dict[str, Any]], None] | None = None,
        enable_debug: bool = False,
    ) -> None:
        gui = get_pyautogui()
        gui.FAILSAFE = self.project.settings.pyautogui_failsafe
        total = len(self.project.actions)
        start_index = max(0, min(start_index, total))
        end_index = total - 1 if end_index is None else max(-1, min(end_index, total - 1))
        self.log("replay started")
        if include_start_delay:
            self.sleep_checked(self.project.settings.start_delay)
        mismatch = foreground_elevation_mismatch()
        if mismatch:
            raise PermissionError(mismatch[1])
        flow = parse_control_flow(self.project.actions)
        if flow.issues:
            issue = flow.issues[0]
            action = self.project.actions[max(0, issue.step_number - 1)]
            raise ReplayActionError(max(0, issue.step_number - 1), action, ValueError(issue.reason))
        loop_states: dict[int, dict[str, Any]] = {}
        index = start_index
        transitions = 0
        while index <= end_index:
            transitions += 1
            if transitions > 1_000_000:
                action = self.project.actions[index]
                raise ReplayActionError(index, action, RuntimeError("failure jumps exceeded the safety limit"))
            action = self.project.actions[index]
            self.current_index = index
            if self.stop_requested():
                raise StopReplay()
            if action.action in METADATA_TYPES:
                action.status = ActionStatus.COMPLETED.value
                if action_callback:
                    action_callback(index, "completed")
                self.log(f"[Step {index + 1}] {action.summary()}")
                index += 1
                continue
            if action.action in CONTROL_TYPES:
                if respect_enabled and not action.enabled:
                    raise ReplayActionError(index, action, ValueError("control steps cannot be disabled"))
                index = self._run_control_step(
                    index, end_index, flow, loop_states, action_callback, control_callback,
                )
                continue
            if respect_enabled and not action.enabled:
                record = self._start_step_record(action, index)
                self._finish_step_record(record, "Skipped")
                if action_callback:
                    action_callback(index, "skipped")
                index += 1
                continue
            debug_command, restart_index = (
                self._debug_gate(index, action, debug_callback)
                if enable_debug else ("execute", None)
            )
            if debug_command == "restart":
                debug_record = self._start_step_record(action, index)
                debug_record["debug_events"] = self._take_debug_events(index)
                self._finish_step_record(
                    debug_record, "Skipped", "Debugger restarted before this step executed",
                )
                if action_callback:
                    action_callback(index, "skipped")
                if restart_index is not None and start_index <= restart_index <= end_index:
                    self.log(f"[Debug] Restarting from Step {restart_index + 1}")
                    self._record_debug_event(restart_index, "restart", "Restarted from selected step")
                    loop_states.clear()
                    self._initialize_restart_loop_states(loop_states, flow, restart_index)
                    index = restart_index
                continue
            if debug_command == "skip":
                record = self._start_step_record(action, index)
                record["debug_events"] = self._take_debug_events(index)
                self._finish_step_record(record, "Skipped", "Skipped while paused in debugger")
                self.log(f"[Debug] Skipped Step {index + 1}: {action.summary()}")
                if action_callback:
                    action_callback(index, "skipped")
                index += 1
                continue
            if action_callback:
                action_callback(index, "running")
            record = self._start_step_record(action, index)
            variables_before = deepcopy(self.runtime_variables)
            debug_events = self._take_debug_events(index)
            if debug_events:
                record["debug_events"] = debug_events
            self._capture_step_screenshot(record, index, "before", bool(action.data.get("capture_before")))
            self.log(f"action started: {index + 1} {action.action}")
            # Click Image steps rely on continuous polling with their own search
            # timeout (see _click_image/wait_for_image) instead of a fixed
            # pre-wait, so they click as soon as the target appears rather than
            # always waiting out the recorded delay.
            if action.action not in (ActionType.CLICK_IMAGE.value, ActionType.DOUBLE_CLICK_IMAGE.value):
                self.sleep_checked(action.delay_before)
            try:
                self._last_window_result = None
                self._last_image_result = None
                self._last_subflow_result = None
                self._last_utility_result = None
                control_data = resolve_placeholders_strict(action.data, self.runtime_variables)
            except MissingPlaceholderError:
                control_data = action.data
            failure_settings = self._failure_settings(action, control_data)
            retry_count = max(0, self._safe_int(failure_settings.get("retry_count", 0), 0))
            retry_delay = max(0.0, self._safe_float(failure_settings.get("retry_delay_seconds", 1.0), 1.0))
            step_timeout = max(0.0, self._safe_float(control_data.get("step_timeout", 0.0), 0.0))
            max_attempts = retry_count + 1
            final_error: Exception | None = None
            self._best_image_confidence = 0.0
            for attempt in range(1, max_attempts + 1):
                self.total_attempts += 1
                record["attempts"] = attempt
                self.log(f"[Step {index + 1}] Attempt {attempt}/{max_attempts}")
                self.execution_context.log_event(
                    "tool_attempt", step=index + 1, action=action.action,
                    attempt=attempt, total=max_attempts,
                )
                self._step_deadline = time.monotonic() + step_timeout if step_timeout > 0 else None
                try:
                    self.run_action(
                        action, self.runtime_variables, index + 1,
                        allow_coordinate_fallback=(attempt == max_attempts),
                    )
                    self._verify_action(action, record)
                    if self._step_timed_out():
                        raise TimeoutError("step timed out")
                    final_error = None
                    break
                except MissingPlaceholderError as exc:
                    final_error = ValueError(f"missing variable '{exc.variable}'")
                except StopReplay:
                    if self._last_utility_result:
                        record["utility_result"] = dict(self._last_utility_result)
                    self._finish_step_record(record, "Stopped", "Stopped by user")
                    if action_callback:
                        action_callback(index, "stopped")
                    raise
                except Exception as exc:
                    final_error = self._friendly_runtime_error(exc)
                finally:
                    self._step_deadline = None
                if attempt < max_attempts:
                    reason = str(final_error)
                    record["retry_attempts"].append({"attempt": attempt + 1, "reason": reason})
                    self.log(
                        f"[Step {index + 1}] Retry {attempt + 1}/{max_attempts} in {retry_delay:.2f}s: {reason}"
                    )
                    self.execution_context.log_event(
                        "tool_retry", step=index + 1, next_attempt=attempt + 1,
                        total=max_attempts, delay_seconds=retry_delay, error=reason,
                    )
                    if retry_callback:
                        retry_callback(index, attempt + 1, max_attempts, reason)
                    self.sleep_checked(retry_delay)
            recovery_resolution: str | None = None
            if final_error is not None:
                final_error, recovery_resolution = self._recover_step_failure(
                    action, failure_settings, record, final_error, index,
                )
            if recovery_resolution == "skip":
                self._finish_step_record(record, "Skipped", "Skipped after explicit user decision")
                if action_callback:
                    action_callback(index, "skipped")
                index += 1
                continue
            if recovery_resolution == "stop":
                self._finish_step_record(record, "Stopped", str(final_error or "Stopped by user"))
                if action_callback:
                    action_callback(index, "stopped")
                self.final_status = STOPPED_BY_USER
                raise StopReplay()
            if final_error is not None:
                if self._last_window_result:
                    record["window_result"] = dict(self._last_window_result)
                if self._last_image_result:
                    record["image_match"] = dict(self._last_image_result)
                if self._last_subflow_result:
                    record["subflow"] = dict(self._last_subflow_result)
                if self._last_utility_result:
                    record["utility_result"] = dict(self._last_utility_result)
                if action_callback:
                    action_callback(index, "failed")
                failure_message = str(final_error)
                screenshot_path = self._capture_failure_screenshot(action, index)
                record["error"] = failure_message
                if screenshot_path:
                    record["screenshots"]["failure"] = screenshot_path
                self._finish_step_record(record, "Failed", failure_message)
                if screenshot_path:
                    failure_message = f"{failure_message} (failure screenshot: {screenshot_path})"
                failure_action = str(failure_settings.get("failure_action", "stop")).strip().lower()
                if not bool(failure_settings.get("stop_flow", True)) and failure_action == "stop":
                    failure_action = "continue"
                if failure_action in {"continue", "jump"}:
                    self.had_continued_failures = True
                    if self.first_failed_index is None:
                        self.first_failed_index = index
                        self.first_failure_error = failure_message
                    self.log(f"[Step {index + 1}] Final failure: {failure_message}")
                    if failure_action == "jump":
                        jump_step = self._safe_int(failure_settings.get("failure_jump_step", 0), 0)
                        jump_index = jump_step - 1
                        if not start_index <= jump_index <= end_index:
                            raise ReplayActionError(
                                index, action, ValueError(f"jump target Step {jump_step} is outside this run range")
                            )
                        self.log(f"[Step {index + 1}] Jumping to Step {jump_step}")
                        index = jump_index
                        continue
                    index += 1
                    continue
                self.final_status = FAILED
                raise ReplayActionError(index, action, RuntimeError(failure_message))
            if action_callback:
                action_callback(index, "completed")
            if self._last_window_result:
                record["window_result"] = dict(self._last_window_result)
            if self._last_image_result:
                record["image_match"] = dict(self._last_image_result)
            if self._last_subflow_result:
                record["subflow"] = dict(self._last_subflow_result)
            if self._last_utility_result:
                record["utility_result"] = dict(self._last_utility_result)
            self._capture_step_screenshot(record, index, "after", bool(action.data.get("capture_after")))
            self._finish_step_record(record, "Recovered" if recovery_resolution == "recovered" else "Success")
            self._log_variable_changes(variables_before, self.runtime_variables)
            self.log(f"action completed: {index + 1} {action.action}")
            index += 1
        full_flow = start_index == 0 and end_index == total - 1
        if self.had_continued_failures:
            self.final_status = FAILED
        elif self.requires_attention:
            self.final_status = REQUIRES_ATTENTION
        elif full_flow and self.project.success_when:
            passed, results = self.verification_engine.verify_completion(
                resolve_placeholders_strict(self.project.success_when, self.runtime_variables),
                self.execution_context,
            )
            self.completion_result = {
                "passed": passed,
                "mode": str(self.project.success_when.get("mode") or "all"),
                "conditions": [result.to_dict() for result in results],
            }
            self.log(
                f"Flow completion verification {'passed' if passed else 'failed'}: "
                f"mode={self.completion_result['mode']} conditions={len(results)}"
            )
            if not passed:
                self.final_status = FAILED
                if total:
                    raise ReplayActionError(
                        total - 1, self.project.actions[-1], RuntimeError("flow completion criteria were not met"),
                    )
                raise RuntimeError("flow completion criteria were not met")
            self.final_status = RECOVERED if self.recovered else COMPLETED_VERIFIED
        else:
            self.completion_result = None
            self.final_status = RECOVERED if self.recovered else COMPLETED_UNVERIFIED
        if self.project.settings.persist_variable_values:
            candidates = {
                name: self.runtime_variables[name]
                for name in (set(self.project.variables) | set(self.project.variable_definitions))
                if name in self.runtime_variables
            }
            persisted, warnings = json_compatible_runtime_values(candidates)
            self.project.persisted_variable_values = persisted
            for warning in warnings:
                self.log(f"Variable persistence warning: {warning}")
        self.log("replay completed")

    def _debug_gate(
        self, index: int, action: RpaAction,
        callback: Callable[[int, str, dict[str, Any]], None] | None,
    ) -> tuple[str, int | None]:
        reason = "step" if self._debug_pause_next else "breakpoint"
        should_pause = self._debug_pause_next or bool(action.breakpoint)
        if not should_pause:
            return "execute", None
        self._debug_pause_next = False
        message = "Step Over pause" if reason == "step" else "Breakpoint reached"
        self.log(f"[Debug] {message} before Step {index + 1}: {action.summary()}")
        self._record_debug_event(index, "pause", reason)
        with self._debug_condition:
            self._debug_paused_index = index
            self._debug_command = None
        if callback:
            callback(index, reason, dict(self.runtime_variables))
        with self._debug_condition:
            while self._debug_command is None and not self.stop_requested():
                self._debug_condition.wait(timeout=0.1)
            if self.stop_requested():
                self._debug_paused_index = None
                record = self._start_step_record(action, index)
                record["debug_events"] = self._take_debug_events(index)
                self._finish_step_record(record, "Stopped", "Stopped while paused at breakpoint")
                self.log(f"[Debug] Stopped while paused before Step {index + 1}")
                raise StopReplay()
            command, target = self._debug_command
            self._debug_command = None
            self._debug_paused_index = None
        if command == "resume":
            self.log(f"[Debug] Resumed at Step {index + 1}")
            self._record_debug_event(index, "resume", "Continued to next breakpoint")
            return "execute", None
        if command == "step":
            self.log(f"[Debug] Executing Step {index + 1} and pausing at the next executable step")
            self._record_debug_event(index, "step_over", "Execute next step")
            self._debug_pause_next = True
            return "execute", None
        if command == "skip":
            self._record_debug_event(index, "skip", "Skipped current step")
            self._debug_pause_next = True
            return "skip", None
        if command == "restart":
            self._debug_pause_next = True
            return "restart", target
        return "execute", None

    def _record_debug_event(self, index: int, event: str, detail: str) -> None:
        self._debug_events.setdefault(index, []).append({
            "event": event,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
        })

    def _take_debug_events(self, index: int) -> list[dict[str, Any]]:
        return self._debug_events.pop(index, [])

    def _initialize_restart_loop_states(self, loop_states: dict[int, dict[str, Any]], flow, index: int) -> None:
        """Treat a restart inside a loop body as the loop's first active iteration."""
        for start in flow.enclosing_loops.get(index, []):
            action = self.project.actions[start]
            data = resolve_placeholders_strict(action.data, self.runtime_variables)
            state: dict[str, Any] = {"iteration": 1, "type": action.action}
            if action.action == ActionType.REPEAT_COUNT.value:
                state["limit"] = max(0, self._safe_int(data.get("count", 1), 1))
            else:
                state["limit"] = max(1, self._safe_int(data.get("max_iterations", 1000), 1000))
                state["data"] = data
            loop_states[start] = state

    def _run_control_step(
        self,
        index: int,
        end_index: int,
        flow,
        loop_states: dict[int, dict[str, Any]],
        action_callback: Callable[[int, str], None] | None,
        control_callback: Callable[[int, str], None] | None,
    ) -> int:
        action = self.project.actions[index]
        if self.stop_requested():
            raise StopReplay()
        if action_callback:
            action_callback(index, "running")
        record = self._start_step_record(action, index)
        record["attempts"] = 1
        self.total_attempts += 1
        try:
            data = resolve_placeholders_strict(action.data, self.runtime_variables)
            kind = action.action
            result: dict[str, Any] = {}
            next_index = index + 1
            if kind in IF_TYPES:
                condition_type = {
                    ActionType.IF_IMAGE_EXISTS.value: "image_exists",
                    ActionType.IF_IMAGE_NOT_EXISTS.value: "image_not_exists",
                    ActionType.IF_WINDOW_EXISTS.value: "window_exists",
                    ActionType.IF_PATH_EXISTS.value: "path_exists",
                    ActionType.IF_VARIABLE.value: "variable",
                }[kind]
                matched, detail = self._evaluate_condition(data, condition_type)
                else_index = flow.if_else.get(index)
                end_if = flow.group_ends[index]
                branch = "If" if matched else ("Else" if else_index is not None else "Skipped")
                message = f"Condition {condition_summary({**data, 'condition_type': condition_type})}: {matched} · branch={branch}"
                self._report_control(index, message, control_callback)
                result = {"kind": "condition", "evaluated": matched, "detail": detail, "branch": branch}
                if not matched:
                    skip_end = else_index if else_index is not None else end_if
                    self._record_skipped_steps(index + 1, skip_end - 1, "If condition was false", action_callback)
                    next_index = (else_index + 1) if else_index is not None else end_if + 1
            elif kind == ActionType.ELSE.value:
                start = flow.else_if[index]
                end_if = flow.group_ends[start]
                self._record_skipped_steps(index + 1, end_if - 1, "If branch was selected", action_callback)
                message = "Else branch skipped because the If condition was true"
                self._report_control(index, message, control_callback)
                result = {"kind": "else", "selected": False}
                next_index = end_if + 1
            elif kind == ActionType.END_IF.value:
                result = {"kind": "end_if"}
            elif kind in LOOP_TYPES:
                end_loop = flow.loop_end[index]
                state = loop_states.get(index)
                if state is None:
                    state = {"iteration": 1, "type": kind}
                    loop_states[index] = state
                if kind == ActionType.REPEAT_COUNT.value:
                    count = max(0, self._safe_int(data.get("count", 1), 1))
                    state["limit"] = count
                    if count == 0:
                        self._record_skipped_steps(index + 1, end_loop - 1, "Repeat count is zero", action_callback)
                        loop_states.pop(index, None)
                        next_index = end_loop + 1
                        message = "Loop skipped · 0 iterations"
                    else:
                        message = f"Loop iteration {state['iteration']}/{count}"
                else:
                    state["limit"] = max(1, self._safe_int(data.get("max_iterations", 1000), 1000))
                    state["data"] = data
                    message = f"Repeat Until iteration {state['iteration']}/{state['limit']}"
                self._report_control(index, message, control_callback)
                result = {
                    "kind": "loop_start", "loop_type": kind,
                    "iteration": state["iteration"], "limit": state["limit"],
                }
            elif kind == ActionType.END_LOOP.value:
                start = flow.end_loop_start[index]
                state = loop_states.get(start)
                if state is None:
                    raise ValueError("loop state is missing")
                if state["type"] == ActionType.REPEAT_COUNT.value:
                    if state["iteration"] < state["limit"]:
                        state["iteration"] += 1
                        message = f"Loop iteration {state['iteration']}/{state['limit']}"
                        self._report_control(index, message, control_callback)
                        result = {"kind": "loop_end", "continue": True, "iteration": state["iteration"], "limit": state["limit"]}
                        next_index = start + 1
                    else:
                        message = f"Loop completed · {state['iteration']} iterations"
                        self._report_control(index, message, control_callback)
                        result = {"kind": "loop_end", "continue": False, "iterations": state["iteration"]}
                        loop_states.pop(start, None)
                else:
                    matched, detail = self._evaluate_condition(state["data"], str(state["data"].get("condition_type", "variable")))
                    if matched:
                        message = f"Repeat Until condition true · completed after {state['iteration']} iterations"
                        result = {"kind": "loop_end", "condition": True, "detail": detail, "iterations": state["iteration"]}
                        loop_states.pop(start, None)
                    elif state["iteration"] >= state["limit"]:
                        message = f"Repeat Until safety limit reached after {state['iteration']} iterations"
                        self._report_control(index, message, control_callback)
                        raise RuntimeError(message)
                    else:
                        state["iteration"] += 1
                        delay = max(0.0, self._safe_float(state["data"].get("iteration_delay", 0.0), 0.0))
                        if delay:
                            self.sleep_checked(delay)
                        message = f"Repeat Until condition false · iteration {state['iteration']}/{state['limit']}"
                        result = {"kind": "loop_end", "condition": False, "detail": detail, "iteration": state["iteration"]}
                        next_index = start + 1
                    self._report_control(index, message, control_callback)
            elif kind == ActionType.BREAK_LOOP.value:
                loops = flow.enclosing_loops.get(index, [])
                if not loops:
                    raise ValueError("Break Loop is not inside a loop")
                start = loops[-1]
                end_loop = flow.loop_end[start]
                for nested_start in [key for key in loop_states if key >= start]:
                    loop_states.pop(nested_start, None)
                self._record_skipped_steps(index + 1, end_loop - 1, "Break Loop selected", action_callback)
                message = f"Break Loop · leaving loop at Step {start + 1}"
                self._report_control(index, message, control_callback)
                result = {"kind": "break", "loop_step": start + 1}
                next_index = end_loop + 1
            record["control_result"] = result
            self._finish_step_record(record, "Success")
            if action_callback:
                action_callback(index, "completed")
            return next_index
        except StopReplay:
            raise
        except Exception as exc:
            record["control_result"] = {"kind": "control_error"}
            self._finish_step_record(record, "Failed", str(exc))
            if action_callback:
                action_callback(index, "failed")
            raise ReplayActionError(index, action, exc) from exc

    def _evaluate_condition(self, data: dict[str, Any], condition_type: str) -> tuple[bool, str]:
        if self.stop_requested():
            raise StopReplay()
        if condition_type in {"image_exists", "image_not_exists"}:
            image = self.project_dir / str(data.get("image", ""))
            match = find_image(image, float(data.get("confidence", self.project.settings.default_confidence)), self.excluded_regions)
            found = bool(match.found)
            result = not found if condition_type == "image_not_exists" else found
            return result, f"found={found}, confidence={float(getattr(match, 'confidence', 0.0)):.3f}"
        if condition_type == "window_exists":
            wanted = str(data.get("window_title", ""))
            case_sensitive = bool(data.get("case_sensitive", False))
            titles = get_pyautogui().getAllTitles() if hasattr(get_pyautogui(), "getAllTitles") else []
            compare = wanted if case_sensitive else wanted.casefold()
            matched_title = next((title for title in titles if compare in (title if case_sensitive else title.casefold())), None)
            return matched_title is not None, f"matched={matched_title or 'none'}"
        if condition_type == "path_exists":
            path = Path(str(data.get("path", ""))).expanduser()
            if not path.is_absolute():
                path = self.project_dir / path
            path_type = str(data.get("path_type", "either"))
            result = path.is_file() if path_type == "file" else path.is_dir() if path_type == "folder" else path.exists()
            return result, f"path={path}, type={path_type}"
        if condition_type == "variable":
            name = str(data.get("variable", ""))
            actual = self.runtime_variables.get(name)
            operator = str(data.get("operator", "equals"))
            expected = data.get("value", "")
            case_sensitive = bool(data.get("case_sensitive", False))
            actual_text, expected_text = str(actual if actual is not None else ""), str(expected)
            if not case_sensitive:
                actual_text, expected_text = actual_text.casefold(), expected_text.casefold()
            if operator == "is_empty":
                result = actual is None or str(actual).strip() == ""
            elif operator == "contains":
                result = expected_text in actual_text
            else:
                result = actual_text == expected_text
            return result, f"variable={name}, operator={operator}, result={result}"
        raise ValueError(f"unsupported condition type: {condition_type}")

    def _report_control(
        self, index: int, message: str, callback: Callable[[int, str], None] | None,
    ) -> None:
        self.log(f"[Step {index + 1}] {message}")
        if callback:
            callback(index, message)

    def _record_skipped_steps(
        self, start: int, end: int, reason: str,
        action_callback: Callable[[int, str], None] | None,
    ) -> None:
        for skipped_index in range(max(0, start), min(end, len(self.project.actions) - 1) + 1):
            skipped_action = self.project.actions[skipped_index]
            record = self._start_step_record(skipped_action, skipped_index)
            record["control_result"] = {"kind": "branch_skip", "reason": reason}
            self._finish_step_record(record, "Skipped")
            if action_callback:
                action_callback(skipped_index, "skipped")

    def run_action(
        self,
        action: RpaAction,
        variables: dict[str, Any] | None = None,
        step_number: int = 1,
        allow_coordinate_fallback: bool = True,
    ) -> None:
        variables = self.runtime_variables if variables is None else variables
        data = resolve_placeholders_strict(action.data, variables)
        self.execution_context.variables = variables
        self.execution_context.current_step = step_number
        self.execution_context.current_action = action
        self.execution_context.execution_state["allow_coordinate_fallback"] = allow_coordinate_fallback
        try:
            self.tool_registry.execute(action.action, data, self.execution_context)
        except KeyError as exc:
            raise ValueError(f"Unsupported action: {action.action}") from exc

    def _get_window_resolver(self) -> WindowResolver:
        if self.window_resolver is None:
            self.window_resolver = WindowResolver(sleep=self.sleep_checked)
        return self.window_resolver

    def _project_path(self, value: Any) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        return path if path.is_absolute() else self.project_dir / path

    def _utility_cancelled(self) -> bool:
        if self.stop_requested():
            raise StopReplay()
        return self._step_timed_out()

    def _run_native_utility(
        self, action: RpaAction, data: dict[str, Any], variables: dict[str, Any],
    ) -> None:
        started = time.monotonic()
        kind = action.action
        result: dict[str, Any]
        if kind == ActionType.LAUNCH_APPLICATION.value:
            raw_executable = str(data.get("path", ""))
            candidate = self._project_path(raw_executable)
            executable = str(candidate) if candidate.is_file() else (shutil.which(raw_executable) or str(candidate))
            arguments = command_arguments(data.get("arguments", ""))
            working = str(self._project_path(data["working_directory"])) if data.get("working_directory") else None
            try:
                process = subprocess.Popen([executable, *arguments], cwd=working or None, shell=False)
            except OSError as exc:
                raise RuntimeError(f"could not launch application: {exc}") from exc
            result = {"pid": process.pid, "path": executable, "duration_seconds": time.monotonic() - started}
            self._store_output(data, variables, process.pid)
        elif kind == ActionType.WAIT_PROCESS.value:
            result = wait_for_process(
                str(data.get("process_name", "")), float(data.get("timeout", 30.0)),
                float(data.get("retry_interval", 0.25)), self._utility_cancelled,
            )
            self._store_output(data, variables, result.get("pid"))
        elif kind in {ActionType.ACTIVATE_PROCESS.value, ActionType.CLOSE_PROCESS.value}:
            result = process_window(
                str(data.get("process_name", "")), close=kind == ActionType.CLOSE_PROCESS.value,
            )
        elif kind == ActionType.READ_CLIPBOARD.value:
            text = read_clipboard_text()
            self._store_output(data, variables, text)
            result = {"characters": len(text), "value": "[PROTECTED]" if data.get("sensitive") else text}
        elif kind == ActionType.WRITE_CLIPBOARD.value:
            text = str(data.get("text", ""))
            write_clipboard_text(text)
            variables["CLIPBOARD_TEXT"] = text
            result = {"characters": len(text), "value": "[PROTECTED]" if data.get("sensitive") else text}
        elif kind in {ActionType.COPY_PATH.value, ActionType.MOVE_PATH.value, ActionType.RENAME_PATH.value}:
            source = self._project_path(data.get("source", ""))
            destination = self._project_path(data.get("destination", ""))
            if kind == ActionType.COPY_PATH.value:
                completed = copy_path(source, destination)
            else:
                completed = move_path(source, destination)
            result = {"source": str(source), "destination": str(completed), "operation": kind}
            self._store_output(data, variables, str(completed))
        elif kind == ActionType.DELETE_PATH.value:
            target = self._project_path(data.get("path", ""))
            delete_path(target)
            result = {"path": str(target), "deleted": True}
        elif kind == ActionType.WAIT_PATH.value:
            target = self._project_path(data.get("path", ""))
            path_type = str(data.get("path_type", "either"))
            deadline = time.monotonic() + float(data.get("timeout", 30.0))
            while True:
                exists = target.is_file() if path_type == "file" else target.is_dir() if path_type == "folder" else target.exists()
                if exists:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{path_type} did not appear after {float(data.get('timeout', 30.0)):.1f}s: {target}")
                self.sleep_checked(float(data.get("retry_interval", 0.25)))
            result = {"path": str(target), "path_type": path_type}
            self._store_output(data, variables, str(target))
        elif kind in {ActionType.RUN_POWERSHELL.value, ActionType.RUN_PYTHON_SCRIPT.value}:
            if kind == ActionType.RUN_POWERSHELL.value:
                executable = shutil.which("powershell.exe") or shutil.which("powershell")
                if not executable:
                    raise FileNotFoundError("PowerShell executable was not found")
                command = [executable, "-NoProfile", "-NonInteractive", "-Command", str(data.get("command", ""))]
            else:
                current = Path(sys.executable)
                executable = str(current) if current.stem.casefold().startswith("python") else (shutil.which("python.exe") or shutil.which("python"))
                if not executable:
                    raise FileNotFoundError("Python interpreter was not found")
                command = [executable, str(self._project_path(data.get("path", ""))), *command_arguments(data.get("arguments", ""))]
            working = str(self._project_path(data["working_directory"])) if data.get("working_directory") else None
            try:
                result = run_command_interruptible(
                    command, working, float(data.get("timeout", 60.0)), self._utility_cancelled,
                )
            except CommandTimeoutError as exc:
                result = dict(exc.result)
                result["command"] = "[REDACTED]" if data.get("sensitive") else command
                self._last_utility_result = result
                raise
            except StopReplay as exc:
                result = dict(getattr(exc, "command_result", {}))
                result["command"] = "[REDACTED]" if data.get("sensitive") else command
                self._last_utility_result = result
                raise
            result["command"] = "[REDACTED]" if data.get("sensitive") else command
            self._store_output(data, variables, result.get("stdout", "").rstrip("\r\n"))
            if str(data.get("stderr_variable", "")).strip():
                variables[str(data["stderr_variable"]).strip()] = result.get("stderr", "").rstrip("\r\n")
            if str(data.get("exit_code_variable", "")).strip():
                variables[str(data["exit_code_variable"]).strip()] = result.get("exit_code")
            self._last_utility_result = dict(result)
            if result.get("exit_code") and not data.get("allow_nonzero_exit", False):
                raise RuntimeError(f"command exited with code {result['exit_code']}: {result.get('stderr', '').strip() or 'no error output'}")
        elif kind == ActionType.SHOW_NOTIFICATION.value:
            show_notification(str(data.get("title", "Python RPA Recorder")), str(data.get("message", "")))
            result = {"title": str(data.get("title", "")), "shown": True}
        else:
            raise ValueError(f"unsupported utility action: {kind}")
        result.setdefault("duration_seconds", max(0.0, time.monotonic() - started))
        self._last_utility_result = result
        if "exit_code" in result:
            detail = (
                f"exit={result['exit_code']}, stdout={len(result.get('stdout', ''))} chars, "
                f"stderr={len(result.get('stderr', ''))} chars, duration={result['duration_seconds']:.2f}s"
            )
        else:
            detail = f"duration={result['duration_seconds']:.2f}s"
        self.log(f"utility completed: {action.friendly_name()} ({detail})")

    def _run_subflow(
        self, action: RpaAction, data: dict[str, Any], variables: dict[str, Any], step_number: int,
    ) -> None:
        target = resolve_subflow_project(self.project_dir, str(data.get("project", "")))
        if not target.is_file():
            raise FileNotFoundError(f"subflow project is missing: {data.get('project') or target}")
        if target in self.subflow_stack:
            chain = " -> ".join(path.parent.name for path in [*self.subflow_stack, target])
            raise RuntimeError(f"circular subflow reference: {chain}")
        if len(self.subflow_stack) >= MAX_SUBFLOW_DEPTH + 1:
            raise RuntimeError(f"subflow nesting exceeds the maximum depth of {MAX_SUBFLOW_DEPTH}")
        child = ProjectManager().load(target)
        input_map = mapping_dict(data.get("input_mappings"))
        output_map = mapping_dict(data.get("output_mappings"))
        supplied: dict[str, Any] = {}
        for child_name, parent_name in input_map.items():
            if parent_name not in variables:
                raise ValueError(f"parent variable '{parent_name}' is not available for subflow input '{child_name}'")
            supplied[child_name] = variables[parent_name]
        child_variables, input_errors = prepare_runtime_variables(child, supplied, validate_paths=True)
        child_variables.update(supplied)
        if input_errors:
            raise ValueError(input_errors[0])
        flow_name = str(data.get("flow_name") or child.project.name or target.parent.name)
        self.log(f"[Subflow {flow_name}] started from Step {step_number}")
        evidence_dir = self.evidence_dir / "subflows" / f"step_{step_number}_{action.id[:8]}" if self.evidence_dir else None
        nested = ReplayRunner(child, target.parent, lambda message: self.log(f"[Subflow {flow_name}] {message}"), evidence_dir=evidence_dir)
        nested.runtime_variables = child_variables
        nested.execution_context.variables = child_variables
        nested._stop_event = self._stop_event
        nested._external_deadline = self._step_deadline or self._external_deadline
        nested.subflow_stack = [*self.subflow_stack, target]
        self._active_subrunner = nested
        try:
            nested.run(include_start_delay=False, enable_debug=False)
            if nested.had_continued_failures:
                raise RuntimeError(nested.first_failure_error or "subflow completed with failed steps")
            for child_name, parent_name in output_map.items():
                if child_name not in nested.runtime_variables:
                    raise ValueError(f"subflow output '{child_name}' was not produced")
                variables[parent_name] = nested.runtime_variables[child_name]
            self._last_subflow_result = {
                "flow_name": flow_name,
                "project": str(data.get("project", "")),
                "status": "Success",
                "attempts": nested.total_attempts,
                "outputs": {parent: nested.runtime_variables[child] for child, parent in output_map.items()},
                "step_results": nested.step_results,
            }
            self.log(f"[Subflow {flow_name}] completed; {len(nested.step_results)} step result(s)")
        except Exception as exc:
            self._last_subflow_result = {
                "flow_name": flow_name,
                "project": str(data.get("project", "")),
                "status": "Stopped" if isinstance(exc, StopReplay) else "Failed",
                "attempts": nested.total_attempts,
                "error": str(exc),
                "step_results": nested.step_results,
            }
            self.log(f"[Subflow {flow_name}] failed: {exc}")
            raise
        finally:
            self.total_attempts += nested.total_attempts
            self._active_subrunner = None

    def _window_target_for(self, data: dict[str, Any]) -> dict[str, Any]:
        target = normalize_window_target(data)
        has_criteria = any(target[key] for key in ("process_name", "window_title", "class_name"))
        if bool(data.get("use_selected_window", False)) and not has_criteria:
            if self.selected_window_target is None:
                raise WindowTargetError("no window has been selected by an earlier Select / Target Window step")
            target = dict(self.selected_window_target)
            # Per-step waiting settings may intentionally override the selected target.
            nested = data.get("window") if isinstance(data.get("window"), dict) else data
            for key in ("timeout", "retry_interval", "multiple_match"):
                if key in nested:
                    target[key] = nested[key]
        return target

    def _run_window_action(
        self, action: RpaAction, data: dict[str, Any], variables: dict[str, Any],
    ) -> None:
        resolver = self._get_window_resolver()
        target = self._window_target_for(data)
        kind = action.action
        try:
            window = resolver.resolve(target, self.stop_requested)
            if kind == ActionType.SELECT_WINDOW.value:
                self.selected_window_target = dict(target)
                operation = "selected"
            elif kind == ActionType.WAIT_WINDOW.value:
                operation = "found"
            elif kind == ActionType.ACTIVATE_WINDOW.value:
                window = resolver.activate(window)
                operation = "activated"
            elif kind in {
                ActionType.MAXIMIZE_WINDOW.value, ActionType.MINIMIZE_WINDOW.value,
                ActionType.RESTORE_WINDOW.value,
            }:
                state = {
                    ActionType.MAXIMIZE_WINDOW.value: "maximize",
                    ActionType.MINIMIZE_WINDOW.value: "minimize",
                    ActionType.RESTORE_WINDOW.value: "restore",
                }[kind]
                window = resolver.change_state(window, state)
                operation = state + "d" if state != "minimize" else "minimized"
            elif kind == ActionType.CLOSE_WINDOW.value:
                resolver.close(window)
                operation = "close requested"
            else:
                window = resolver.activate(window)
                x, y = self._window_relative_point(data, window)
                gui = get_pyautogui()
                if kind == ActionType.CLICK_WINDOW_RELATIVE.value:
                    self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
                    gui.click(x, y, button=str(data.get("button", "left")))
                    self._set_last_click(variables, x, y)
                    operation = f"clicked at ({x}, {y})"
                else:
                    gui.moveTo(x, y, duration=float(data.get("duration", 0.2)))
                    operation = f"moved to ({x}, {y})"
            self._last_window_result = {
                "operation": operation,
                "target": describe_window_target(target),
                "window": window.evidence(),
            }
            self.log(
                f"window {operation}: {window.title or window.process_name} "
                f"at ({window.left}, {window.top}, {window.width}x{window.height})"
            )
        except WindowTargetError as exc:
            if kind in {ActionType.CLICK_WINDOW_RELATIVE.value, ActionType.MOVE_WINDOW_RELATIVE.value} and bool(data.get("use_absolute_fallback", False)):
                x, y = int(data.get("fallback_x", 0)), int(data.get("fallback_y", 0))
                if kind == ActionType.CLICK_WINDOW_RELATIVE.value:
                    get_pyautogui().click(x, y, button=str(data.get("button", "left")))
                    self._set_last_click(variables, x, y)
                    operation = f"absolute fallback click at ({x}, {y})"
                else:
                    get_pyautogui().moveTo(x, y, duration=float(data.get("duration", 0.2)))
                    operation = f"absolute fallback move to ({x}, {y})"
                self._last_window_result = {
                    "operation": operation, "target": describe_window_target(target),
                    "fallback": True, "window_error": str(exc),
                }
                self.log(f"window target unavailable; {operation}: {exc}")
                return
            self._last_window_result = {
                "operation": "failed", "target": describe_window_target(target), "error": str(exc),
            }
            raise

    @staticmethod
    def _window_relative_point(data: dict[str, Any], window) -> tuple[int, int]:
        relative_x = float(data.get("relative_x", 0))
        relative_y = float(data.get("relative_y", 0))
        if bool(data.get("scale_with_window", False)):
            original_width = max(1.0, float(data.get("original_window_width", window.width) or window.width))
            original_height = max(1.0, float(data.get("original_window_height", window.height) or window.height))
            relative_x = relative_x * window.width / original_width
            relative_y = relative_y * window.height / original_height
        x = window.left + round(relative_x)
        y = window.top + round(relative_y)
        if not window.contains(x, y):
            raise WindowTargetError(
                f"relative point ({relative_x:.0f}, {relative_y:.0f}) is outside the current window bounds "
                f"({window.width}x{window.height})"
            )
        return x, y

    def run_python_code(self, action: RpaAction, data: dict[str, Any], variables: dict[str, Any], step_number: int) -> str:
        if self.stop_requested():
            raise StopReplay()
        if self._step_timed_out():
            raise TimeoutError("step timed out")
        code = str(data.get("code", ""))
        if not code.strip():
            raise ValueError(f"Step {step_number} {action.friendly_name()}: code is required")
        output = StringIO()
        env = {
            "variables": variables,
            "project_dir": self.project_dir,
            "action": action,
            "current_action": action,
            "logger": self.log,
            "check_stop": self._check_stop_for_code,
        }
        try:
            with redirect_stdout(output):
                exec(code, env)
        except Exception as exc:
            if data.get("continue_on_error", False):
                self.log(f"Step {step_number} {action.friendly_name()} exception ignored: {exc}")
            else:
                raise RuntimeError(f"Step {step_number} {action.friendly_name()}: {exc}") from exc
        if self.stop_requested():
            raise StopReplay()
        if self._step_timed_out():
            raise TimeoutError("step timed out")
        text = output.getvalue().strip()
        if text:
            self.log(text)
        output_name = str(data.get("output_variable", "")).strip()
        if output_name:
            variables[output_name] = env.get("result", output.getvalue().strip())
        return output.getvalue()

    def _check_stop_for_code(self) -> bool:
        if self.stop_requested():
            raise StopReplay()
        if self._step_timed_out():
            raise TimeoutError("step timed out")
        return False

    def _friendly_runtime_error(self, exc: Exception) -> Exception:
        if exc.__class__.__name__ == "FailSafeException":
            return RuntimeError(
                "PyAutoGUI safety stop was triggered because the mouse reached a screen corner. "
                "Move the pointer away from the corners and run again, or disable failsafe in Settings."
            )
        return exc

    def _click_image(self, action: RpaAction, data: dict, allow_coordinate_fallback: bool = True) -> None:
        image_path = self.project_dir / str(data.get("image", ""))
        required_confidence = float(data.get("confidence", self.project.settings.default_confidence))
        references = [image_path]
        for value in data.get("reference_images", []) if isinstance(data.get("reference_images"), list) else []:
            path = self.project_dir / str(value)
            if path not in references:
                references.append(path)
        advanced = bool(
            len(references) > 1 or data.get("grayscale") or data.get("search_region")
            or str(data.get("match_priority", "highest_confidence")) != "highest_confidence"
            or int(data.get("match_index", 1) or 1) != 1
        )
        warnings: list[str] = []
        if advanced:
            match, warnings = wait_for_references(
                references, required_confidence,
                float(data.get("timeout", self.project.settings.default_timeout)),
                self._poll_cancelled, excluded_regions=self.excluded_regions,
                grayscale=bool(data.get("grayscale", False)),
                search_region=data.get("search_region") if isinstance(data.get("search_region"), dict) else None,
                match_priority=str(data.get("match_priority", "highest_confidence")),
                match_index=int(data.get("match_index", 1) or 1),
            )
        else:
            match = wait_for_image(
                image_path,
                required_confidence,
                float(data.get("timeout", self.project.settings.default_timeout)),
                self._poll_cancelled,
                excluded_regions=self.excluded_regions,
            )
            if not getattr(match, "reference_image", ""):
                match.reference_image = str(image_path)
        for warning in warnings:
            self.log(f"image target warning: {warning}")
        if self.stop_requested():
            raise StopReplay()
        self._best_image_confidence = max(
            self._best_image_confidence, float(getattr(match, "confidence", 0.0) or 0.0),
        )
        if self._step_timed_out():
            raise TimeoutError(
                "step timed out while searching for the image target; "
                f"best confidence={self._best_image_confidence:.3f}, required={required_confidence:.3f}"
            )
        if match.found:
            reference = str(getattr(match, "reference_image", "") or image_path)
            self.log(
                f"image match: reference={Path(reference).name}, confidence={match.confidence:.3f} "
                f"(required {required_confidence:.3f}), "
                f"location=({match.x}, {match.y}), search time={match.duration:.2f}s"
            )
            x = match.x + int(data.get("click_offset_x", match.width / 2))
            y = match.y + int(data.get("click_offset_y", match.height / 2))
            self._last_image_result = {
                "matched": True, "reference_image": self._evidence_reference(reference),
                "reference_index": int(getattr(match, "reference_index", 0)),
                "confidence": float(match.confidence), "required_confidence": required_confidence,
                "search_duration": float(match.duration), "match_x": int(match.x), "match_y": int(match.y),
                "click_x": int(x), "click_y": int(y), "grayscale": bool(data.get("grayscale", False)),
                "search_region": data.get("search_region"),
            }
            clicks = 2 if action.action == ActionType.DOUBLE_CLICK_IMAGE.value else 1
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            get_pyautogui().click(x, y, clicks=clicks, button=str(data.get("button", "left")))
            self._set_last_click(self.runtime_variables, x, y)
            return
        self.log(
            f"image match: no match found, best confidence={getattr(match, 'confidence', 0.0):.3f} "
            f"(required {required_confidence:.3f}), search time={getattr(match, 'duration', 0.0):.2f}s"
        )
        self._last_image_result = {
            "matched": False, "reference_image": self._evidence_reference(str(getattr(match, "reference_image", "") or image_path)),
            "confidence": float(getattr(match, "confidence", 0.0)),
            "required_confidence": required_confidence, "search_duration": float(getattr(match, "duration", 0.0)),
            "grayscale": bool(data.get("grayscale", False)), "search_region": data.get("search_region"),
            "warnings": warnings,
        }
        if allow_coordinate_fallback and data.get("use_coordinate_fallback", True):
            self.sleep_checked(float(data.get("pre_click_pause", self.project.settings.pre_click_pause)))
            get_pyautogui().click(int(data.get("fallback_x", 0)), int(data.get("fallback_y", 0)), button=str(data.get("button", "left")))
            self._set_last_click(
                self.runtime_variables, int(data.get("fallback_x", 0)), int(data.get("fallback_y", 0)),
            )
            self._last_image_result["coordinate_fallback"] = True
            self._last_image_result["click_x"] = int(data.get("fallback_x", 0))
            self._last_image_result["click_y"] = int(data.get("fallback_y", 0))
            return
        raise FileNotFoundError(
            f"Image not found: {image_path}; best confidence={self._best_image_confidence:.3f}, "
            f"required={required_confidence:.3f}"
        )

    def _evidence_reference(self, value: str) -> str:
        try:
            return (Path(value).resolve().relative_to(self.project_dir.resolve())).as_posix()
        except ValueError:
            return str(value)

    def _capture_failure_screenshot(self, action: RpaAction, index: int) -> str | None:
        if self.evidence_dir is None and not action.data.get("capture_failure_screenshot", False):
            return None
        try:
            target_dir = (
                self.evidence_dir / "screenshots" if self.evidence_dir is not None
                else self.project_dir / "logs" / "failures"
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"step_{index + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
            screenshot_image().save(path, "PNG")
            self.log(f"[Step {index + 1}] Failure screenshot saved: {path}")
            return self._evidence_relative_path(path)
        except Exception as exc:
            self.log(f"[Step {index + 1}] Could not save failure screenshot: {exc}")
            return None

    def _start_step_record(self, action: RpaAction, index: int) -> dict[str, Any]:
        record = {
            "step_number": index + 1,
            "step_name": action.summary(),
            "action": action.action,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": None,
            "duration_seconds": None,
            "status": "Running",
            "attempts": 0,
            "retry_attempts": [],
            "fallback_executed": False,
            "verification_result": None,
            "user_intervention": [],
            "error": None,
            "screenshots": {},
        }
        self.step_results.append(record)
        return record

    def _finish_step_record(self, record: dict[str, Any], status: str, error: str | None = None) -> None:
        ended = datetime.now(timezone.utc)
        record["ended_at"] = ended.isoformat()
        try:
            started = datetime.fromisoformat(str(record["started_at"]))
            record["duration_seconds"] = max(0.0, (ended - started).total_seconds())
        except (TypeError, ValueError):
            record["duration_seconds"] = None
        record["status"] = status
        record["error"] = error

    def _capture_step_screenshot(
        self, record: dict[str, Any], index: int, kind: str, enabled: bool,
    ) -> str | None:
        if not enabled or self.evidence_dir is None:
            return None
        try:
            target_dir = self.evidence_dir / "screenshots"
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"step_{index + 1}_{kind}.png"
            screenshot_image().save(path, "PNG")
            relative = self._evidence_relative_path(path)
            record["screenshots"][kind] = relative
            self.log(f"[Step {index + 1}] {kind.title()} screenshot saved: {relative}")
            return relative
        except Exception as exc:
            self.log(f"[Step {index + 1}] Could not save {kind} screenshot: {exc}")
            return None

    def _evidence_relative_path(self, path: Path) -> str:
        if self.evidence_dir is not None:
            try:
                return path.relative_to(self.evidence_dir).as_posix()
            except ValueError:
                pass
        return str(path)

    def _store_output(self, data: dict[str, Any], variables: dict[str, Any], value: Any) -> None:
        name = str(data.get("output_variable", "")).strip()
        if name:
            variables[name] = value

    def _run_variable_action(self, kind: str, data: dict[str, Any], variables: dict[str, Any]) -> None:
        name = str(data.get("variable", "")).strip()
        if not name:
            raise ValueError("variable name is required")
        if kind == ActionType.SET_VARIABLE.value:
            variables[name] = deepcopy(data.get("value"))
        elif kind == ActionType.GET_VARIABLE.value:
            if name not in variables:
                raise KeyError(f"variable '{name}' does not exist")
            self.log(f"Variable read: {name} = {variables[name]!r}")
            output = str(data.get("output_variable", "")).strip()
            if output:
                variables[output] = deepcopy(variables[name])
        elif kind == ActionType.INCREMENT_VARIABLE.value:
            if name not in variables:
                raise KeyError(f"variable '{name}' does not exist")
            amount = data.get("amount", 1)
            if isinstance(variables[name], bool) or not isinstance(variables[name], (int, float)):
                raise TypeError(f"variable '{name}' must be numeric")
            variables[name] += amount
        elif kind == ActionType.APPEND_VARIABLE.value:
            if name not in variables:
                raise KeyError(f"variable '{name}' does not exist")
            if not isinstance(variables[name], list):
                raise TypeError(f"variable '{name}' must be a list")
            variables[name].append(deepcopy(data.get("value")))
        elif kind == ActionType.SET_OBJECT_PROPERTY.value:
            if name not in variables or not isinstance(variables[name], dict):
                raise TypeError(f"variable '{name}' must be an object")
            property_path = str(data.get("property", "")).strip()
            if not property_path:
                raise ValueError("object property is required")
            target = variables[name]
            parts = property_path.split(".")
            for part in parts[:-1]:
                child = target.get(part)
                if child is None:
                    child = {}
                    target[part] = child
                if not isinstance(child, dict):
                    raise TypeError(f"object property '{part}' is not an object")
                target = child
            target[parts[-1]] = deepcopy(data.get("value"))
        elif kind == ActionType.DELETE_VARIABLE.value:
            variables.pop(name, None)

    def _log_variable_changes(self, before: dict[str, Any], after: dict[str, Any]) -> None:
        sensitive = sensitive_variable_names(self.project)
        for name in sorted(set(before) | set(after)):
            if before.get(name, object()) == after.get(name, object()) and (name in before) == (name in after):
                continue
            if name not in after:
                self.log(f"Variable deleted: {name}")
                continue
            display = "********" if name in sensitive else repr(after[name])
            self.log(f"Variable updated: {name} = {display}")

    def _set_last_click(self, variables: dict[str, Any], x: int, y: int) -> None:
        variables["LAST_CLICK_X"] = x
        variables["LAST_CLICK_Y"] = y

    def _verify_action(self, action: RpaAction, record: dict[str, Any]) -> VerificationResult | None:
        if not action.expect:
            return None
        condition = resolve_placeholders_strict(action.expect, self.runtime_variables)
        self.log(f"[Step {self.current_index + 1 if self.current_index is not None else '?'}] Verification started: {condition.get('type')}")
        result = self.verification_engine.verify(condition, self.execution_context)
        record["verification_result"] = result.to_dict()
        self.log(
            f"[Step {self.current_index + 1 if self.current_index is not None else '?'}] "
            f"Verification {'passed' if result.passed else 'failed'}: {result.message} "
            f"(attempts={result.attempts}, duration={result.duration_seconds:.2f}s)"
        )
        self.execution_context.log_event(
            "verification_result", step=self.current_index + 1 if self.current_index is not None else None,
            type=result.condition_type, passed=result.passed, attempts=result.attempts,
            duration_seconds=round(result.duration_seconds, 4),
        )
        if not result.passed:
            raise RuntimeError(result.message)
        return result

    def _failure_settings(self, action: RpaAction, data: dict[str, Any]) -> dict[str, Any]:
        configured = resolve_placeholders_strict(action.on_failure or {}, self.runtime_variables)
        return {
            "retry_count": configured.get("retry_count", data.get("retry_count", 0)),
            "retry_delay_seconds": configured.get(
                "retry_delay_seconds", configured.get("retry_delay", data.get("retry_delay", 1.0)),
            ),
            "fallback_step": configured.get("fallback_step"),
            "ask_user": bool(configured.get("ask_user", False)),
            "stop_flow": bool(configured.get("stop_flow", True)),
            "failure_action": configured.get("failure_action", data.get("failure_action", "stop")),
            "failure_jump_step": configured.get("failure_jump_step", data.get("failure_jump_step", 0)),
        }

    @staticmethod
    def _fallback_action(raw: Any) -> RpaAction | None:
        if not isinstance(raw, dict) or not str(raw.get("action", "")).strip():
            return None
        payload = dict(raw)
        action_type = str(payload.pop("action"))
        data = payload.pop("data", None)
        return RpaAction(action_type, dict(data) if isinstance(data, dict) else payload)

    def set_attention_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._attention_callback = callback

    def submit_attention_decision(self, decision: str) -> None:
        normalized = str(decision).strip().casefold()
        if normalized not in {"retry", "skip", "stop"}:
            raise ValueError("attention decision must be retry, skip, or stop")
        with self._attention_condition:
            self._attention_decision = normalized
            self._attention_condition.notify_all()

    def _request_attention(self, payload: dict[str, Any]) -> str:
        if self._attention_callback is None:
            self.requires_attention = True
            self.log("Human escalation requested but no interactive handler is available")
            return "stop"
        with self._attention_condition:
            self._attention_decision = None
        self._attention_callback(payload)
        with self._attention_condition:
            while self._attention_decision is None and not self.stop_requested():
                self._attention_condition.wait(timeout=0.1)
            if self.stop_requested():
                return "stop"
            return self._attention_decision or "stop"

    def _recover_step_failure(
        self,
        action: RpaAction,
        settings: dict[str, Any],
        record: dict[str, Any],
        error: Exception,
        index: int,
    ) -> tuple[Exception | None, str | None]:
        fallback = self._fallback_action(settings.get("fallback_step"))
        if fallback is not None:
            self.fallback_count += 1
            record["fallback_executed"] = True
            record["fallback_action"] = fallback.to_dict()
            self.log(f"[Step {index + 1}] Fallback started: {fallback.action}")
            self.execution_context.log_event(
                "fallback_started", step=index + 1, action=fallback.action,
            )
            try:
                self.total_attempts += 1
                self.run_action(fallback, self.runtime_variables, index + 1, False)
                self._verify_action(action, record)
            except StopReplay:
                raise
            except Exception as fallback_error:
                error = self._friendly_runtime_error(fallback_error)
                record["fallback_error"] = str(error)
                self.log(f"[Step {index + 1}] Fallback failed: {error}")
            else:
                self.recovered = True
                self.log(f"[Step {index + 1}] Fallback recovered the step")
                return None, "recovered"

        if not settings.get("ask_user"):
            return error, None
        while True:
            screenshot_path = self._capture_failure_screenshot(action, index)
            if screenshot_path:
                record["screenshots"]["attention"] = screenshot_path
            payload = {
                "flow_name": self.project.project.name,
                "step_number": index + 1,
                "step_name": action.summary(),
                "error": str(error),
                "screenshot": screenshot_path,
            }
            decision = self._request_attention(payload)
            intervention = {"decision": decision, "error": str(error)}
            self.user_interventions.append({"step_number": index + 1, **intervention})
            record["user_intervention"].append(intervention)
            self.log(f"[Step {index + 1}] User decision: {decision}")
            self.execution_context.log_event(
                "human_decision", step=index + 1, decision=decision,
            )
            if decision == "skip":
                self.requires_attention = True
                return None, "skip"
            if decision == "stop":
                return error, "stop"
            try:
                self.total_attempts += 1
                record["attempts"] = int(record.get("attempts", 0)) + 1
                self.log(f"[Step {index + 1}] User-requested retry started")
                self.run_action(action, self.runtime_variables, index + 1, True)
                self._verify_action(action, record)
            except StopReplay:
                raise
            except Exception as retry_error:
                error = self._friendly_runtime_error(retry_error)
                self.log(f"[Step {index + 1}] User-requested retry failed: {error}")
                continue
            self.recovered = True
            return None, "recovered"

    def _safe_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def _safe_float(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def run_diagnostics(self) -> dict[str, Any]:
        verification_results = [
            record.get("verification_result") for record in self.step_results
            if isinstance(record.get("verification_result"), dict)
        ]
        error_messages = [
            str(record.get("error")) for record in self.step_results if record.get("error")
        ]
        retry_count = sum(max(0, int(record.get("attempts", 0)) - 1) for record in self.step_results)
        return {
            "retry_count": retry_count,
            "fallback_executed": self.fallback_count > 0,
            "verification_result": {
                "passed": all(item.get("passed", False) for item in verification_results),
                "steps": verification_results,
            } if verification_results else None,
            "completion_criteria_result": self.completion_result,
            "user_intervention": list(self.user_interventions),
            "error_messages": error_messages,
        }


def get_pyautogui():
    global pyautogui
    if pyautogui is None:
        import pyautogui as module
        pyautogui = module
    return pyautogui
