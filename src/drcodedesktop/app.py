"""The DrCode Desktop main window (skeleton, plan task 1).

One window: a project picker (native folder dialog + the recents list
persisted in the GUI config) and a debug console where every core event
(task_updated / stream_delta / service_event / notify / host log) is
appended live. The core runs in-process on a qasync loop — Qt and the
pipeline share one thread, exactly like the Textual app.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QToolBar,
    QWidget,
)
from qasync import QEventLoop

from .chat_view import ChatTimelineView
from .core_shell import CoreShell
from .event_hub import EventHub, log_path_for
from .gui_config import GuiConfig
from .model_settings_dialog import ModelSettingsDialog
from .qt_host import QtHost
from .settings_dialog import ProjectSettingsDialog
from .task_queue_view import TaskQueueView


class MainWindow(QMainWindow):
    def __init__(self, config: GuiConfig) -> None:
        super().__init__()
        self._config = config
        self._shell: CoreShell | None = None
        self._hub: EventHub | None = None
        self._host: QtHost | None = None

        self.setWindowTitle("DrCode Desktop")
        self.resize(1000, 640)

        # The console keeps every raw core event; the queue tab (task 2)
        # renders the live task list above it.
        self._console = QPlainTextEdit(readOnly=True)
        self._console.setMaximumBlockCount(5000)
        self._tabs = QTabWidget()
        self._tabs.addTab(QWidget(), "Queue")  # replaced per project
        self._tabs.addTab(self._console, "Console")
        self._queue_view: TaskQueueView | None = None
        self._chat_view: ChatTimelineView | None = None
        self.setCentralWidget(self._tabs)

        toolbar = QToolBar("main")
        self.addToolBar(toolbar)
        open_action = QAction("Open Project…", self)
        open_action.triggered.connect(self._pick_project)
        toolbar.addAction(open_action)
        self._recent_button = QAction("Recent ▾", self)
        self._recent_button.triggered.connect(self._show_recents)
        toolbar.addAction(self._recent_button)
        self._model_label = QLabel("no project")
        toolbar.addWidget(self._model_label)

        self._recents_menu = QMenu(self)
        self._fill_recents_menu()

        # The Settings menu (plan task 6): project settings and per-role
        # model selection — both dialogs write the very config files the
        # TUI writes, so GUI and TUI stay interchangeable on one project.
        settings_menu = QMenu("&Settings", self.menuBar())
        self._settings_project_action = QAction("Project settings…", self)
        self._settings_project_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self._settings_project_action.triggered.connect(self._open_project_settings)
        self._settings_models_action = QAction("&Models…", self)
        self._settings_models_action.triggered.connect(self._open_model_settings)
        settings_menu.addAction(self._settings_project_action)
        settings_menu.addAction(self._settings_models_action)
        self.menuBar().addMenu(settings_menu)
        self._settings_project_action.setEnabled(False)
        self._settings_models_action.setEnabled(False)

    # -- project lifecycle ----------------------------------------------------

    def open_project(self, project_root: Path) -> None:
        """(Re)start the core on `project_root`: shut the previous shell
        down, build a fresh hub/host/shell trio and wire the hub into
        the debug console."""
        self.close_project()
        project_root = project_root.resolve()
        if not project_root.is_dir():
            QMessageBox.warning(self, "DrCode Desktop", "Not a folder")
            return

        self._hub = EventHub(log_path_for(project_root))
        self._hub.task_updated.connect(self._on_task_updated)
        self._hub.stream_delta.connect(self._on_stream_delta)
        self._hub.service_event.connect(self._on_service_event)
        self._hub.notify.connect(self._on_notify)
        self._hub.host_log.connect(self._console_append)

        self._host = QtHost(self._hub)
        self._shell = CoreShell(project_root, self._host, self._hub)
        self._install_queue_view()
        self._config.remember_project(project_root)
        self._fill_recents_menu()
        self._model_label.setText(project_root.name)
        self.setWindowTitle(f"DrCode Desktop — {project_root.name}")
        self._settings_project_action.setEnabled(True)
        self._settings_models_action.setEnabled(True)
        self._console_append(f"=== project: {project_root} ===")
        self._console_append(f"log file: {log_path_for(project_root)}")
        # Start once the event loop is running (workers/timers need it).
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self._start_shell)

    def close_project(self) -> None:
        if self._queue_view is not None:
            self._queue_view.shutdown()
        if self._chat_view is not None:
            self._chat_view.shutdown()
        if self._host is not None:
            self._host.clear_stream_sink(self._chat_view)
        if self._queue_view is not None:
            # The queue view lives inside the tab's splitter — remove the
            # tab by position (it is always tab 0 while a project is open).
            self._tabs.removeTab(0)
            self._queue_view.deleteLater()
            self._queue_view = None
            self._tabs.insertTab(0, QWidget(), "Queue")
        self._chat_view = None
        if self._shell is not None:
            self._shell.shutdown()
            self._shell = None
        self._host = None
        self._hub = None
        self._model_label.setText("no project")
        self._settings_project_action.setEnabled(False)
        self._settings_models_action.setEnabled(False)

    def _install_queue_view(self) -> None:
        """The queue screen of the freshly opened project (its model
        subscribes to the new hub; the placeholder tab is swapped out).
        The tab is a splitter: the live queue left, the selected task's
        chat pane right (plan task 3)."""
        assert (
            self._shell is not None
            and self._hub is not None
            and self._host is not None
        )
        self._queue_view = TaskQueueView(self._shell, self._hub)
        self._chat_view = ChatTimelineView(self._shell, self._hub)
        self._queue_view.task_activated.connect(self._view_task)
        self._chat_view.open_task_requested.connect(self._queue_view.select_task)
        splitter = QSplitter()
        splitter.addWidget(self._queue_view)
        splitter.addWidget(self._chat_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self._host.set_stream_sink(self._chat_view)
        self._tabs.removeTab(0)
        self._tabs.insertTab(0, splitter, "Queue")

    def _view_task(self, task_id: str) -> None:
        """A task row was selected: open it in the core (the pipeline's
        live blocks attribute to it) and render its transcript."""
        if self._shell is not None:
            self._shell.view_task(task_id)
        if self._chat_view is not None:
            self._chat_view.view_task(task_id)

    def _start_shell(self) -> None:
        if self._shell is not None:
            self._shell.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self.close_project()
        super().closeEvent(event)

    # -- pickers ---------------------------------------------------------------

    def _open_project_settings(self) -> None:
        """Settings → "Project settings…": the modal form over the
        project's .drcode/settings.json (the TUI's Task-settings
        dialog twin). Nothing persists until the dialog's Apply."""
        if self._shell is not None:
            ProjectSettingsDialog(self._shell, self).exec()

    def _open_model_settings(self) -> None:
        """Settings → "Models…": the per-role model selection /
        max_tokens form over the user catalog and the project's
        drcode.json (the TUI's Project-model-settings twin)."""
        if self._shell is not None:
            ModelSettingsDialog(self._shell, self).exec()

    def _pick_project(self) -> None:
        start = self._config.last or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Open project folder", start)
        if chosen:
            self.open_project(Path(chosen))

    def _show_recents(self) -> None:
        self._recents_menu.exec(self.cursor().pos())

    def _fill_recents_menu(self) -> None:
        self._recents_menu.clear()
        for entry in self._config.recent:
            action = QAction(entry, self)
            action.triggered.connect(
                lambda _checked=False, path=entry: self.open_project(Path(path))
            )
            self._recents_menu.addAction(action)
        self._recent_button.setEnabled(bool(self._config.recent))

    # -- console slots (the debug console IS the task-1 event view) --------------

    def _console_append(self, line: str) -> None:
        self._console.appendPlainText(line)

    def _on_task_updated(self, task_id: str, status: str) -> None:
        self._console.appendPlainText(f"task_updated  {task_id}  {status}")

    def _on_stream_delta(self, task_id: str, text: str) -> None:
        self._console.appendPlainText(f"stream_delta  {task_id}  +{len(text)} chars")

    def _on_service_event(self, task_id: str, kind: str, text: str) -> None:
        self._console.appendPlainText(f"service_event  [{kind}]  {task_id}  {text}")

    def _on_notify(self, message: str, title: str, severity: str) -> None:
        self._console.appendPlainText(f"notify({severity})  {title}: {message}")


def main() -> int:
    app = QApplication(sys.argv)
    # The pipeline's asyncio loop rides the Qt event loop: one thread
    # for timers, workers and UI callbacks (the Textual threading model).
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    config = GuiConfig()
    window = MainWindow(config)
    window.show()
    if config.last and Path(config.last).is_dir():
        window.open_project(Path(config.last))

    with loop:
        return app.exec()


if __name__ == "__main__":
    sys.exit(main())
