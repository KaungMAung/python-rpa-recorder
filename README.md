# Python RPA Recorder

Python RPA Recorder is a Windows-first desktop recorder for simple PyAutoGUI workflows. It records global mouse and keyboard actions, stores projects as JSON plus screenshots, replays actions in a responsive PySide6 UI, and generates a normal editable Python script.

The main workflow is `Record -> Review -> Test -> Run`. The step list remains visible while a selected step is edited in the details panel.

## Architecture

- `app.py` starts the PySide6 application.
- `ui/` contains the main window, action table, editor, recorder toolbar, and dialogs.
- `rpa/models.py` defines project and action data.
- `rpa/project_manager.py` saves and opens folder-based projects.
- `rpa/recorder.py` uses `pynput` to capture global mouse and keyboard input.
- `rpa/runner.py` replays actions with `pyautogui` in a worker thread.
- `rpa/image_matcher.py` captures screenshots and locates images with OpenCV.
- `rpa/generator.py` creates `generated/generated_rpa.py`.
- `rpa/scheduler.py` stores and evaluates per-flow automatic run schedules (`flows/schedules.json`).
- `ui/schedule_dialog.py` is the Schedule Flows page used to enable, adjust, and monitor those schedules.

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

## Scheduling Flows

Open `Schedule Flows` (Execution toolbar group or menu, `⏱`) to see every saved flow and manage its automatic schedule. The header summarizes enabled, paused, disabled, and currently running flows. Search by flow name or filter by schedule/run status; a clear empty state explains when nothing matches.

Columns:

- **Flow** - the automation's name.
- **State** - color-coded `Enabled`, `Paused`, or `Disabled`.
- **Run every** - the interval (5 minutes up to 24 hours).
- **Last run** - a friendly relative time such as `2 min ago` (hover for the exact timestamp).
- **Duration** - how long the last run took.
- **Last status** - `Success`, `Failed`, `Running`, or a `Skipped (...)` reason.
- **Next run** - a countdown such as `in 12 min` (or `Paused`/`Disabled`; hover for the exact timestamp).

Select a row to open its Details panel. The panel shows the latest run, duration, result, error, next run, and schedule interval. It also contains persistent run history with source, start/end times, duration, attempts, result, failed step, and error. Filter history by `Success`, `Failed`, `Skipped`, or `Running`. Select a history row and use **Run Details** (or double-click it) to inspect its execution report. Change the interval or enabled/paused state there without adding controls to every table row. The row's labeled **Actions** menu provides:

- **Run Now** - runs the flow immediately without affecting its schedule or next run time.
- **Pause / Resume** - temporarily stops automatic runs while keeping the interval configuration intact. No confirmation needed.
- **Enable / Disable** - fully turns the schedule on or off. Disabling asks for confirmation first.
- **Details** - selects the row and opens full run information in the side panel (failure details also remain available on the Last status tooltip).

The header shows the auto-refresh state and refreshes every 5 seconds so `Running` status and countdowns stay current; use **Refresh** or `F5` for an immediate update. Click `Flow`, `Last run`, `Next run`, or `Last status` headers to sort (click again to reverse); column widths and the chosen sort order are remembered between openings. Row selection and all controls support keyboard navigation.

Only one flow runs at a time: if a manual or different scheduled flow is already running, new scheduled runs wait in the queue; if the *same* flow is requested while its previous scheduled run is active, that attempt is skipped and marked `Skipped (Already Running)`. Scheduled runs use the same desktop lifecycle as manual Run: the recorder hides, Windows shows the desktop, the safely positioned floating **Stop Run** control remains available, and execution starts after desktop preparation. The recorder is restored after success, failure, timeout, or stop. Other minimized applications remain minimized.

Schedules and run history are stored permanently in `flows/schedules.json` and persist across app restarts. History includes start/end time, duration, total step attempts, final status, failed/stopped step, and error. It keeps the latest 100 records per flow by default; adjust the retention control in the Details panel from 10 to 1,000 records. Existing schedule files are migrated automatically from their previous latest-run fields.

## Step Details

Select a step to edit its friendly name and common settings on the right. Technical target settings such as match accuracy, timeout, image path, original coordinates, click offsets, retries, and failure handling are kept under `Advanced Settings`. Target previews retain their original aspect ratio and point out missing screenshots. Deselecting a step does not hide the step list.

The lower Logs/Status and Validation area has a practical minimum height and remains resizable. Window geometry, splitter positions, table widths, the logs state, and the Advanced section state are stored with `QSettings`.

## Adding Steps

