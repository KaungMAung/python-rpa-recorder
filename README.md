# Python RPA Recorder

Python RPA Recorder is a Windows-first desktop recorder for simple PyAutoGUI workflows. It records global mouse and keyboard actions, stores projects as JSON plus screenshots, replays actions in a responsive PySide6 UI, and generates a normal editable Python script.

The main workflow is `Record -> Review -> Test -> Run`. The step list remains visible while a selected step is edited in the details panel.

## Architecture

- `app.py` starts the PySide6 application.
- `ui/` contains the main window, action table, editor, recorder toolbar, and dialogs.
- `rpa/models.py` defines project and action data.
- `rpa/project_manager.py` saves and opens folder-based projects.
- `rpa/recorder.py` uses `pynput` to capture global mouse and keyboard input.
- `rpa/runner.py` replays actions with `pyautogui` in a worker thread and owns the thread-safe breakpoint gate; UI commands wake its condition without running automation work on the UI thread.
- `rpa/tools.py` defines the common `RpaTool` contract and registry used to validate, execute, verify, and recover executable actions.
- `rpa/builtin_tools.py` registers the existing image, coordinate, keyboard, wait, Python, window, variable, subflow, application, and file implementations without changing their saved action names.
- `rpa/execution.py` provides the mutable per-run `ExecutionContext` shared by tools, including variables, flow metadata, current step, logs, screenshots, helpers, and execution state.
- `rpa/verification.py` evaluates step expectations and flow completion criteria through one polling engine.
- `rpa/control_flow.py` validates visual If, loop, and named-group boundaries.
- `rpa/step_editing.py` applies ID-aware reorder, delete, copy, and paste operations while preserving jump targets.
- `rpa/image_matcher.py` captures screenshots and locates images with OpenCV.
- `rpa/windowing.py` discovers, matches, activates, and controls native Windows application windows.
- `rpa/generator.py` creates `generated/generated_rpa.py`.
- `rpa/scheduler.py` stores schedules and persistent run history (`flows/schedules.json`).
- `rpa/windows_tasks.py` registers one Windows Task Scheduler task per saved schedule and builds the standalone runner command.
- `rpa/scheduled_runner.py` runs a scheduled flow without requiring the main recorder window to remain open.
- `rpa/desktop_lifecycle.py` performs deterministic Windows desktop preparation and recorder-only restoration.
- `ui/schedule_dialog.py` is the Schedule Flows page used to enable, adjust, and monitor those schedules.
- `ui/debug_variables_dialog.py` presents editable non-sensitive runtime values while a breakpoint is paused.

## Install

Use Python 3.11 or newer.

