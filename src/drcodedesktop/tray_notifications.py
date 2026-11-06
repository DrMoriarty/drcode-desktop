"""System-tray notifications about pipeline events (the GUI's own
channel, independent of the project's ntfy/voice settings).

Two cooperating pieces:

- :class:`TrayNotifier` — the Qt half. It owns a
  :class:`QSystemTrayIcon` (visible while the shell has a project open
  AND at least one notification kind is enabled), decides per event
  whether the user's checkboxes want it, formats the text and shows the
  balloon — unless the main window is currently active (the user is
  looking at the pipeline anyway). Event sources:

  - task transitions come from a GUI-own manager status listener
    (registered by :class:`drcodedesktop.core_shell.CoreShell`), filtered
    to system-origin transitions — a user-caused change is something the
    user just did and does not need to be told about;
  - the settled pipeline states (queue empty / blocked) ride the
    pipeline's existing ``notifier.pipeline_idle`` calls — see
    :class:`TrayEventBridge` — with the same prime-at-startup and
    change-only dedup semantics the ntfy push path uses
    (``_notifications_primed`` in the shell).

- :class:`TrayEventBridge` — a thin :class:`drcode.notifications.Notifier`
  subclass that forwards the pipeline's idle/busy transitions to the
  tray AFTER delegating to the base class untouched: the ntfy behaviour
  of the GUI shell stays exactly as it is (per-task ntfy pushes are not
  wired in the shell — only the settled states ever reach the notifier),
  the tray simply piggybacks on the same calls.

Message texts reuse the core's own wording: the human-readable status
one-liner from ``drcode.notifications._status_text`` and the queue-empty
phrase from ``drcode.voice`` (in the project's announcement language).
No new formulations are invented here.

Threading: the manager's status listeners may fire from worker threads
(the tester's verdict path), while every QSystemTrayIcon call must stay
on the GUI thread — the transition path therefore emits a queued Qt
signal and only the ``_deliver`` slot touches the tray. The settled path
always runs on the loop thread (the pipeline drives it).
"""

from __future__ import annotations

from collections.abc import Callable

from drcode.models import Task, TaskStatus, VerificationStatus
from drcode.notifications import Notifier, _status_text
from drcode.settings import ProjectSettings
from drcode.voice.config import (
    VOICE_LANGUAGE_EN,
    announcement_phrase,
    effective_voice_language,
)
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon, QWidget

from .gui_config import GuiConfig

#: How long a balloon stays up (ms) — short enough not to pile up.
_MESSAGE_MS = 8000

#: GuiConfig checkbox attribute per notification kind.
_NOTIFY_FIELDS = {
    "completed": "notify_completed",
    "tester_failed": "notify_tester_failed",
    "clarification": "notify_clarification",
    "pipeline": "notify_pipeline",
}


