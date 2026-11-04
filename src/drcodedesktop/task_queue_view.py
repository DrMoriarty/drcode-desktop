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
sidebar's own dual scheme).

Plan task 5 adds the action half: the "New task…" header button opens
:class:`~drcodedesktop.new_task_dialog.NewTaskDialog`, and a context
menu on a task row exposes the lifecycle commands (hold / resume,
queue moves, dependency add/remove, cancel, delete) — all routed to
:meth:`drcodedesktop.core_shell.CoreShell` (the semantics live in the
shell, the menu only forwards).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from drcode import merge_engine
from drcode.models import TaskStatus
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .activity_view import ActivityIndicator
from .merge_screen import MergeScreenDialog
from .new_task_dialog import NewTaskDialog
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
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)

        header = QHBoxLayout()
        header.setContentsMargins(4, 4, 4, 0)
        header.addStretch(1)
        self._new_task_button = QPushButton("New task…")
        self._new_task_button.clicked.connect(self._open_new_task_dialog)
        header.addWidget(self._new_task_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self._tree)
        # The run-activity indicator (plan task 7): the sidebar bottom of
        # the TUI — fresh/stale status, stall countdown, session stats,
        # git unmerged-work counter.
        self._activity = ActivityIndicator(shell)
        layout.addWidget(self._activity)
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

    # -- task actions (plan task 5) ----------------------------------------------

    def _open_new_task_dialog(self) -> None:
        """The "New task…" button: the creation dialog (modal); the
        dialog routes the submission to the shell itself."""
        dialog = NewTaskDialog(self._shell, self)
        dialog.exec()

    def _on_context_menu(self, pos: QPoint) -> None:
        """The row context menu: the lifecycle commands available for the
        task's CURRENT status (the guards live in the shell — the menu
        only hides what cannot apply)."""
        item = self._tree.itemAt(pos)
        if item is None:
            return
        task_id = item.data(0, Qt.ItemDataRole.UserRole)
        task = self._shell.manager.tasks.get(task_id or "")
        if task is None:
            return
        menu = QMenu(self)
        if task.status in (TaskStatus.QUEUED, TaskStatus.AWAITING_CLARIFICATION):
            menu.addAction("Hold", lambda: self._run_action("hold", task.id))
        elif task.status is TaskStatus.HOLD:
            menu.addAction("Resume", lambda: self._run_action("resume", task.id))
        if task.status is TaskStatus.QUEUED:
            queue = self._shell.manager.queued()
            index = queue.index(task) if task in queue else -1
            move_up = menu.addAction("Move up (away from execution)")
            move_up.setEnabled(index > 0)
            move_up.triggered.connect(lambda: self._run_action("move_up", task.id))
            move_down = menu.addAction("Move down (closer to execution)")
            move_down.setEnabled(0 <= index < len(queue) - 1)
            move_down.triggered.connect(lambda: self._run_action("move_down", task.id))
        if self._shell.can_edit_dependencies(task):
            add = menu.addAction("Add dependency…")
            add.triggered.connect(lambda: self._prompt_add_dependency(task.id))
            if task.depends_on:
                deps_menu = menu.addMenu("Remove dependency")
                for dep in task.depends_on:
                    deps_menu.addAction(
                        f"#{dep}",
                        lambda dep=dep: self._run_action("remove_dep", task.id, dep),
                    )
        if task.status is not TaskStatus.COMPLETED:
            menu.addAction("Cancel task", lambda: self._run_action("cancel", task.id))
        if task.status is TaskStatus.HOLD:
            menu.addAction("Delete task", lambda: self._run_action("delete", task.id))
        # The parked-conflict entry (plan task 9): the task the resolver
        # gave up on (needs_human) — or one with a live in-progress merge
        # — opens the conflict screen straight from the list.
        if task.needs_human or self._merge_parked(task.id):
            menu.addAction(
                "Resolve merge conflicts…",
                lambda: self._open_merge_screen(task.id),
            )
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _merge_parked(self, task_id: str) -> bool:
        """A live parked merge in the task's worktree (one cheap git
        call, only when the menu is actually opened for the row)."""
        env = self._shell.task_env
        return env.active and merge_engine.in_progress(env.worktree_path(task_id))

    def _open_merge_screen(self, task_id: str) -> None:
        """The needs_human → screen edge from the task list."""
        if not self._shell.task_env.active:
            QMessageBox.warning(
                self,
                "Merge",
                "The conflict screen needs worktree isolation "
                "(project setting `task_isolation: worktree`).",
            )
            return
        MergeScreenDialog(self._shell, task_id, self).exec()

    def _prompt_add_dependency(self, task_id: str) -> None:
        """The "Add dependency…" input: one task id (the ``#id`` mention
        form tolerated — the shell normalises it). The shell surfaces a
        validation failure as a notify."""
        text, ok = QInputDialog.getText(self, "Add dependency", "Task id to wait for:")
        if ok and text.strip():
            self._run_action("add_dep", task_id, text)

    def _run_action(self, action: str, task_id: str, dep_id: str = "") -> None:
        """One lifecycle command → the shell (the single seam the tests
        drive; the menu actions are thin lambdas over this)."""
        shell = self._shell
        if action == "hold":
            shell.hold_task(task_id)
        elif action == "resume":
            shell.resume_task(task_id)
        elif action == "cancel":
            shell.cancel_task(task_id)
        elif action == "delete":
            shell.delete_task(task_id)
        elif action == "move_up":
            shell.move_task_in_queue(task_id, up=True)
        elif action == "move_down":
            shell.move_task_in_queue(task_id, up=False)
        elif action == "add_dep":
            shell.add_dependency(task_id, dep_id)
        elif action == "remove_dep":
            shell.remove_dependency(task_id, dep_id)
        else:  # pragma: no cover - a programming error, not a user path
            raise ValueError(f"unknown task action: {action}")

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
        self._activity.shutdown()
        self._model.detach()
        self._model.rows_changed.disconnect(self.rebuild)
        self._tree.clear()
        self._items = {}
