"""The live task-queue screen (plan task 2): a QTreeWidget mirroring the
TUI sidebar's sections and rows, refreshed in real time.

Structure: one bold top-level item per non-empty section (``- QUEUE (3)``,
the TUI's collapse-glyph convention) with the section's task rows as
children. Columns: id, kind (the colored B/R/F letter), status (marker +
value), verification, flags (unfinished / needs-human / hold /
cancelled) and the title — the plan's column set.

Updates arrive two ways, both pushed by the model
(:class:`~drcodedesktop.task_queue_model.TaskQueueModel`): the hub's
``task_updated`` events (the real-time path) and the 1 s fingerprint
poll that catches flag mutations without a status transition (the TUI
sidebar's own dual scheme). Read-only: no task actions live here (plan
task 5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .task_queue_model import TaskQueueModel

if TYPE_CHECKING:
    from .core_shell import CoreShell
    from .event_hub import EventHub

_COLUMNS = ("id", "kind", "status", "verification", "flags", "title")

_KIND_ROLE_COLORS = {
    "red": QColor(224, 108, 117),
    "yellow": QColor(229, 192, 123),
    "green": QColor(152, 195, 121),
}


class TaskQueueView(QWidget):
    """The queue tab: sections + task rows, live."""

    #: The user selected a task row (plan task 3: the chat pane follows
    #: the selection). Carries the task id.
    task_activated = Signal(str)

    def __init__(self, shell: CoreShell, hub: EventHub) -> None:
        super().__init__()
        self._model = TaskQueueModel(shell, hub)
        self._shell = shell

        self._tree = QTreeWidget()
        self._tree.setColumnCount(len(_COLUMNS))
        self._tree.setHeaderLabels(_COLUMNS)
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        # Read-only screen: the rows select for viewing (the chat pane
        # rides along); no editing, no context actions.
        self._tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self._tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self._tree.currentItemChanged.connect(self._on_current_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tree)

        self._model.rows_changed.connect(self.rebuild)
        self.rebuild()

        # The safety-net poll (flags mutating without a status event —
        # the same gap the TUI's sidebar poller covers).
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._model.refresh)
        self._poll.start()

    def row_item(self, task_id: str) -> QTreeWidgetItem | None:
        """The tree item of a task row (tests, the chat-pane link)."""
        return self._items.get(task_id)

    def _on_current_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        task_id = current.data(0, Qt.ItemDataRole.UserRole)
        if task_id:
            self.task_activated.emit(task_id)

    def select_task(self, task_id: str) -> None:
        """Programmatic selection (a mention link in the chat pane): the
        current-item change re-emits task_activated — one path for both."""
        item = self._items.get(task_id)
        if item is not None:
            self._tree.setCurrentItem(item)

    def rebuild(self) -> None:
        """Redraw the whole tree from the model. The queue is small
        (hundreds of rows at most, the retention cap bounds COMPLETED);
        a plain rebuild keeps the code honest — the TUI's incremental
        reconcile exists because Textual re-mount is expensive, a
        QTreeWidget rebuild is not. The selection survives the rebuild
        (restored with signals blocked — a re-emit would ping-pong the
        chat pane's view_task)."""
        current = self._tree.currentItem()
        current_id = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        self._tree.blockSignals(True)
        self._tree.clear()
        self._items: dict[str, QTreeWidgetItem] = {}
        for section in self._model.sections():
            header = QTreeWidgetItem([section.header(), "", "", "", "", ""])
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = header.font(0)
            font.setBold(True)
            header.setFont(0, font)
            self._tree.addTopLevelItem(header)
            header.setExpanded(True)
            for row in section.rows:
                item = QTreeWidgetItem(
                    [
                        f"#{row.task_id}",
                        row.kind_letter,
                        row.status_label,
                        (
                            f"{row.verification_glyph} {row.verification}"
                            if row.verification
                            else ""
                        ),
                        " ".join(row.flag_fragments()),
                        row.title,
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, row.task_id)
                item.setToolTip(5, row.label)
                color = _KIND_ROLE_COLORS.get(row.kind_color)
                if color is not None:
                    item.setForeground(1, QBrush(color))
                if row.needs_human:
                    item.setForeground(4, QBrush(QColor(224, 108, 117)))
                elif row.unfinished:
                    item.setForeground(4, QBrush(QColor(229, 192, 123)))
                header.addChild(item)
                self._items[row.task_id] = item
        self._tree.setColumnWidth(0, 90)
        self._tree.setColumnWidth(1, 40)
        self._tree.setColumnWidth(2, 120)
        self._tree.setColumnWidth(3, 220)
        self._tree.setColumnWidth(4, 180)
        if current_id and current_id in self._items:
            self._tree.setCurrentItem(self._items[current_id])
        self._tree.blockSignals(False)

    def shutdown(self) -> None:
        """Detach from the core (project closed): stop the poll and
        disconnect the model from the hub so no signal fires into a dead
        shell."""
        self._poll.stop()
        self._model.detach()
        self._model.rows_changed.disconnect(self.rebuild)
        self._tree.clear()
        self._items = {}