class TrayNotifier(QObject):
    """The tray icon + the show/suppress/filter decisions (see module)."""

    #: Marshals a notification onto the GUI thread (transitions may
    #: originate in worker threads): title, body.
    _show_requested = Signal(str, str)

    def __init__(self, config: GuiConfig, window: QWidget) -> None:
        super().__init__(window)
        self._config = config
        self._window = window
        self._project: str | None = None
        self._settings_getter: Callable[[], ProjectSettings] | None = None
        # Dedup of the settled states (the notifier's _settled_state
        # semantics mirrored here: the tray must not depend on the ntfy
        # checkboxes being on to keep its own state machine).
        self._settled_state: str | None = None
        self._tray = QSystemTrayIcon(self._icon(), self)
        self._tray.setToolTip("DrCode Desktop")
        self._show_requested.connect(self._deliver)

    # -- configuration -----------------------------------------------------------

    @property
    def any_enabled(self) -> bool:
        """At least one notification kind is switched on."""
        return any(
            getattr(self._config, field) for field in _NOTIFY_FIELDS.values()
        )

    def set_settings_getter(self, getter: Callable[[], ProjectSettings]) -> None:
        """Bind the live project settings (CoreShell owns them) — the
        announcement language of the queue-empty phrase follows the
        project, like the core's own phrases do."""
        self._settings_getter = getter

    def set_project(self, project: str | None) -> None:
        """A project opened (name) or closed (None): the tray icon only
        makes sense with a live pipeline to report about."""
        self._project = project
        self._update_visibility()

    def refresh(self) -> None:
        """The Preferences dialog applied new checkboxes: re-read the
        flags (they are read live everywhere else) and fix visibility."""
        self._update_visibility()

    def _update_visibility(self) -> None:
        self._tray.setVisible(self._project is not None and self.any_enabled)

    def _icon(self) -> QIcon:
        """The tray icon: the application's own when it has one, a
        generic desktop icon otherwise (fresh dev checkouts)."""
        icon = self._window.windowIcon()
        if not icon.isNull():
            return icon
        return QApplication.style().standardIcon(
            QStyle.StandardPixmap.SP_ComputerIcon
        )

    # -- task transitions (the manager status listener) ----------------------------

    def task_transition(
        self, task: Task, old: TaskStatus, new: TaskStatus, origin: str
    ) -> None:
        """A task changed status (possibly from a worker thread): report
        the system-origin events the checkboxes ask for. User-origin
        transitions are never reported — the user just caused one."""
        if origin == "user" or old is new:
            return
        kind = self._classify(task, old, new)
        if kind is None:
            return
        if not getattr(self._config, _NOTIFY_FIELDS[kind]):
            return
        title = self._title()
        body = f'#{task.id} "{task.title}" — {_status_text(new, task)}'
        # Queued into the GUI thread: the tray must only be touched there.
        self._show_requested.emit(title, body)

    @staticmethod
    def _classify(task: Task, old: TaskStatus, new: TaskStatus) -> str | None:
        """The notification kind of a transition (the same mapping the
        core's ``Notifier.task_status`` uses for its events): completions
        (a FAILED verdict counts as a tester failure), the tester
        returning a task to the queue, and clarifications."""
        if new is TaskStatus.COMPLETED:
            return (
                "tester_failed"
                if task.verification is VerificationStatus.FAILED
                else "completed"
            )
        if new is TaskStatus.QUEUED and old is TaskStatus.TESTING:
            return "tester_failed"
        if new is TaskStatus.AWAITING_CLARIFICATION:
            return "clarification"
        return None

    # -- settled pipeline states (fed by TrayEventBridge) ----------------------------

    def settled(self, *, blocked: bool, detail: str, prime: bool) -> None:
        """The pipeline settled with nothing to run (loop thread). The
        first evaluation only primes the state (a cold start on an empty
        queue must not announce "queue is empty"); afterwards only a
        CHANGE of the settled state is reported."""
        state = "blocked" if blocked else "empty"
        if prime:
            self._settled_state = state
            return
        if state == self._settled_state:
            return
        self._settled_state = state
        if not self._config.notify_pipeline:
            return
        title = self._title()
        if blocked:
            body = f"No tasks can be taken into development. {detail}".rstrip()
        else:
            body = self._phrase("queue_empty")
        self._show_requested.emit(title, body)

    def reset_busy(self) -> None:
        """A run started: forget the last settled state so the next
        settle is reported again (the notifier's pipeline_busy rule)."""
        self._settled_state = None

    # -- delivery -----------------------------------------------------------------

    def _title(self) -> str:
        """The notification title: the app name, project-suffixed like
        the core notifier's titles (several drcode instances must stay
        distinguishable)."""
        if self._project:
            return f"DrCode Desktop — {self._project}"
        return "DrCode Desktop"

    def _phrase(self, key: str, **kwargs: object) -> str:
        """A phrase from the core's announcement table, in the project's
        announcement language (English before a project is bound)."""
        if self._settings_getter is None:
            return announcement_phrase(VOICE_LANGUAGE_EN, key, **kwargs)
        return announcement_phrase(
            effective_voice_language(self._settings_getter()), key, **kwargs
        )

    def _deliver(self, title: str, body: str) -> None:
        """The GUI-thread delivery slot: the balloon is suppressed while
        the main window is active — the user is looking at the pipeline
        right now."""
        if self._window.isActiveWindow():
            return
        self._tray.showMessage(
            title,
            body,
            QSystemTrayIcon.MessageIcon.Information,
            _MESSAGE_MS,
        )


class TrayEventBridge(Notifier):
    """The shell's :class:`Notifier` with tray taps on the pipeline
    settle/busy transitions.

    Every method delegates to the base class FIRST (the ntfy behaviour
    of the GUI shell — the settled-state pushes it already sends — is
    untouched), then forwards the same event to the tray. Per-task
    events do NOT go through here: the shell does not wire
    ``notifier.task_status`` (no per-task ntfy pushes in the GUI), the
    tray listens to the manager itself instead.
    """

    def __init__(self, tray: TrayNotifier, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._tray = tray

    def pipeline_idle(
        self, *, blocked: bool, detail: str, prime: bool = False
    ) -> None:
        super().pipeline_idle(blocked=blocked, detail=detail, prime=prime)
        self._tray.settled(blocked=blocked, detail=detail, prime=prime)

    def pipeline_busy(self) -> None:
        super().pipeline_busy()
        self._tray.reset_busy()
