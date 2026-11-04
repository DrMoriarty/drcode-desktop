"""Read-only todo checklist panel of the chat pane (plan task 7).

The GUI port of :mod:`drcode.tui.todo_panel` + the
``refresh_todo_panel`` rendering from ``tui/sidebar_view``: a
collapsible strip between the chat history and the user input that
renders the viewed task's ``task.todos`` checklist. Strictly read-only —
the agent owns the list (todo_create / todo_update tools, feature tasks
only), the user just watches it.

Visibility: hidden while there is nothing to show (no viewed task, no
todos). Collapse state: the header line is clickable and toggles the
checklist between the collapsed counter line (``todos N/N``) and the
full item list. The default state matches the TUI behaviour: a fully
completed checklist auto-collapses (its item signal is spent), anything
else is expanded; a manual click overrides that — session-only, reset
when another task is viewed. Done items render struck-through-dim,
unchecked ones in the normal text color.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from drcode.models import TodoItem

_HEADER_BG = "#1d2027"
_ACCENT = "#3b8eea"
_DIM = "#6b7280"


class TodoPanel(QWidget):
    """The collapsible checklist strip of the chat pane."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task_id: str | None = None
        self._user_collapsed: bool | None = None
        self._todos: list[TodoItem] = []

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 0)
        self._layout.setSpacing(0)
        self._header = QLabel()
        self._header.setTextFormat(Qt.TextFormat.RichText)
        self._header.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self._header.linkActivated.connect(lambda _href: self.toggle_collapsed())
        self._header.setStyleSheet(
            f"background-color: {_HEADER_BG};"
            f"border-left: 3px solid {_ACCENT};"
            "padding: 3px 6px;"
        )
        self._body = QLabel()
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setWordWrap(True)
        self._body.setStyleSheet("padding: 2px 6px;")
        self._layout.addWidget(self._header)
        self._layout.addWidget(self._body)
        self.hide()

    # -- state -------------------------------------------------------------------

    @property
    def task_id(self) -> str | None:
        """The task the current render belongs to (a manual collapse
        override lives only within one task's view)."""
        return self._task_id

    @property
    def user_collapsed(self) -> bool | None:
        """The manual collapse override (None = follow the auto rule)."""
        return self._user_collapsed

    # -- rendering ---------------------------------------------------------------

    def update_todos(self, task_id: str | None, todos: list[TodoItem]) -> None:
        """Re-render from the viewed task's checklist (the TUI's
        ``refresh_todo_panel``). A task switch resets the manual collapse
        override; an empty list hides the panel."""
        if task_id != self._task_id:
            self._task_id = task_id
            self._user_collapsed = None
        self._todos = list(todos)
        if task_id is None or not self._todos:
            self.hide()
            return
        done = len(self._todos) - sum(1 for t in self._todos if not t.done)
        collapsed = (
            self._user_collapsed
            if self._user_collapsed is not None
            else done == len(self._todos)
        )
        self._header.setText(
            f'<a href="#toggle" style="color:{_ACCENT};text-decoration:none">'
            f"<b>todos</b> <span style='color:{_DIM}'>{done}/{len(self._todos)}"
            f" {'▸' if collapsed else '▾'}</span></a>"
        )
        if collapsed:
            self._body.hide()
        else:
            rows: list[str] = []
            for i, item in enumerate(self._todos, 1):
                text = _escape(item.text)
                if item.done:
                    rows.append(
                        f"<span style='color:{_DIM};text-decoration:line-through'>"
                        f"&nbsp;&nbsp;[x] {i}. {text}</span>"
                    )
                else:
                    rows.append(f"&nbsp;&nbsp;[ ] {i}. {text}")
            self._body.setText("<br>".join(rows))
            self._body.show()
        self.show()

    def toggle_collapsed(self) -> None:
        """The header click: invert the collapse state (the first click
        on an untouched panel inverts its DEFAULT state — the TUI's
        ``action_toggle_todo_panel``)."""
        if self._task_id is None or not self._todos:
            return
        done = len(self._todos) - sum(1 for t in self._todos if not t.done)
        if self._user_collapsed is None:
            self._user_collapsed = done != len(self._todos)
        else:
            self._user_collapsed = not self._user_collapsed
        self.update_todos(self._task_id, self._todos)

    def shutdown(self) -> None:
        """Project closed: drop the state (the widget itself dies with
        the chat pane's teardown)."""
        with contextlib.suppress(Exception):
            self._header.linkActivated.disconnect()
        self._todos = []
        self._task_id = None
        self.hide()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
