# Python RPA Recorder

Python RPA Recorder is a Windows-first desktop application for building and running repeatable desktop automations. It records global mouse and keyboard input, stores each automation as a readable folder containing JSON and screenshots, replays flows from a PySide6 interface, and can generate an editable standalone Python script.

The normal workflow is **Record → Review → Test → Run**. Non-programmers can use the Guided Add Step screens; experienced users can open the Full Step Editor or add Python and PowerShell steps.

For task-oriented instructions, see the [User Manual](docs/USER_MANUAL.md).

## Key capabilities

| Area | Implemented capabilities |
| --- | --- |
| Recording | Global clicks, double-click detection, scroll events, typed-text buffering, special keys, and keyboard shortcuts; click screenshots and coordinate fallback |
| Mouse and keyboard | Image or coordinate clicks, right-click, double-click, move, drag, scroll, type text, press key, hotkey, and wait |
| Windows and processes | Select, wait for, activate, resize, restore, close, click in, or move within native windows; launch, wait for, activate, or close processes |
| Files and data | Open, copy, move, rename, delete, or wait for paths; read an Excel/CSV column; read or write the clipboard |
| Logic | Image/window/path/variable conditions; Else; counted, repeat-until, and for-each loops; Break Loop |
| Variables | Typed project variables, runtime inputs, output declarations, variable actions, nested placeholders, built-ins, optional persisted values, and secret-value masking |
| Reliability | Validation, retries, fallback action, stop/continue/jump policy, expected-result polling, completion criteria, step timeouts, and interruptible Stop Run |
| Editing | Multi-select, duplicate, copy/cut/paste, reorder, enable/disable, groups, comments, undo/redo, filtering, and breakpoints |
| Reuse and code | Relative subflows with input/output mappings, PowerShell, Python Script, Run Python, Python Code, and generated Python |
| Operations | Manual and scheduled runs, Windows Task Scheduler integration, runtime-input profiles, history, logs, screenshots, evidence summaries, and Run Details |

The complete saved action identifiers are defined in `rpa/models.py`. The **Settings → Available Actions** page presents them in collapsible groups and controls only which types may be added. Disabling a type never removes or blocks an existing step.

New installations initially hide these advanced actions from Add Step: Drag, Scroll, Set Object Property, Run Python Script, Run Python, Run Subflow, Else, End If, Comment / Note, Group, and End Group. They can be enabled at any time. Existing flows and saved selections keep their stored settings.

## Requirements

- Windows is the primary supported platform.
- Python 3.11 or newer is recommended.
- A normal interactive desktop session is required for recording and PyAutoGUI replay.
- The recorder and target application should run at the same Windows privilege level.

Core dependencies include PySide6, pynput, PyAutoGUI, Pillow, OpenCV, NumPy, pandas, openpyxl, pywin32, pyperclip, and psutil. The repository also includes document, database, HTTP, test, and packaging dependencies in `requirements.txt`; not every package is used by every flow.

## Install from source

From PowerShell in the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Some optional integrations may require software outside Python, such as an ODBC driver for `pyodbc` or Microsoft Excel-compatible file support supplied by the installed spreadsheet libraries.

## Run from source

```powershell
.\.venv\Scripts\python.exe app.py
```

Useful application command forms are:

```text
app.py --project <project.json> --schedule-id <id> --scheduled-run
app.py --run-generated <generated_rpa.py>
```

The first is normally created and invoked by scheduling code. The second runs generated code through the current application executable or Python process.

## Basic workflow

1. Choose **File → New** or open an existing `project.json`.
2. Save the flow into its own folder.
3. Record actions, or choose **Add Step**.
4. Select a step to edit its name, target, timing, expected result, failure policy, or advanced settings.
5. Choose **Validate Flow** and correct errors.
6. Use **Test This Step**, **Run From Here**, or **Run**.
7. Review Logs/Status, Validation, and Run Details.

The [User Manual](docs/USER_MANUAL.md) explains each operation in detail.

## Project and flow folders

The application keeps its default flow collection under `flows/` beside the source application or packaged executable. A saved flow is folder-based:

```text
flows/
  schedules.json
  InvoiceEntry/
    project.json
    screenshots/
      click_0001.png
      manual_target_1784669823716.png
    generated/
      generated_rpa.py
      requirements.txt
      run_generated.ps1
    logs/
      run_YYYYMMDD_HHMMSS.log
    runs/
      <timestamped-run-id>/
        execution.log
        summary.json
        screenshots/
```

`project.json` contains project metadata, settings, variable definitions and optional runtime values, runtime-input definitions, output names, optional completion criteria, and the ordered action list. Screenshot paths are normally relative to the flow folder. **Save As** copies the existing screenshot files into the new flow folder.