```powershell
cd D:\00_2026\python-rpa\simple-rpa\python-rpa-recorder
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run From Source

```powershell
.\.venv\Scripts\python.exe app.py
```

## Recording Workflow

1. Click `New` or open an existing project.
2. Click `Record` and select a project folder if prompted.
3. By default, the app hides, shows the Windows desktop, and displays a three-second countdown in the compact always-on-top toolbar. Input capture begins only after that countdown, so neither preparation nor the desktop shortcut becomes a step. Turn this off in Settings with **Show desktop before recording**.
4. Perform actions in other applications. Stopping or cancelling restores only Python RPA Recorder; the other desktop windows remain as they are.
5. Use `Pause`, `Resume`, `Stop`, or `Cancel` from the floating toolbar.
6. Review, rename, edit, enable, disable, test, delete, or reorder recorded steps. Use the step search box to narrow a flow, Ctrl/Cmd-click or Shift-click rows for bulk enable/disable and deletion, and Ctrl+Z / Ctrl+Y for undo/redo.

The recorder buffers printable typing into `type_text` actions, stores hotkeys as one action, preserves recorded runtime delays, and saves click screenshots under `screenshots/`.

## Replay Workflow

Click `Run`. Replay starts after the configured countdown. `Stop Run` requests a safe stop during waits and image polling. Click image actions search the screen first and use the original position when that option is enabled.

Right-click a step to test only that step, run from it, run until it, enable or disable it, insert another step, duplicate it, delete it, or move it. Replay and step tests run outside the Qt UI thread.

## Tool Architecture, Verification, and Recovery

Every executable action is resolved by action type through the central `ToolRegistry`. Existing flow action names and data remain unchanged; the built-in tools delegate to the recorder's established execution helpers for image matching, windows, variables, subflows, scripts, files, and native utilities. A tool implements the common `validate(inputs, context)`, `execute(inputs, context)`, `verify(result, context)`, and `recover(error, context)` contract. The runner supplies one shared `ExecutionContext`, so runtime variables and stop state remain consistent across attempts and later steps.

In **Step Details**, expand **Expected Result** to make a step successful only when its action and post-action condition both pass. Supported conditions are `image_visible`, `image_not_visible`, `file_exists`, `file_not_exists`, `variable_equals`, `variable_not_empty`, `window_title_contains`, and `process_running`. Timeout polling is interruptible by Stop Run. Steps without `expect` execute exactly as before.

```json
{
  "action": "click_image",
  "data": {"image": "screenshots/save.png"},
  "expect": {
    "type": "file_exists",
    "value": "${output_file}",
    "timeout_seconds": 15,
    "poll_interval_seconds": 0.5
  }
}
```

Expand **Failure Handling** to configure retry count/delay, one fallback action, optional user escalation, and the final stop/continue/jump behavior. Retries rerun only the failed step and preserve the run's mutable variables. A fallback executes once after automatic retries and the original expectation is checked again. When **Ask user** is enabled, the failure dialog identifies the flow and step, shows the error and latest available screenshot, and offers **Retry**, **Skip**, or **Stop**. A failure is never skipped without an explicit configured policy or user choice.

```json
{
  "on_failure": {
    "retry_count": 2,
    "retry_delay_seconds": 1,
    "fallback_step": {"action": "press_key", "key": "f8"},
    "ask_user": true,
    "stop_flow": true
  }
}
```

In **Project → Flow Settings**, enable **Completion Criteria** and choose whether all or any conditions must pass. Completion uses the same verification engine and `${variable}` references as step expectations.

```json
{
  "success_when": {
    "mode": "all",
    "conditions": [
      {"type": "file_exists", "value": "${output_file}"},
      {"type": "variable_not_empty", "value": "report_path"}
    ]
  }
}
```

Full runs finish as `COMPLETED_VERIFIED`, `COMPLETED_UNVERIFIED`, `RECOVERED`, `FAILED`, `STOPPED_BY_USER`, or `REQUIRES_ATTENTION`. Persistent schedule history and evidence summaries retain retry totals, fallback use, verification and completion results, user decisions, and errors. The Run Details **Diagnostics** tab presents these fields. Older history records load with safe defaults, and older flows without the new fields retain their previous behavior.

## Scheduling Flows

On Windows, every enabled schedule is registered as its own Windows Task Scheduler task under `\PythonRPARecorder\`, so the recorder does not need to remain open. Task names use the saved project flow ID plus schedule ID (`Flow_<flow-id>_<schedule-id>`), so flow renames cannot create duplicates. A flow may have several schedules; use **Add Schedule** to create another interval for the selected flow. Existing one-schedule-per-flow data remains compatible and receives stable flow/schedule identities automatically.

Open `Schedule Flows` (Execution toolbar group or menu, `⏱`) to see every saved flow and manage its automatic schedule. The header summarizes enabled, paused, disabled, and currently running flows. Search by flow name or filter by schedule/run status; a clear empty state explains when nothing matches.

Columns:

- **Flow** - the automation's name.
- **State** - color-coded `Enabled`, `Paused`, or `Disabled`.
- **Run every** - the interval (5 minutes up to 24 hours).
- **Last run** - a friendly relative time such as `2 min ago` (hover for the exact timestamp).
- **Duration** - how long the last run took.
- **Last status** - `Success`, `Failed`, `Running`, or a `Skipped (...)` reason.
- **Next run** - a countdown such as `in 12 min` (or `Paused`/`Disabled`; hover for the exact timestamp).
- **Windows task** - `Registered`, `Disabled`, `Running`, `Task missing`, or `Registration failed`.

Select a row to open its Details panel. The panel shows the latest run, duration, result, error, next run, and schedule interval. It also contains persistent run history with source, start/end times, duration, attempts, result, failed step, and error. Filter history by `Success`, `Failed`, `Skipped`, or `Running`. Select a history row and use **Run Details** (or double-click it) to inspect its execution report. Use **Configure Inputs…** to save the Runtime Input values used by unattended scheduled runs. Change the interval or enabled/paused state there without adding controls to every table row. The row's labeled **Actions** menu provides:

- **Run Now** - starts the standalone runner immediately without affecting its schedule or next run time.
- **Test Run** - launches the exact command stored in the Windows task, including the project path and schedule ID.
- **Repair / Register Task** - recreates or updates a missing/broken task and shows the exact registration error if Windows rejects it.
- **Pause / Resume** - temporarily stops automatic runs while keeping the interval configuration intact.
- **Enable / Disable** - fully turns the schedule on or off.
- **Details** - selects the row and opens full run information in the side panel (failure details also remain available on the Last status tooltip).
- **Delete Schedule** - removes that schedule and its corresponding Windows task. Other schedules for the same flow are unaffected.

The header shows the auto-refresh state and refreshes every 5 seconds so task state and countdowns stay current; use **Refresh** or `F5` for an immediate update. `Running` is shown only when Task Scheduler's explicit state field says the task is running; descriptive settings containing the word "running" are ignored. Click `Flow`, `Last run`, `Next run`, or `Last status` headers to sort (click again to reverse); column widths and the chosen sort order are remembered between openings. Row selection and all controls support keyboard navigation.

The table and Details panel now use a resizable splitter with a wider default Details area. The splitter position,
column widths, sort order, and Advanced section state are remembered. Long task names and errors wrap in Details and
expose their full value in a tooltip. Primary actions remain visible in a compact grid; Test Run, runtime inputs, and
Delete are under **More Actions**. Less-used privilege, timeout, input, and retention controls are under
**Advanced Schedule Settings**. Run history is larger, opens a run on double-click, and initially renders only the
newest 100 matching entries; **Load More** paginates large histories.

Enable, Disable, Pause, Resume, Delete, Repair/Register, Run Now, and Test Run show a confirmation naming the exact
flow and action, with Cancel as the safe default. Only Run Now and Pause/Resume can remember **Do not ask again**;
destructive and system-level actions always confirm.

Automatic refresh uses a non-overlapping background read for schedules and Windows tasks. Task status is cached for
30 seconds rather than queried once per schedule on every five-second tick; **Refresh** or `F5` forces a new status
pass. Existing rows update in place to preserve selection, sorting, and scroll position, and history is rendered only
for the selected flow. Stale background results are discarded after edits. Timing log entries cover dialog opening,
schedule loading, Windows task querying, history rendering, and table/details refresh.

Each Windows task runs only while the Windows user is logged on, starts as soon as practical after a missed start, and uses the Task Scheduler **IgnoreNew** policy so a second instance of the same schedule cannot overlap its active run. In Details, optionally set an execution timeout or enable **Run with highest privileges**. The recorder never stores a Windows password. If registration needs elevation, only the small task-registration helper requests UAC approval; the main application is not relaunched as administrator.

The registered action uses the existing standalone runner with this command shape:

```text
PythonRPARecorder.exe --project "<project.json path>" --schedule-id "<id>" --scheduled-run
```

When running from source, the equivalent command uses the current Python interpreter and `app.py`. Paths are passed as separate arguments and quoted in the Task Scheduler XML rather than stored as a shell command.

Scheduled runs use the same validation, evidence, history, runtime-input, retry, and desktop preparation behavior as manual Run. Windows deterministically minimizes normal top-level windows before execution (it does not use the toggling Win+D shortcut), the safely positioned floating **Stop Run** control remains available, and a recorder window that was visible before preparation is restored after success, failure, timeout, or stop. Other minimized applications remain minimized. Results are written directly to the existing run-history storage, so they appear when Schedule Flows is opened after the recorder has been closed. Desktop preparation and recorder restoration counts are written to the run evidence log for troubleshooting.

Schedules and run history are stored permanently in `flows/schedules.json` and persist across app restarts. The Windows task name, flow ID, registration status, and registration error are stored with each schedule. At startup, enabled schedules are reconciled idempotently using Task Scheduler's update (`/F`) behavior; disabled schedules have existing tasks disabled, obsolete legacy task names are removed, and missing tasks remain clearly reported. Windows uses Task Scheduler as the authoritative automatic runner, so the in-app polling scheduler is not also active and cannot produce duplicate runs. The internal polling scheduler remains the non-Windows fallback. Additional schedules are stored alongside the compatible primary flow schedule. History includes start/end time, duration, total step attempts, final status, failed/stopped step, and error. It keeps the latest 100 records per schedule by default; adjust the retention control in the Details panel from 10 to 1,000 records. Existing schedule files are migrated automatically from their previous latest-run fields.

## Step Details

Select a step to edit its friendly name and common settings on the right. Image steps show their ordered reference images, confidence slider, search area, and coordinate-fallback status without requiring a path to be typed. Timeout, grayscale mode, match priority, original coordinates, click offsets, retries, and failure handling remain under `Advanced Settings`. Target previews retain their original aspect ratio and point out missing screenshots. Deselecting a step does not hide the step list.

The lower Logs/Status and Validation area has a practical minimum height and remains resizable. Window geometry, splitter positions, table widths, the logs state, and the Advanced section state are stored with `QSettings`.

## Adding Steps

**Add Step** opens the Guided Flow Builder. Start with one of eleven everyday intentions: **Click something**, **Type
text**, **Open an application**, **Wait for something**, **Work with a window**, **Work with a file**, **Add a
condition**, **Repeat steps**, **Run another flow**, **Work with a variable**, or **Run a script or command**. The next screen offers only the
plain-language choices related to that intention, and the final screen asks only for that action's fields. Each stage
explains what is missing immediately and shows a live sentence describing the configured step. Optional utility
settings stay under **Advanced**.

Use **Pick on Screen**, **Pick Window**, and **Browse** directly from the relevant configuration screen. **Test
Match** checks a configured image without inserting the step; **Test Step** runs an executable draft once through the
normal runner and removes the temporary draft afterward. Conditions and repeat markers are tested with their
surrounding flow. Experienced users can choose **Use the full step editor** at any time to access the complete existing
action list. Guided and full modes both create the same `RpaAction` records, so existing projects, execution,
validation, save/reload, and generated Python remain unchanged.

Click, double-click, right-click, mouse move, and drag steps have **Pick on Screen** controls: the recorder hides while a crosshair overlay captures the selected position. Press Esc or right-click to cancel without changing the new step. This works across monitors, including monitors positioned to the left of the primary display.

For clicks, choose coordinate-only execution or capture/select an image target. Captured targets use image matching first and retain the selected coordinate as a fallback. Scroll uses direction and amount, typing accepts multiline text and variable insertion, waiting accepts milliseconds, keyboard steps offer common keys/shortcuts, and file/application steps use Browse.

## Advanced Step Editing and Groups

The step table supports Ctrl-click and Shift-click range selection. Drag selected rows to reorder them; dragging an If, Repeat, or named-group header moves its complete block. Reordering is intentionally unavailable while a step filter is active, preventing hidden rows from producing an unexpected position. Invalid drops that would separate an If/Else/End If, Repeat/End Loop, or Group/End Group pair are rejected without changing the flow.

Use the **Step Editing** menu, keyboard shortcuts, or the table's context menu for:

- **Copy / Cut / Paste** (`Ctrl+C`, `Ctrl+X`, `Ctrl+V`) with new IDs for pasted steps. Complete control blocks are copied as a unit. Failure-action jump targets are tracked by step ID and remapped to the correct step number after insertion or reordering; a cross-flow paste is rejected if its external jump target is unavailable.
- **Duplicate** (`Ctrl+D`) for one step, several selected executable steps, or a continuous range. Selecting an If, Repeat, or Group header duplicates the complete block after its matching closer.
- **Enable Selected**, **Disable Selected**, **Delete**, and **Set Wait Before** for bulk changes. Non-executable comments and structural markers are not enabled or disabled. A deletion is blocked when another step still jumps to the selected target.
- **Add Comment** to place a maintainers' note between steps. Comments are editable in Step Details, do not execute, and become `# Note:` comments in generated Python.
- **Group Selected** to wrap a structurally complete continuous range in a named section. Click the disclosure arrow on the Group row to collapse or expand it. The group name and collapsed state are saved in `project.json`.
- **Move Into Group** to append a selected continuous range to a chosen group, or **Move Out of Group** to place it immediately after its nearest enclosing group. Moves that would cut through a condition or loop are rejected.

