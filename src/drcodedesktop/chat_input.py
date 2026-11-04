"""The chat input (plan task 4): the message composer of the chat pane.

The GUI twin of the TUI's ``ChatInput`` (``tui/inputs.py``) keyboard
contract, on a plain QPlainTextEdit:

- **Enter** submits the message (the full text — a multi-line draft of
  clarification answers is submitted as ONE message, exactly like the
  TUI's editor); **Shift+Enter** inserts a newline;
- **Esc** requests a run stop (the TUI's ESC binding);
- **Ctrl+R** requests a run resume (the TUI's nudge binding).

The widget emits signals only; the state-changing semantics live in
:class:`drcodedesktop.core_shell.CoreShell` (``submit_user_text`` /
``stop_viewed_run`` / ``resume_viewed_run``) — the view routes to them.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QPlainTextEdit

_ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
_SHIFT = Qt.KeyboardModifier.ShiftModifier
_CONTROL = Qt.KeyboardModifier.ControlModifier


class ChatInput(QPlainTextEdit):
    """The message composer: Enter sends, Shift+Enter breaks a line."""

    #: The user pressed Enter — submit the current text (raw; the
    #: receiver strips and decides, the accepted submit clears it).
    submitted = Signal(str)

    #: Esc — stop the viewed task's run.
    stop_requested = Signal()

    #: Ctrl+R — resume/restart the viewed task's run.
    resume_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(64)
        self.setPlaceholderText(
            "Message the task…  (Enter — send, Shift+Enter — newline)"
        )

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        modifiers = event.modifiers()
        if key in _ENTER_KEYS and not modifiers & _SHIFT:
            # The full draft goes out as ONE message (a clarification
            # package's per-question lines must reach the agent merged,
            # exactly like the TUI's Enter).
            self.submitted.emit(self.toPlainText())
            return
        if key == Qt.Key.Key_Escape:
            self.stop_requested.emit()
            return
        if key == Qt.Key.Key_R and modifiers & _CONTROL:
            self.resume_requested.emit()
            return
        super().keyPressEvent(event)