Use **Add Step** to configure an action in plain language. Click, double-click, right-click, mouse move, and drag steps have **Pick on Screen** controls: the recorder hides while a crosshair overlay captures the selected position. Press Esc or right-click to cancel without changing the new step. This works across monitors, including monitors positioned to the left of the primary display.

For clicks, choose coordinate-only execution or capture/select an image target. Captured targets use image matching first and retain the selected coordinate as a fallback. Scroll uses direction and amount, typing accepts multiline text and variable insertion, waiting accepts milliseconds, keyboard steps offer common keys/shortcuts, and file/application steps use Browse.

During Run, Run From Here, Run Until Here, Test Step, and scheduled execution, the recorder hides by default, Windows shows the desktop (minimizing other windows), and a floating **Stop Run** control remains available. The recorder is restored when execution ends; other windows remain minimized. Turn this behavior off in Settings with **Hide recorder while running**.

## Retries and Failure Handling

Every step has compatible defaults of zero retries and **Stop Flow** on failure. Expand **Advanced Settings** to configure additional retry attempts, the interruptible delay between retries, an optional step timeout, and the final failure action:

- **Stop Flow** ends the run immediately.
- **Continue** marks the step failed and executes the next step.
- **Jump to Step** marks the step failed and moves to a validated step number inside the current run range.

App runs automatically save a full-screen screenshot on final step failure inside that run's evidence folder. The existing **Capture final failure** option remains compatible with direct/legacy runner use outside an evidence session. Retry attempts and reasons appear in the Logs/Status view and floating runner. Image steps continue polling until their search timeout, retain the best confidence found, retry when configured, and only use coordinate fallback on the final attempt. Stop Run interrupts start delays, waits, retry delays, and image polling promptly. Step timeout applies to interruptible waits and image polling; Python code can cooperate through its existing `check_stop()` callback. A flow that continues or jumps after a failed step finishes its remaining work but retains a final `Failed` result and the first failure in schedule history.

## Validate Flow

Use **Validate** in the Review toolbar or **Validate Flow** in the Execution menu before running. Run, Test Step, Run From Here, Run Until Here, Python generation, and scheduled execution also validate automatically.

Validation checks required fields, variables, screenshots and file/application paths, coordinate data, image confidence/timeouts, Python syntax, action types, IDs, and other runtime values. Results appear in the Validation tab as `Error`, `Warning`, or `Info`, with the step number, step name, and reason. Double-click a result to clear the step filter, select the affected step, and scroll it into view. Errors block execution. Interactive warnings require confirmation; unattended scheduled runs record warnings in the log and continue, while validation errors are stored as failed schedule history.

## Log Viewer

The larger, resizable Logs/Status tab remembers its splitter size and uses a more readable default font. Entries are color-coded by severity, include the currently running step, and follow the newest entry unless you scroll up. Use Search, Clear, Copy, Save Log, Open File, or **Run Details** from its header. Switch to the neighboring Validation tab to review flow-readiness results without losing logs.

## Execution Evidence and Run Reports

Each manual, scheduled, range, or step-test run creates a timestamped folder under the flow's `runs/` directory. The folder contains `execution.log`, a machine-readable `summary.json`, an automatic failure screenshot when a step fails, and optional before/after screenshots for steps where those Advanced Settings are enabled. Reports include the flow and run source, validation results, timestamps, duration, final status, failed step/error, per-step results and durations, and retry attempts.

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

`project.json` contains settings, variables, and ordered actions.

## Variables

Use the `Variables` dialog to define project-level values. Placeholders such as `{{PROJECT_NAME}}` and `{{INPUT_FILE}}` are resolved in typed text, file paths, and Python action data.

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

### Windows Permission Boundary

1. Run the recorder normally and start Notepad as administrator. Begin recording or replay against elevated Notepad.
2. Confirm the recorder reports that the applications use different permission levels.
3. Restart both applications at the same elevation level and confirm recording and replay work.

### Generated Python

1. Click `Generate`, open `generated\generated_rpa.py`, and confirm each enabled step appears explicitly in the same order.
2. Run `generated\run_generated.ps1` and verify the Notepad workflow completes before the `Flow completed` message appears.
3. Repeat from the packaged application folder on a Windows PC without Python installed to verify the generated runner locates `PythonRPARecorder.exe`.

## Troubleshooting

- If recording fails, check that `pynput` installed correctly and no security policy blocks global hooks.
- If click replay misses, lower confidence or enable coordinate fallback.
- If PyAutoGUI aborts, move the mouse away from the screen corner or disable failsafe in settings.
- If generated scripts fail to locate images, confirm screenshots still exist in the project `screenshots/` folder.
