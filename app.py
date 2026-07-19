from __future__ import annotations

import sys
import runpy
import argparse
from pathlib import Path

from PySide6.QtWidgets import QApplication


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-generated":
        script = Path(sys.argv[2]).resolve()
        sys.argv = [str(script), *sys.argv[3:]]
        runpy.run_path(str(script), run_name="__main__")
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project")
    parser.add_argument("--schedule-id")
    parser.add_argument("--scheduled-run", action="store_true")
    parser.add_argument("--task-helper", nargs=2, metavar=("REQUEST", "RESULT"))
    options, _unknown = parser.parse_known_args(sys.argv[1:])
    if options.task_helper:
        from rpa.windows_tasks import run_task_helper

        return run_task_helper(Path(options.task_helper[0]), Path(options.task_helper[1]))
    if options.scheduled_run:
        if not options.project or not options.schedule_id:
            parser.error("--scheduled-run requires --project and --schedule-id")
        from rpa.scheduled_runner import scheduled_run_main

        app = QApplication(sys.argv)
        app.setApplicationName("Python RPA Recorder Scheduled Run")
        app.setQuitOnLastWindowClosed(False)
        _controller, exit_code = scheduled_run_main(
            app, Path(options.project), str(options.schedule_id),
        )
        return exit_code
    app = QApplication(sys.argv)
    app.setApplicationName("Python RPA Recorder")
    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    window.start_scheduler_reconciliation()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
