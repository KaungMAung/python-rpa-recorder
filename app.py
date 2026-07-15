from __future__ import annotations

import sys
import runpy
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-generated":
        script = Path(sys.argv[2]).resolve()
        sys.argv = [str(script), *sys.argv[3:]]
        runpy.run_path(str(script), run_name="__main__")
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("Python RPA Recorder")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
