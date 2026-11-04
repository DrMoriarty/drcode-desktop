"""The GUI event hub: the observable seam between the core and the shell.

The plan's four event kinds — ``task_updated``, ``stream_delta``,
``service_event``, ``notify`` — surface here as Qt signals so any
widget can subscribe with plain ``connect()``; everything emitted is
also appended to a per-project log file, so a headless session still
leaves a readable trace.

Threading: the pipeline runs on ONE thread (the qasync loop riding the
Qt event loop — the same single-thread model as the Textual app), so
signals emitted on the loop thread are delivered synchronously. The few
core callbacks that may fire from daemon threads (the notifier's
delivery thread, worktree snapshot threads) still land safely: Qt
auto-queues a signal into the receiver's thread when the emitter lives
in another one.
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

_LOG_STAMP = "%Y-%m-%d %H:%M:%S"


class EventHub(QObject):
    """Qt signal mirror of the core's event seam + a file log sink."""

    #: A task changed in a way the UI should re-render (status, queue,
    #: events). Carries the task id and its current status value.
    task_updated = Signal(str, str)

    #: One streaming delta of a live model response.
    stream_delta = Signal(str, str)

    #: A persisted service-event line (kind, text) for a task.
    service_event = Signal(str, str, str)

    #: A toast-worthy notification (message, title, severity).
    notify = Signal(str, str, str)

    #: A task was deleted entirely (storage + memory) — the queue model
    #: and the open chat pane drop their rows/views for it. Carries the id.
    task_removed = Signal(str)

    #: A diagnostics line from the host log.
    host_log = Signal(str)

    def __init__(self, log_path: Path | None = None) -> None:
        super().__init__()
        self._log_path = log_path
        self._log_lock = threading.Lock()
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- file log ------------------------------------------------------------

    def append_log(self, line: str) -> None:
        """Append one line to the session log file (never fatal)."""
        if self._log_path is None:
            return
        stamp = datetime.datetime.now().strftime(_LOG_STAMP)
        try:
            with self._log_lock, self._log_path.open("a", encoding="utf-8") as f:
                f.write(f"{stamp} {line}\n")
        except OSError:
            # Logging must never break the pipeline.
            pass

    # -- emitters used by the shell/host -------------------------------------

    def emit_task_updated(self, task_id: str, status: str) -> None:
        self.append_log(f"task_updated {task_id} -> {status}")
        self.task_updated.emit(task_id, status)

    def emit_stream_delta(self, task_id: str, text: str) -> None:
        self.append_log(f"stream_delta {task_id}: {text!r}")
        self.stream_delta.emit(task_id, text)

    def emit_service_event(self, task_id: str, kind: str, text: str) -> None:
        self.append_log(f"service_event {task_id} [{kind}] {text}")
        self.service_event.emit(task_id, kind, text)

    def emit_task_removed(self, task_id: str) -> None:
        self.append_log(f"task_removed {task_id}")
        self.task_removed.emit(task_id)

    def emit_notify(self, message: str, title: str, severity: str) -> None:
        self.append_log(f"notify [{severity}] {title}: {message}")
        self.notify.emit(message, title, severity)

    def emit_host_log(self, message: str) -> None:
        self.append_log(f"log {message}")
        self.host_log.emit(message)


def log_path_for(project_root: Path) -> Path:
    """Session log location: the GUI's own config dir, NOT the shared
    project ``.drcode/`` — a parallel TUI owns that directory's layout,
    and the GUI must not add files to it."""
    base = _gui_config_dir()
    return base / "logs" / f"{project_root.name}.log"


def _gui_config_dir() -> Path:
    import os

    override = os.environ.get("DRCODE_DESKTOP_CONFIG_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "drcode-desktop"