Every editing command, including collapse/expand, grouping, bulk changes, drag-and-drop, and clipboard operations, participates in Undo/Redo. Search temporarily reveals matching rows inside collapsed groups without changing their saved collapse state. Existing flows load without conversion because groups and comments use optional action records in the existing ordered action list.

## Advanced Image Targeting

Select a Click Image or Double Click Image step to manage targeting in **Step Details**:

- **Capture / Crop Target** hides the recorder and opens the resizable screen-crop overlay. Add existing alternatives with **Add Images**; references are tried in the displayed order, and can be reordered or removed without editing paths.
- **Test Match Now** searches one current desktop screenshot, lists every candidate at 50% confidence or better, and shows its reference, confidence, top-left location, rank, and exact click coordinate. **Highlight on Screen** outlines every candidate and emphasizes the selected one. **Use Selected Match** moves its reference to first priority when necessary and stores the selected match number.
- **Search area > Select on Screen** limits matching to a rectangle selected across the virtual multi-monitor desktop. **Clear** restores whole-desktop matching.
- The confidence slider defaults to 86%. Advanced settings provide grayscale matching and highest-confidence, edge-based, or specific-match priority. Coordinate fallback is always shown as enabled with its saved position or disabled/image-required.

Runtime matching captures the desktop once per poll, tries reference images in order, and logs the winning reference, confidence, location, best score, and search duration. These values are also saved in per-step run evidence. Missing, unreadable, and oversized references produce actionable warnings; oversized images specifically call out display scaling and resolution. Because image matching is pixel-based, recapture references after material DPI, display-scaling, theme, or resolution changes. Existing projects with only the legacy `image` field continue to load and run unchanged, while generated Python includes the same ordered references, grayscale, search-region, priority, and fallback behavior.