Do not separate `project.json` from its `screenshots/` folder. Back up or move the complete flow folder.

## Features in practice

### Recording and image targeting

Recording uses `pynput` global listeners. Printable characters are combined into Type Text steps; modifiers plus keys become Hotkey steps; special keys become Press Key steps. Each click saves a cropped screenshot and the original coordinates. Replay searches for the image and can use the recorded position as a configured fallback.

Image steps support multiple ordered reference images, confidence, timeout, grayscale matching, a restricted search region, match priority, coordinate fallback, preview, recapture, and match diagnostics. Duplicate steps receive deep-copied settings and separate project-owned image files.

### Guided Add Step and available actions

Add Step begins with plain-language intentions for clicking, typing, opening, waiting, windows, files, conditions, loops, subflows, variables, and scripts. Categories with no enabled choices disappear and the remaining category buttons reflow into two columns. **Use the full step editor** remains available.

**Settings → Available Actions** contains grouped checkboxes with per-group and global controls. The selection affects only new-step choices. Loading, editing, validating, and running existing steps is unchanged.

### Variables and placeholders

Project variables support text, integer, decimal, Boolean, list, object, null, and secret-text definitions. Runtime inputs support text, number, date, dropdown, password, file, and folder controls. Output Variables document names produced by steps or scripts.

Action data uses double-brace placeholders:

```text
{{CUSTOMER_NAME}}
{{order.invoice_number}}
```

Built-ins include `RUN_DATE`, `CLIPBOARD_TEXT`, `LAST_CLICK_X`, and `LAST_CLICK_Y`. Missing placeholders are validation/runtime errors. Sensitive values are masked in UI and evidence, but local project and schedule files are not encrypted.

### Conditions and loops

Implemented If conditions test image presence/absence, window existence, file/folder existence, or a variable. Variable comparisons include Equals, Contains, and Is Empty. Implemented loops are Repeat N Times, Repeat Until, and For Each, with End Loop and Break Loop markers.

Adding an If or loop opener inserts its matching closer. The structural parser prevents invalid Else, End, Break, reorder, delete, and run-range operations. Nested blocks are indented and collapsible.

### Expected-result verification and failure handling

Expected Result is optional and condition-specific:

- Image Visible / Not Visible: image browse or capture, preview, confidence, timeout, and poll interval.
- File Exists / Not Exists: file/folder path, timeout, and poll interval.
- Process Running: process name or executable, timeout, and poll interval.
- Variable Equals: variable and expected value.
- Variable Not Empty: variable only.
- Window Title Contains: text or window picker, timeout, and poll interval.

Verification uses `image_visible`, `image_not_visible`, `file_exists`, `file_not_exists`, `process_running`, `variable_equals`, `variable_not_empty`, and `window_title_contains`. Verification values may use `${NAME}` references. Stop Run interrupts polling.

Failure Handling supports retry count/delay, a fallback step, optional user choice, and final stop, continue, or jump behavior. Flow Settings can also define all/any completion criteria using the same verification engine.

### Subflows and scripts

Run Subflow references another `project.json` relative to the parent flow and maps parent values to child runtime inputs and child outputs back to parent variables. Validation detects missing/corrupt targets, invalid mappings, cycles, and excessive nesting.

Script-related steps include Run PowerShell Command, Run Python Script, Run Python, and Python Code. Process-based script steps can capture output and exit information. These steps execute trusted local code with the current user's permissions.

### Scheduling

Schedule Flows stores configuration and history in `flows/schedules.json`. A flow may have multiple schedules with interval, enabled/paused state, runtime inputs, optional timeout, and optional highest-privilege setting.

On Windows, enabled schedules are registered under Windows Task Scheduler and run through the standalone scheduled-run entry point; the main window does not need to stay open. On non-Windows systems, the code contains an in-application polling fallback, but desktop actions remain Windows-oriented. Run Now, repair/register, pause/resume, enable/disable, runtime-input configuration, history filtering, and Run Details are implemented.

### Logs, evidence, and debugging

Runs write a normal log plus timestamped evidence containing `execution.log`, `summary.json`, and relevant screenshots. Run Details displays execution, validation, verification, retry, completion, user-intervention, and error information when present.

Breakpoints support Resume, Step Over, Skip Step, Restart Selected, variable inspection/editing, and Stop Run. Scheduled runs do not pause for interactive breakpoints.

### Generated Python

