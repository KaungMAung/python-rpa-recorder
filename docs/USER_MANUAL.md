# Python RPA Recorder User Manual

This manual explains the current application from a user's point of view. For installation, build, architecture, and the complete capability summary, return to the [README](../README.md).

## Getting started

Python RPA Recorder builds a flow as an ordered list of steps. A flow may come from recording, manually added steps, or both.

1. Start the application.
2. Choose **File → New**.
3. Save the flow into a new folder.
4. Record actions or choose **Add Step**.
5. Review and validate the steps.
6. Test a step or run the complete flow.

The main window contains:

- Workflow buttons for Record, Review, Test, and Run.
- A step table with filtering and multi-selection.
- Step Details for the selected row.
- Logs/Status and Validation tabs.
- Menus for file, recording, execution, step editing, project settings, and help.

## Create, open, save, and manage flows

### Create a flow

Choose **File → New**. If the current flow has unsaved work, respond to the save prompt. Give the flow a meaningful name and save it into its own folder before recording or capturing targets.

### Open a flow

Choose **File → Open** and select the flow's `project.json`. Open the JSON file, not an individual screenshot or generated script.

### Save and Save As

- **Save** updates `project.json` in the current flow folder.
- **Save As** creates or uses another flow folder and copies the current screenshots.

A flow should be moved or backed up as one folder because `project.json` normally refers to files under `screenshots/` by relative path.

### Flow folder contents

| Item | Purpose |
| --- | --- |
| `project.json` | Settings, variables, action list, and optional verification/completion data |
| `screenshots/` | Recorded, captured, reference, duplicated, and expected-result images |
| `generated/` | Generated Python and its launcher/dependency file |
| `logs/` | Normal run log files |
| `runs/` | Timestamped evidence folders and summaries |

The shared `flows/schedules.json` file stores schedules and schedule history.

## Record actions

1. Save the flow first.
2. Select **Record**.
3. Wait for desktop preparation and the countdown.
4. Work in the target application.
5. Use the floating toolbar to **Pause**, **Resume**, **Stop**, or **Cancel**.

Recording captures:

- Mouse clicks, including the button used.
- A second nearby click within the configured interval as a double-click.
- Scroll-wheel events.
- Printable typing grouped into Type Text steps.
- Special keys as Press Key steps.
- Modifier combinations as Hotkey steps.

Each click saves a cropped screenshot and the original screen position. The recorder does not capture arbitrary mouse movement paths. Use a Mouse Move or Drag step when needed.

By default, the application hides and minimizes normal desktop windows before recording begins. Flow Settings controls the countdown, crop size, confidence, coordinate fallback, typing interval, whether the application window is ignored, and desktop preparation.

If recording cannot interact with an elevated application, close both programs and restart them at the same Windows privilege level.

## Add and edit steps

Choose **Add Step** from the toolbar, Step Editing menu, or table context menu. Insert Before and Insert After use the same Add Step dialog relative to the selected row.

Select any existing row to open Step Details. Depending on the action, the editor shows its main fields, target preview, expected result, failure handling, and advanced settings. Changes update the in-memory flow immediately; use **Save** to write them to disk.

Common settings include:

- Friendly step name.
- Enabled/disabled state for executable steps.
- Wait before the step.
- Breakpoint.
- Action-specific target, text, path, keys, values, or mappings.
- Expected Result.
- Retry and final failure behavior.
- Step timeout and evidence screenshots.

### Image targets

Image click steps can contain an ordered list of references. Use **Capture / Crop Target**, **Add Images**, replace, remove, and reorder controls. Test Match Now reports current candidates and allows a selected reference/match to be prioritized.

Use the confidence and search-area controls to reduce false matches. Coordinate fallback can keep a flow working when an image is temporarily unavailable, but coordinates are fragile when windows move.

When an image step is duplicated, its nested settings receive a deep copy and project-owned images receive separate physical files. Recapturing or replacing the duplicate's image does not overwrite the original step's file.

## Guided Add Step and Full Step Editor

Guided Add Step begins with plain-language categories:

- Click something
- Type text
- Open an application
- Wait for something
- Work with a window
- Work with a file
- Add a condition
- Repeat steps
- Run another flow
- Work with a variable
- Run a script or command

Choose a category, choose the specific action, and complete only the fields relevant to that action. Hidden categories leave no gaps in the two-column list. Test Match and Test Step are offered where applicable.

Choose **Use the full step editor** from the category or choice screen to see every currently enabled technical action type. The Full Step Editor remains reachable even when some types are unavailable.

Guided and Full modes create the same saved actions. They differ only in how choices and fields are presented.

## Enable or disable available actions

Open **Project → Flow Settings → Available Actions**. Actions are arranged in collapsible groups such as Mouse Actions, Keyboard and Text, Windows, Conditions, Loops, Variables, Scripts and Commands, Clipboard, Flow Control, and Utilities.

**Project → System Settings** stores persistent application-wide defaults. A new flow receives a copy of those defaults. Changes made later in Flow Settings affect only the active flow, while later System Settings changes affect only flows created afterward. To replace the active flow's settings with the current defaults, choose **Reset to System Defaults** in Flow Settings and confirm.

Each group header shows an enabled count and contains a group checkbox. Use:

- A group checkbox to enable or disable that group.
- **Enable All** or **Disable All** for every type.
- **Reset to Default** for the application's new-flow defaults.

The new-flow defaults disable Drag, Scroll, Set Object Property, Run Python Script, Run Python, Run Subflow, Else, End If, Comment / Note, Group, and End Group. All other types are enabled.

This setting only controls adding new steps. Existing disabled-type steps still load, display, edit, validate, run, and generate normally.

## Variables and placeholders

Open **Project → Variables** or select **Variables** on the Review toolbar.

### Project Variables

Project variables have a name, type, default value, description, and optional secret flag. Supported types are Text, Integer, Decimal, Boolean, List, Object/JSON, Null, and Secret Text.

Names must follow Python identifier rules, such as `CUSTOMER_NAME` or `invoice_total`. List and Object values use JSON.

### Runtime Inputs

Runtime Inputs request values when a manual run starts. Types are text, number, date, dropdown, password, file, and folder. Inputs can be required, sensitive, and given defaults or choices.

Scheduled runs cannot show an input dialog. Save their values through Schedule Flows.

### Output Variables

Declare names that a flow or subflow returns or that scripts/actions create. The declaration documents the interface and supports subflow validation.

### Placeholders

Most action fields resolve placeholders immediately before execution:

```text
Hello {{CUSTOMER_NAME}}
{{order.invoice_number}}
C:\Reports\{{RUN_DATE}}.xlsx
```

An exact placeholder preserves the underlying value type. A placeholder inside longer text becomes text.

Built-ins include:

| Name | Meaning |
| --- | --- |
| `RUN_DATE` | Current date in `YYYY-MM-DD` form |
| `CLIPBOARD_TEXT` | Clipboard text captured for the run |
| `LAST_CLICK_X`, `LAST_CLICK_Y` | Most recent click/drag destination |

Expected-result verification uses `${NAME}` references in its verification values. This is separate from the normal `{{NAME}}` action placeholder syntax.

Secret values are masked in normal UI, logs, and evidence. They are not encrypted in local JSON files.

## Conditions and loops

### If blocks

Add one of:

- If Image Exists
- If Image Does Not Exist
- If Window Exists
- If File or Folder Exists
- If Variable

Variable conditions support Equals, Contains, and Is Empty. Adding an If opener automatically inserts End If. Add Else at a position inside the matching If block when enabled in Available Actions.

### Loops

- **Repeat N Times** runs a block a fixed number of times.
- **Repeat Until** checks a condition after each iteration and includes a maximum-iteration safety limit.
- **For Each** reads a list variable and assigns each item to an item variable.
- **Break Loop** leaves the nearest enclosing loop.

Adding a loop opener inserts End Loop. Invalid orphaned markers, misplaced Break steps, and edits that split blocks are rejected or reported by validation.

Control blocks are indented in the table. Their disclosure arrow collapses or expands nested rows without changing execution.

## Expected-result verification

Select an executable step and expand **Expected Result**. Enable verification, select a condition, and fill only the displayed fields.