## Breakpoints and Step-Through Debugging

Select one or more executable steps and press **F9**, use **Step Editing > Toggle Breakpoint**, or right-click and choose **Toggle Breakpoint**. A red dot in the Step column marks each saved breakpoint. Block markers such as Else and End If cannot hold breakpoints. Breakpoints are stored in `project.json`, survive save/reload and undo/redo, and are intentionally ignored by generated Python.

Normal **Run**, **Run From Here**, and **Test Step** pause before any breakpoint in their run range. **Run Until Breakpoint** starts at the selected step (or Step 1 when nothing is selected) and stops at the next saved breakpoint. With no breakpoint in range, normal execution is unchanged.

While paused, the main step list reappears with the paused row highlighted and the floating runner shows the current and next executable step. Its controls are:

- **Resume** continues until the next breakpoint.
- **Step Over** executes the current step and pauses before the next executable step.
- **Skip Step** records the current step as skipped and pauses before the next executable step.
- **Restart Selected** restarts debugging at the enabled executable row selected in the current run range.
- **Variables** shows current runtime values. Project, non-sensitive runtime-input, and output values can be edited before continuing; sensitive inputs and protected built-ins remain masked/read-only.
- **Stop Run** interrupts the breakpoint wait immediately and completes the run as Stopped.

Breakpoint pauses, resumes, step-over commands, skipped steps, restarts, and variable edits are written to the Logs/Status view and the affected step's `debug_events` in execution evidence. Scheduled execution does not pause for interactive breakpoints.

During Run, Run From Here, Run Until Here, Test Step, and scheduled execution, the recorder hides by default, Windows shows the desktop (minimizing other windows), and a floating **Stop Run** control remains available. The recorder is restored when execution ends; other windows remain minimized. Turn this behavior off in Settings with **Hide recorder while running**.

## Visual Conditions and Loops

Add logic from **Add Step** without writing Python. Available condition blocks are **If Image Exists**, **If Image Does Not Exist**, **If Window Exists**, **If File or Folder Exists**, and **If Variable** with Equals, Contains, and Is Empty comparisons. Add **Else** inside an If block when an alternative branch is needed. Loop blocks support **Repeat N Times**, **Repeat Until**, and **Break Loop**.

Adding an If or Repeat opener automatically adds its matching **End If** or **End Loop**, preventing an incomplete new block. Insert Else or Break Loop at a selected position inside the appropriate block; invalid placement is rejected with a clear explanation. Nested blocks are indented in the step list. Click the disclosure arrow in the Step column to collapse or expand a block; searching temporarily searches all nested rows.

Repeat Until always has a configurable maximum-iteration safety limit and optional interruptible delay. Validation blocks orphaned Else/End markers, missing closers, Break outside a loop, disabled structural markers, run ranges that cut through blocks, and failure jumps that cross or target control boundaries. Very large iteration limits are reported as warnings. **Stop Run** interrupts loop delays and image-condition checks promptly.

During a run, condition outcomes, selected branches, loop iterations, completion, and breaks appear in the Logs/Status view and floating runner. The same results are stored per step in execution evidence and shown in Run Details under **Execution Result**. Generated Python emits normal readable `if`/`else` and `for` control structures and preserves the same variable, window, file/folder, and image conditions.

## Window-Aware Automation

Use the window actions in **Add Step** when a flow should keep working after an application window moves or changes monitor:

- **Select / Target Window** finds a window and makes it available to later window steps.
- **Wait for Window** waits up to the configured timeout without activating it.
- **Activate Window**, **Maximize Window**, **Minimize Window**, **Restore Window**, and **Close Window** perform the named native Windows operation.
- **Click Relative to Window** and **Move Mouse Relative to Window** activate the located window and calculate the screen position from its current top-left corner.

Click **Pick Window**, then click a visible application window. The recorder stays hidden while the crosshair is active and captures the process filename, visible title, native class, window bounds, and—when adding a relative action—the clicked offset inside that window. Esc or right-click cancels without changing the step. Picking and runtime coordinates support negative multi-monitor positions.

Targets can match process filename, window title, optional native class, or a combination. Title matching supports **Contains**, **Exact**, and **Regular Expression**. When several windows match, choose a clear error (default), the top-most match, or the currently active match. Timeout and retry interval control how long the runner waits for a window to appear.

Relative actions can optionally scale their saved offset when the window is resized. Minimized targets are restored before relative input and the target is activated immediately before the mouse action. Absolute coordinate fallback is disabled by default and is used only when **Use absolute fallback** is explicitly enabled. Missing and ambiguous windows produce distinct errors; activation/control failures explain that the recorder and target application may need the same Windows permission level.

Window matching and operation results are logged and stored with the step result in execution evidence. Run Details shows the operation and resolved window. Generated Python includes equivalent standalone Win32 discovery and control helpers. Existing Click Position and Mouse Move steps retain their original behavior and project format.

## Retries and Failure Handling

Every step has compatible defaults of zero retries and **Stop Flow** on failure. Expand **Advanced Settings** to configure additional retry attempts, the interruptible delay between retries, an optional step timeout, and the final failure action:

- **Stop Flow** ends the run immediately.
- **Continue** marks the step failed and executes the next step.
- **Jump to Step** marks the step failed and moves to a validated step number inside the current run range.

App runs automatically save a full-screen screenshot on final step failure inside that run's evidence folder. The existing **Capture final failure** option remains compatible with direct/legacy runner use outside an evidence session. Retry attempts and reasons appear in the Logs/Status view and floating runner. Image steps continue polling until their search timeout, retain the best confidence found, retry when configured, and only use coordinate fallback on the final attempt. Stop Run interrupts start delays, waits, retry delays, and image polling promptly. Step timeout applies to interruptible waits and image polling; Python code can cooperate through its existing `check_stop()` callback. A flow that continues or jumps after a failed step finishes its remaining work but retains a final `Failed` result and the first failure in schedule history.

## Validate Flow

Use **Validate** in the Review toolbar or **Validate Flow** in the Execution menu before running. Run, Test Step, Run From Here, Run Until Here, Python generation, and scheduled execution also validate automatically.

Validation checks required fields, variables, screenshots and file/application paths, coordinate data, image confidence/timeouts, control-block nesting and loop safety, Python syntax, action types, IDs, and other runtime values. Results appear in the Validation tab as `Error`, `Warning`, or `Info`, with the step number, step name, and reason. Double-click a result to clear the step filter, select the affected step, and scroll it into view. Errors block execution. Interactive warnings require confirmation; unattended scheduled runs record warnings in the log and continue, while validation errors are stored as failed schedule history.

## Log Viewer

The larger, resizable Logs/Status tab remembers its splitter size and uses a more readable default font. Entries are color-coded by severity, include the currently running step, and follow the newest entry unless you scroll up. Use Search, Clear, Copy, Save Log, Open File, or **Run Details** from its header. Switch to the neighboring Validation tab to review flow-readiness results without losing logs.

## Execution Evidence and Run Reports

Each manual, scheduled, range, or step-test run creates a timestamped folder under the flow's `runs/` directory. The folder contains `execution.log`, a machine-readable `summary.json`, an automatic failure screenshot when a step fails, and optional before/after screenshots for steps where those Advanced Settings are enabled. Reports include the flow and run source, validation results, masked runtime inputs, timestamps, duration, final status, failed step/error, per-step results and durations, and retry attempts.

Open a report from **Run Details** in the main Logs/Status header or from Schedule History. The report provides direct buttons for its folder, log, and screenshots. Older history entries remain compatible and are labeled as legacy runs when they do not have an evidence reference. If evidence was manually deleted or moved, Run Details explains what is unavailable instead of failing.

Use **Settings → Run evidence retention** to choose how many timestamped run folders each flow keeps (100 by default). Cleanup only removes older run folders for that flow; scheduler-history retention remains independently configurable in Schedule Flows.

## Generate Python

Click `Generate Python` to create:

```text
generated/generated_rpa.py
```

The generated script uses relative project paths, PyAutoGUI, Pillow, and OpenCV. It does not load `project.json` or call the internal runner.

## Project Format

Each project is a folder:

```text
MyRecording/
  project.json
  screenshots/
  generated/
  logs/
  runs/
    20260718_165500_000_manual_ab12cd34/
      execution.log
      summary.json
      screenshots/
```

`project.json` contains settings, typed flow-variable definitions, Runtime Input definitions, Output Variable names, and ordered actions. Older projects containing a flat `variables` object—or a legacy JSON-string variable map—are migrated when loaded and continue to run unchanged.

## Variables and Runtime Inputs

Open **Project → Variables** (or use **Variables** in the Review toolbar) to manage four clear views:

- **Project Variables** uses a table for Name, Type, Default Value, Current Runtime Value, Description, and Secret. Supported types are Text, Integer, Decimal, Boolean, List, Object / JSON, Null, and Secret Text. Add, edit, delete, duplicate, import/export JSON, or reset runtime values without writing initialization code. Names follow Python identifier rules and must be unique across flow variables and Runtime Inputs. List/Object editors validate and format nested JSON before saving.
- **Runtime Inputs** are defined with a type, default, required/optional state, description, and sensitive flag. Manual Run, Test Step, Run From Here, and Run Until Here request them before the recorder hides. Supported controls include text, number, date, dropdown, password, file, and folder.
- **Output Variables** document values produced by earlier steps. Type Text and Open File can store their resolved value; Run Python and Python Code can store `result`. Python code may also continue assigning `variables['NAME']` directly.
- **Current Values** shows the latest or active runner state for debugging, including built-ins and outputs. Sensitive values are always displayed as `[REDACTED]`.

At run start, defaults are deep-copied into one mutable Python dictionary named `variables`. Every step and retry in that run receives the same dictionary, so `variables["quantity"] = 100`, `variables.update(...)`, and nested changes such as `variables["order"]["approved"] = True` are immediately visible to later steps. A new run resets to defaults. Enable **Persist variable values between runs** only when the flow should reuse JSON-compatible values; unsupported Python objects are skipped with a clear log warning. Retry attempts intentionally preserve changes made earlier in the same run.

Use the Guided Flow Builder's **Work with a variable** intent for **Set Variable**, **Get Variable**, **Increment Variable**, **Append to List**, **Set Object Property**, and **Delete Variable**. Variable fields provide known names while remaining editable for outputs created earlier in the flow. These steps use the normal retry, timeout, validation, evidence, and generated-Python paths.

Placeholders resolve immediately before each step while the saved template remains unchanged. They work throughout action data, including coordinates, timeouts, paths, image settings, text, and keys. Nested object paths are supported: `{{quantity}}` and `{{order.product}}`. Common built-ins include `{{CLIPBOARD_TEXT}}`, `{{LAST_CLICK_X}}`, `{{LAST_CLICK_Y}}`, and `{{RUN_DATE}}`. `RUN_DATE` uses ISO `YYYY-MM-DD`; clipboard text is captured when the run begins; the last-click coordinates update after click, image-click fallback, and drag actions.

Required, typed, choice, and file/folder inputs are validated before execution. Scheduled runs never show an input dialog: configure their saved values in **Schedule Flows → Configure Inputs…**. Missing scheduled values block the run and are recorded as a validation failure. Sensitive runtime and Secret Text values are masked in the Variables table, Log Viewer, evidence logs, summaries, errors, and persistent history. Project variables and scheduled values are still stored locally in `project.json` and `flows/schedules.json`; this is masking, not encryption, so protect those files using normal Windows account and folder permissions.

Generated Python contains the same non-secret defaults, nested placeholder handling, shared mutable dictionary, and variable steps. It prompts for Runtime Inputs in the console (using hidden password entry), or accepts unattended values from environment variables named `RPA_INPUT_<VARIABLE_NAME>`. Supply flow secrets at runtime because raw Secret Text defaults are intentionally not emitted into generated source.

## Build EXE

The build uses the existing main `.venv`; it does not create another environment or install packages on every build.

```powershell
cd D:\00_2026\python-rpa\simple-rpa\python-rpa-recorder
.\scripts\build.ps1
```

This runs compilation checks, pytest, PyInstaller, and the Inno Setup installer. Build only the unpacked application with:

```powershell
.\scripts\build.ps1 -SkipInstaller
```

The target executable is:

```text
dist\PythonRPARecorder\PythonRPARecorder.exe
```

## Build Windows Installer

Install Inno Setup 6 once if it is not already available:

```powershell
winget install --id JRSoftware.InnoSetup -e
```

After building the EXE, the installer can be rebuilt independently:

```powershell
.\scripts\build_installer.ps1
```

The installer is created at:

```text
installer_output\PythonRPARecorderSetup.exe
```

The installer defaults to `%LOCALAPPDATA%\Programs\PythonRPARecorder`, does not require administrator rights, and optionally creates a desktop shortcut. This location is intentional because flows are stored under the installed application folder and must remain writable. Existing `flows` are not packaged or overwritten, and the uninstaller leaves user-created flows in place.

## Current Limitations

- No OCR, browser automation, database, cloud service, AI, or Excel row looping.
- Custom Python actions are trusted code and run with the current user's permissions.
- Windows security boundaries matter: a non-administrator recorder may not capture or control administrator applications reliably. Run the recorder with the same privilege level as the target app.
- Automated tests use mocked input APIs and Qt's offscreen platform. Real global hooks, desktop input, multi-monitor scaling, and security software still require verification on the target Windows PC.

## Reusable Subflows

Add **Run Subflow** when a flow should reuse another saved flow. The step editor lists flows beside the current flow; it does not accept a manually typed path. References are saved relative to the parent `project.json`, so moving the containing projects folder keeps them portable. **Open Flow** opens the selected child directly (and offers to save parent changes first).

Map only the parent Project Variables, Runtime Inputs, built-ins, or earlier outputs the child needs. A child input mapping is `child input ← parent variable`; an output mapping is `child output → parent variable`. Required child Runtime Inputs not supplied by a mapping still use their configured defaults, or validation/execution reports the missing value. Declare returned names under the child flow's Output Variables.

Run Subflow uses the same retry count, retry delay, step timeout, final-failure action, and screenshot/evidence settings as other executable steps. Logs are prefixed with the child flow name. The parent step's evidence contains the child's per-step status, duration, retries, errors, and mapped outputs; Stop Run and the parent timeout propagate into nested waits immediately. Validation blocks missing/corrupt targets, undefined mappings, undeclared outputs, circular references, and nesting beyond 10 child levels. Generated Python retains the relative reference and uses the same application runner for nested execution. Existing projects are unchanged because `run_subflow` is only present when explicitly added.