**Generate Python** validates the flow and writes `generated/generated_rpa.py`, a generated requirements file, and a PowerShell launcher. The script uses project-relative screenshots and implements recorded/manual actions, variables and runtime-input prompting, conditions, loops, subflows, Windows actions, and utility actions.

Run it from the generated folder:

```powershell
cd <flow-folder>\generated
.\run_generated.ps1
```

Generated Python is intentionally editable and does not load `project.json`. Current generation does not emit editor/runtime orchestration such as breakpoints, expected-result verification, failure-handling policies, flow completion criteria, run evidence, or schedule history. Use the application runner when those features are required.

## Build

The build scripts use the existing `.venv`.

Build the unpacked application only:

```powershell
.\scripts\build.ps1 -SkipInstaller
```

Build the application and then the installer:

```powershell
.\scripts\build.ps1
```

`build.ps1` runs Python compilation, the test suite, and PyInstaller before optionally invoking Inno Setup. The PyInstaller output is:

```text
dist\PythonRPARecorder\PythonRPARecorder.exe
```

Install Inno Setup 6 when an installer is required:

```powershell
winget install --id JRSoftware.InnoSetup -e
```

After a successful unpacked build, the installer can be compiled separately:

```powershell
.\scripts\build_installer.ps1
```

The Inno Setup script writes `installer_output\RPARecorderSetup.exe`. Before distributing an installer, verify that `installer/PythonRPARecorder.iss` names the same executable produced by `PythonRPARecorder.spec`; the current files use different executable names.

## Known limitations

- Recording and replay depend on a visible, unlocked interactive desktop; they are not service/session-zero automation.
- Image matching is pixel-based. Display scaling, resolution, theme, font rendering, and application appearance changes may require new reference images.
- Windows security boundaries can block hooks or input. Run the recorder and target at the same elevation level.
- The recorder captures clicks, scrolling, and keyboard activity, but not arbitrary mouse-move paths.
- Window automation uses native Windows discovery; there is no browser DOM, accessibility-tree, OCR, or AI vision automation layer.
- Delete Path is permanent. Test destructive file operations on disposable data.
- Python and PowerShell steps are trusted code, not a sandbox.
- Secret values are masked, not encrypted at rest.
- Generated Python does not reproduce every application-runner feature; see [Generated Python](#generated-python).
- Scheduled desktop automation still requires a suitable logged-in Windows desktop session and permissions.
- The current installer manifest/executable-name mismatch must be checked before installer distribution.

## Developer architecture

| Component | Responsibility |
| --- | --- |
| `app.py` | GUI, scheduled-run, task-helper, and generated-script entry points |
| `ui/main_window.py` | Main workflow, project commands, run orchestration, capture overlays, scheduling entry, logs, and evidence navigation |
| `ui/dialogs.py` | Guided/Full Add Step, Variables, Flow Settings, and Available Actions |
| `ui/action_editor.py` | Existing-step editor, image targets, expected results, failure handling, and action-specific fields |
| `ui/action_table.py` | Step rendering, selection, filtering, groups, context commands, and drag/drop |
| `rpa/models.py` | Versioned project, action, settings, variable, and runtime-input data models |
| `rpa/project_manager.py` | Folder creation, save, Save As, and load |
| `rpa/recorder.py` | Global pynput listeners, text buffering, click crops, and recorded timing |
| `rpa/runner.py` | Execution, control flow, retries, stop state, variables, verification, subflows, evidence, and debugging |
| `rpa/tools.py`, `rpa/builtin_tools.py` | Tool registry and built-in action adapters |
| `rpa/control_flow.py`, `rpa/step_editing.py` | Structural parsing and safe ID-aware list mutations |
| `rpa/validator.py` | Project, action, variable, verification, script, and structure validation |
| `rpa/image_matcher.py`, `rpa/windowing.py` | Screen/image and native-window services |
| `rpa/verification.py` | Polling expected-result and completion-condition engine |
| `rpa/subflows.py`, `rpa/native_utilities.py` | Reusable-flow and Windows utility services |
| `rpa/scheduler.py`, `rpa/windows_tasks.py`, `rpa/scheduled_runner.py` | Schedule persistence, Task Scheduler integration, and unattended entry point |
| `rpa/evidence.py` | Run folders, logs, summaries, screenshots, and retention |
| `rpa/generator.py` | Standalone Python generation |

The project format remains `python-rpa-recorder`, version `1`. New fields are optional and older supported representations are normalized while loading.

## Troubleshooting

Start with the [Common problems and solutions](docs/USER_MANUAL.md#common-problems-and-solutions) section. For a failed flow, also check the Validation tab, Logs/Status, the latest `logs/run_*.log`, and the latest `runs/<run-id>/summary.json`.