| Condition | Required controls |
| --- | --- |
| Image Visible / Image Not Visible | Image Browse or Capture, preview/name; optional confidence, timeout, poll interval |
| File Exists / File Not Exists | File or folder path picker, timeout, poll interval |
| Process Running | Process name or executable selector, timeout, poll interval |
| Variable Equals | Variable and expected value |
| Variable Not Empty | Variable only |
| Window Title Contains | Title text or Pick Window, timeout, poll interval |

The step succeeds only when the action completes and its expectation passes. A zero timeout performs an immediate check; a positive timeout polls at the configured interval. Stop Run interrupts polling.

Flow Settings also offers optional completion criteria. Choose whether all or any listed verification conditions must pass after the flow finishes.

## Failure handling

Expand **Failure Handling** for an executable step:

- Retry count and delay.
- Optional fallback step JSON.
- Ask user after automatic recovery fails.
- Stop Flow, Continue, or Jump to Step.

Retries rerun the failed step. Continue and Jump keep a failure recorded even if later steps run. Jump targets are maintained when steps are safely inserted or reordered.

## Duplicate, reorder, enable, and delete steps

Select one row, use Ctrl-click for separate executable rows, or Shift-click for a range.

| Command | Behavior |
| --- | --- |
| Duplicate | Inserts a deep copy after the selection; block openers duplicate their complete block; new IDs and separate step-owned image files are created |
| Copy / Cut / Paste | Uses new IDs and remaps internal jump targets; complete control blocks remain structural units |
| Drag / Move Up / Move Down | Reorders selected steps while preserving valid control structures |
| Enable / Disable | Skips or restores executable steps; structural markers cannot be disabled |
| Delete | Removes the selection; may expand to a complete block and refuses unsafe external jump references |
| Group Selected | Wraps a continuous valid range in Group and End Group markers |
| Add Comment | Inserts a non-executing note |

Reordering is disabled while the step filter is active. Clear the filter first so hidden rows cannot make the destination ambiguous.

Use **Undo** and **Redo** after editing commands. Save once the flow is correct.

## Validate, test, run, stop, and troubleshoot flows

### Validate

Choose **Validate Flow**. Results appear in the Validation tab with severity, step number, step name, and reason. Double-click a result to select its step.

Errors block running and generated Python. Interactive warnings ask for confirmation. Validation checks fields, types, variables, paths, screenshots, scripts, IDs, control structure, verification, subflows, and runtime settings.

### Test

- **Test This Step** runs the selected executable step.
- **Test Match Now** checks an image without clicking.
- **Run From Here** starts at the selected step.
- **Run Until Here** stops after the selected range.
- A draft step's **Test Step** uses the normal runner without adding the draft permanently.

Control markers are tested in their surrounding structure rather than as independent actions.

### Run and stop

Choose **Run** for the complete flow. Required Runtime Inputs are collected first. The application normally hides during replay and shows a floating Stop Run control.

Stop Run is cooperative and interrupts waits, image polling, retry delays, loop delays, and supported child processes. Custom Python code should use its provided stop-check callback for long work.

### Breakpoints

Toggle a breakpoint with F9 or the Step Editing menu. During a debug pause, use Resume, Step Over, Skip Step, Restart Selected, Variables, or Stop Run. Scheduled runs ignore interactive breakpoints.

## Generate Python

Choose **Generate Python** after validation succeeds. Files are written under the flow's `generated/` folder.

Run with:

```powershell
cd <flow-folder>\generated
.\run_generated.ps1
```

The generated file is normal editable Python and uses the parent flow's screenshots. It includes actions, variables, runtime-input prompts, conditions, loops, subflows, Windows operations, and utility actions.

It does not include application-runner features such as breakpoints, expected-result verification, retries/fallback/final-failure policy, completion criteria, evidence reports, or scheduling. Run through the application when those features matter.

## Scheduling

Open **Schedule Flows** from the Execution controls.

1. Select a saved flow.
2. Add or select a schedule.
3. Choose an interval.
4. Configure required Runtime Inputs.
5. Enable the schedule.