Manual check: create sibling flows `Parent` and `Child`; give Child a required `VALUE` Runtime Input, declared `RESULT` Output Variable, and a Python step that writes `variables['RESULT']`. In Parent, add Run Subflow, select Child, map `VALUE` from a parent variable and `RESULT` back to a parent output. Run Parent and confirm the nested log prefix, returned value, and nested step results in Run Details. Then temporarily rename Child's folder and confirm Validate Flow blocks the run with a missing-subflow error.

## Windows Utility Steps

**Add Step** includes Windows-oriented actions that avoid fragile screen clicks: Launch Application; Wait for, Activate, or Close Process; Read or Write Clipboard; Copy, Move, Rename, Delete, or Wait for a file/folder; Run PowerShell Command; Run Python Script; and Show Desktop Notification. Each form exposes only its relevant application/process/path/command fields and provides Browse or Pick Running controls. Paths, arguments, commands, messages, and working folders accept the same `{{VARIABLE}}` placeholders as other actions.

PowerShell and Python Script steps capture stdout, stderr, exit code, and duration in the step's execution evidence. Optional output fields store stdout, stderr, and exit code in named variables for later steps. A non-zero exit code fails the step unless **Allow a non-zero exit code** is enabled. **Mask command and arguments in logs** replaces the stored command line with `[REDACTED]`; use sensitive Runtime Inputs as well when secret values are passed. Stop Run terminates a running child command promptly, including during retries and timeouts.

All utility actions use the normal Advanced Settings for retry count, delay, step timeout, final-failure action, and evidence screenshots. Validation checks required fields, missing applications/scripts/source paths, output-variable names, working folders, destination permissions where Windows exposes them, operation timeouts, and PowerShell availability. A Wait for File/Folder target is intentionally allowed to be absent before execution. Delete is permanent and should be limited to a narrowly selected path. Existing Open File and Python Code steps remain compatible and unchanged.

Manual check: launch Notepad, pick `notepad.exe` for Wait/Activate Process, write text to the clipboard and read it into an output variable, then copy/move/rename/delete a disposable file under a test folder. Run a PowerShell command that writes to both output streams and a Python script that returns a non-zero exit code; confirm stdout/stderr/code/duration in Run Details and failure handling. Finally run a long script, click Stop Run, and confirm its process exits promptly and the evidence status is Stopped.

## Manual Windows Verification With Notepad

Run these checks on the same Windows account and display configuration that will run the automation. Start Notepad and Python RPA Recorder at the same elevation level unless the permission test specifically says otherwise.

### Recording, Pause, Resume, and Stop

1. Start the recorder with `.\.venv\Scripts\python.exe app.py` and create a flow named `Notepad Verification`.
2. Open Notepad and click `Record`. Confirm the main window hides, Windows shows the desktop, the floating recording toolbar remains on top, and capture does not begin until its three-second countdown ends.
3. Click in Notepad, type `Hello from Python RPA Recorder`, press Enter, type `Second line`, and press Ctrl+S.
4. Click `Pause`, type `THIS MUST NOT BE RECORDED`, and click `Resume`.
5. Close or cancel the Notepad Save dialog, return to Notepad, scroll, then click `Stop`.
6. Confirm the recording summary appears, typed text is grouped, Ctrl+S is one Keyboard Shortcut step, screenshots exist, and no Pause/Resume/Stop toolbar clicks appear as steps.

### Cancel

1. Note the current step count, start another recording, type `CANCELLED`, and click `Cancel`.
2. Confirm the main window returns, the step count is unchanged, and screenshots from the cancelled session were removed.

### Interactive Target Recapture

1. Select a Click step and click `Recapture Target` in Step Details.
2. Confirm the main window hides and a full-screen transparent overlay with a crosshair appears.
3. Click a visible Notepad target. Change Width and Height and confirm the red crop rectangle resizes around the selected point.
4. Click `Cancel` once and confirm no step data changes and the main window returns.
5. Repeat, then click `Confirm`. Confirm the preview changes, the project becomes Modified, and Advanced Settings shows updated original X/Y and click offsets.
6. Test the same workflow on each monitor if the PC uses multiple monitors, including a monitor positioned left of the primary display.

### Replay and Stop Run

1. Clear Notepad, select the first step, and click `Run`.
2. Confirm Click, Type Text, Press Key, Keyboard Shortcut, and Scroll steps execute in order and row/status progress remains responsive.
3. Add a 10-second Wait step, run again, click `Stop Run` during the wait, and confirm execution stops promptly.
4. Test Stop Run while a Click step is searching for an image that is not visible.
5. With PyAutoGUI failsafe enabled, run and move the pointer into a screen corner. Confirm the run stops with the friendly safety message.

### Step Editing, Groups, and Comments

1. Create six simple steps. Ctrl-click several executable rows, disable and enable them, set their Wait Before value, then use Undo/Redo after each change.
2. Select a continuous range and choose **Group Selected**. Name it, collapse and expand it, save/reopen the flow, and confirm the name and collapsed state persist.
3. Drag the Group header and confirm the complete group moves. Try dragging only an End If or End Loop away from its opener and confirm the move is rejected without changing order.
4. Copy and paste a range, then confirm pasted steps have distinct IDs in `project.json`. Duplicate an If or Repeat header and confirm its complete block is placed after the original closer.
5. Add a Comment, edit it in Step Details, and generate Python. Confirm the note appears as `# Note:` and does not execute as an automation action.
6. Move a selected range into the named group and back out. Confirm any existing failure-action Jump still points to the same logical target step.
7. Apply a search filter and confirm matching rows inside a collapsed group are visible. Attempt a drag while filtered and confirm ordering remains unchanged.

