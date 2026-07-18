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
3. The main window hides and a compact always-on-top toolbar appears.
4. Perform actions in other applications.
5. Use `Pause`, `Resume`, `Stop`, or `Cancel` from the floating toolbar.
6. Review, rename, edit, enable, disable, test, delete, or reorder recorded steps.

The recorder buffers printable typing into `type_text` actions, stores hotkeys as one action, preserves recorded runtime delays, and saves click screenshots under `screenshots/`.

## Replay Workflow

Click `Run`. Replay starts after the configured countdown. `Stop Run` requests a safe stop during waits and image polling. Click image actions search the screen first and use the original position when that option is enabled.

Right-click a step to test only that step, run from it, run until it, enable or disable it, insert another step, duplicate it, delete it, or move it. Replay and step tests run outside the Qt UI thread.

## Scheduling Flows

Open `Schedule Flows` (Execution toolbar group or menu, `⏱`) to see every saved flow and manage its automatic schedule. Click the `?` button in the dialog for an in-app explanation of every column and button; every control also has a hover tooltip.

Columns:

- **Flow** - the automation's name.
- **Enabled** - `Enabled`, `Paused`, or `Disabled`.
- **Run every** - the interval (15 minutes up to 24 hours).
- **Last run** - when the flow last started.
- **Duration** - how long the last run took.
- **Last status** - `Success`, `Failed`, `Running`, or a `Skipped (...)` reason.
- **Next run** - when the flow will run next (or `Paused`/`Disabled`).

Row actions:

- **Run Now** - runs the flow immediately without affecting its schedule or next run time.
- **Pause / Resume** - temporarily stops automatic runs while keeping the interval configuration intact. No confirmation needed.
- **Enable / Disable** - fully turns the schedule on or off. Disabling asks for confirmation first.
- **Details** - shows full last-run information, including the failure reason if the last run failed (also available as a tooltip on the Last status cell).

The table refreshes automatically every 5 seconds so a `Running` status and countdowns stay current, and can be refreshed manually with `Refresh`. Click `Flow`, `Last run`, `Next run`, or `Last status` headers to sort (click again to reverse); column widths and the chosen sort order are remembered between openings.

Only one flow runs at a time: if a different flow is already running, new runs are queued until it finishes; if the *same* flow is asked to run again while its own previous run hasn't finished, that attempt is skipped and marked `Skipped (Already Running)`. Schedules are stored permanently in `flows/schedules.json` and persist across app restarts.

## Step Details

Select a step to edit its friendly name and common settings on the right. Technical target settings such as match accuracy, timeout, image path, original coordinates, and click offsets are kept under `Advanced Settings`. Deselecting a step does not hide the step list.

The logs panel starts collapsed and can be expanded from its header. Window geometry, splitter positions, table widths, the logs state, and the Advanced section state are stored with `QSettings`.

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
2. Open Notepad and click `Record`. Confirm the main window hides and the floating recording toolbar remains on top.
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