Available operations include Run Now, Test Run, Repair/Register Task, Pause/Resume, Enable/Disable, Details, and Delete Schedule. Advanced settings include Windows highest privileges, an execution timeout, runtime inputs, and history retention.

On Windows, enabled schedules are represented by Windows Task Scheduler tasks. A suitable logged-in desktop session and the correct permissions are still required for visible desktop automation. Closing the main application does not remove registered tasks.

The Schedule Flows page refreshes task state and shows history, duration, result, attempts, failed step, and error. Double-click a history entry or choose Run Details when evidence is available.

## Logs and run details

The Logs/Status tab provides Search, Clear, Copy, Save Log, Open File, and Run Details. Messages include severity and current-step context.

Each run can create:

- `execution.log`
- `summary.json`
- Failure screenshots
- Optional before/after screenshots

Run Details displays the execution result, validation, verification, retries, completion criteria, user decisions, and errors recorded for that run. Flow Settings controls run-evidence folder retention; schedule history has a separate retention setting.

## Practical examples

### Example: enter text and save

1. Create `CUSTOMER_NAME` as a Text project variable.
2. Add Type Text with `Hello {{CUSTOMER_NAME}}`.
3. Add Hotkey with `Ctrl+S`.
4. Add Wait for one second.
5. Add Expected Result → Window Title Contains if the application changes its title after saving.
6. Validate and Test This Step before running the full flow.

### Example: wait for a downloaded file

1. Create a runtime input named `OUTPUT_FILE` with type File or Text.
2. Perform the steps that start the download.
3. Add Wait for File or Folder using `{{OUTPUT_FILE}}`.
4. Alternatively, attach Expected Result → File Exists to the triggering step with a timeout and poll interval.
5. Validate with a realistic test path.

### Example: process a spreadsheet column

1. Add Read Excel Column and choose an Excel or CSV file and column.
2. Store its result in a list variable.
3. Add For Each using that list and an item variable such as `current_item`.
4. Put Type Text or other actions inside the loop using `{{current_item}}`.
5. Validate the complete block and run it on disposable sample data first.

### Example: reusable child flow

1. In the child flow, define Runtime Inputs and Output Variables.
2. In the parent, enable Run Subflow in Available Actions.
3. Add Run Subflow and select the child flow.
4. Map parent variables to child inputs and child outputs back to parent names.
5. Validate to detect missing mappings or circular references.

## Common problems and solutions

| Problem | What to check |
| --- | --- |
| Click image is not found | Confirm the screenshot exists; use Test Match Now; lower confidence carefully; recapture after DPI, scaling, resolution, theme, or application changes |
| Click occurs in the wrong place | Prefer image or window-relative targeting; inspect click offsets and coordinate fallback |
| Recording misses an elevated app | Run the recorder and target at the same elevation level |
| Add Step category/action is missing | Open Project → Flow Settings → Available Actions and enable its type or group |
| Existing disabled-type step still runs | This is expected: Available Actions prevents new additions only; disable the step itself to skip it |
| Flow will not run | Open Validation, double-click the first Error, and correct missing variables, paths, screenshots, scripts, or structural markers |
| Placeholder remains unresolved | Check spelling/case and define the project variable, runtime input, output, or built-in before that step |
| Break Loop / Else is rejected | Select a position inside the correct enclosing Loop or If block |
| Reorder is unavailable | Clear the step filter; select a structurally complete range |
| Duplicate image changes the original | Save and duplicate through the current Step Editing command; duplicates should reference a new `manual_target_<timestamp>` file |
| Stop Run seems delayed | Long custom Python must call the provided stop check; external applications may not be interruptible after receiving input |
| Scheduled run has missing inputs | Configure Runtime Inputs on that exact schedule |
| Windows task is missing/broken | Use Repair / Register Task and review the displayed registration error; check permissions and executable/project paths |
| Generated script behaves differently | Generated Python omits verification, recovery, completion, breakpoint, evidence, and scheduler orchestration; use the application runner for parity |
| Secret appears in a JSON file | Masking is not encryption; protect project and schedule folders with Windows file permissions |

For developer details and current limitations, see the [README](../README.md#developer-architecture).