### Windows Task Scheduler

1. Save a small flow, open **Schedule Flows**, select it, and enable its schedule at the 5-minute interval.
2. Confirm the Windows task status becomes **Registered**. In Task Scheduler, open `\PythonRPARecorder\` and verify the task is named `Flow_<flow-id>_<schedule-id>`, uses **Run only when user is logged on**, **Start the task as soon as possible after a scheduled start is missed**, and **Do not start a new instance**.
3. Click **Add Schedule**, choose a different interval, and enable it. Confirm a second task with a different schedule ID exists for the same flow.
4. Close Python RPA Recorder and click **Test Run** before closing, or choose **Run** in Windows Task Scheduler. Confirm the standalone floating Stop Run window appears, the desktop is prepared, and the flow executes without the main app staying open.
5. Reopen the recorder and Schedule Flows. Confirm the run appears in that schedule's history and Run Details opens its evidence.
6. Pause, resume, disable, edit, and delete one schedule. Confirm only its matching Windows task changes and the other schedule remains intact.
7. If registration requests administrator access, confirm UAC applies only to the short task-registration helper and the main recorder remains at its original privilege level.

### Breakpoints and Step-Through Debugging

1. Add three simple steps that write distinct text, select Steps 1 and 3 with Ctrl-click, and press F9. Confirm both rows show red breakpoint dots; save, close, and reopen the flow to confirm the markers persist.
2. Click **Run Until Breakpoint**. Confirm execution pauses before Step 1, the main step list reappears with Step 1 selected, and the floating runner shows both the current and next executable steps.
3. Click **Step Over**. Confirm Step 1 executes and the runner pauses before Step 2 even though Step 2 has no breakpoint.
4. Open **Variables**, change a non-sensitive project/output value, and apply it. Confirm password/sensitive inputs are redacted and protected built-ins cannot be edited.
5. Click **Skip Step**. Confirm Step 2 does not execute, its row becomes Skipped, and execution pauses before Step 3.
6. Select Step 1 in the visible table and click **Restart Selected**. Confirm the debugger returns to Step 1 without starting a second runner or restoring unrelated windows.
7. Click **Resume** and confirm it pauses at Step 3. Resume again and confirm the flow completes normally.
8. Repeat and click **Stop Run** while paused. Confirm it stops immediately, restores the recorder, and Run Details records the pause and stopped status.
9. Generate Python and confirm the script executes the same normal step sequence without interactive breakpoint prompts.

### Window-Aware Steps

1. Open Notepad, add **Select / Target Window**, click **Pick Window**, and click Notepad. Confirm its process, title, and class are populated.
2. Add **Click Relative to Window** using the selected window, pick a point inside Notepad, and leave absolute fallback disabled.
3. Move Notepad to another monitor and run. Confirm the click keeps the same window-relative position.
4. Resize Notepad and run with **Scale this position when the window is resized** enabled, then minimize it and run again. Confirm it is restored and activated before the click.
5. Open a second matching Notepad window and confirm the default multiple-match behavior reports an ambiguous target. Refine the title or choose an explicit multiple-match policy.
6. Close Notepad and confirm the missing-window error includes the configured timeout. If testing an elevated application, confirm a permission mismatch produces the permission guidance rather than an absolute click.

### Windows Permission Boundary

1. Run the recorder normally and start Notepad as administrator. Begin recording or replay against elevated Notepad.
2. Confirm the recorder reports that the applications use different permission levels.
3. Restart both applications at the same elevation level and confirm recording and replay work.

### Generated Python

1. Click `Generate`, open `generated\generated_rpa.py`, and confirm each enabled step appears explicitly in the same order.
2. Run `generated\run_generated.ps1` and verify the Notepad workflow completes before the `Flow completed` message appears.
3. Add a nested If/Else inside Repeat N Times, generate again, and confirm the script contains readable nested `if`/`else` and `for` statements and produces the same branch/iteration results as the desktop runner.
3. Repeat from the packaged application folder on a Windows PC without Python installed to verify the generated runner locates `PythonRPARecorder.exe`.

## Troubleshooting

- If recording fails, check that `pynput` installed correctly and no security policy blocks global hooks.
- If click replay misses, lower confidence or enable coordinate fallback.
- If PyAutoGUI aborts, move the mouse away from the screen corner or disable failsafe in settings.
- If generated scripts fail to locate images, confirm screenshots still exist in the project `screenshots/` folder.
- If a schedule shows **Task missing**, use **Repair / Register Task**. Also confirm the flow's `project.json` still exists at the saved location.
- If a schedule shows **Registration failed**, hover the status or check Logs/Status for the Task Scheduler error. A cancelled UAC prompt, Windows policy, invalid project path, or insufficient task-folder permission is reported without storing credentials.
- If a Test Run fails to launch, verify that the installed executable still exists. Source runs require the same Python environment and `app.py` path used when the task was registered; edit or re-save the schedule after moving the application.
- If **Run Until Breakpoint** reports that none exists, select an enabled executable row and press F9; structural If/Else/End/Repeat markers cannot hold breakpoints.
- If a debugger variable is read-only, it is either a sensitive Runtime Input or a protected run-provided value such as `RUN_DATE` or `CLIPBOARD_TEXT`.
