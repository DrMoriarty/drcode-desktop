"""Headless smoke of the main window: build it, open a tmp project,
pump the event loop briefly, tear down. Exit 0 = the window wiring is
alive (the GUI test suite covers the core; this covers app.py)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from drcodedesktop.app import MainWindow
from drcodedesktop.gui_config import GuiConfig


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="drcode-desktop-smoke-"))
    project = tmp / "proj"
    project.mkdir()
    os_env_config = tmp / "gui-config"
    os_env_config.mkdir()
    os.environ["DRCODE_DESKTOP_CONFIG_DIR"] = str(os_env_config)

    app = QApplication([])
    window = MainWindow(GuiConfig())
    window.show()
    window.open_project(project)
    QTimer.singleShot(1500, app.quit)
    app.exec()
    assert window._config.last == str(project.resolve()), window._config.last
    assert (os_env_config / "gui.json").is_file()
    assert (os_env_config / "logs" / "proj.log").is_file()
    log = (os_env_config / "logs" / "proj.log").read_text(encoding="utf-8")
    assert "Core started" in log, log
    print("window smoke ok; log lines:", len(log.strip().splitlines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
