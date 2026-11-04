"""The unmerged-branches screen (plan task 9) — the GUI port of the
TUI's ``tui.unmerged_screen.UnmergedListScreen``: a modal list with one
row per task whose ``drcode/<id>`` branch still holds commits the base
branch does not have (or whose merge is parked as a conflict).

Each row carries the merge state, computed fresh from git on every
refresh (never from a cached snapshot):

- ``conflict`` — the engine parked a conflicted merge in the task's
  worktree: ``Resolve…`` opens the merge screen (the hunk decisions);
- ``staged`` — the merge is resolved and staged but not concluded:
  ``Merge / commit`` commits it on the task branch;
- ``ff-ready`` — nothing is left to resolve: ``Merge / commit``
  fast-forwards the MAIN checkout's base branch to ``drcode/<id>``
  (``--ff-only`` behind the ancestor re-verification — the contract's
  user-triggered half);
- ``unmerged`` — a plain unmerged branch: ``Merge / commit`` prepares
  the reversed merge (the engine's merge + triage + auto-resolvers) and
  takes whatever the report vouches for (a clean merge is concluded and
  ff-merged; a conflict is announced — ``Resolve…`` takes over).

Exactly one candidate never reaches this dialog (the app menu opens the
merge screen directly, like the TUI's single-candidate Ctrl+M).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .merge_model import (
    UnmergedEntry,
    collect_unmerged_entries,
)

if TYPE_CHECKING:
    from drcode.merge_engine import MergeReport
    from PySide6.QtWidgets import QWidget

_KIND_COLORS = {
    "conflict": QColor(224, 108, 117),
    "staged": QColor(229, 192, 123),
    "ff": QColor(152, 195, 121),
    "unmerged": QColor(61, 156, 255),
}


class UnmergedDialog(QDialog):
    """The >1-candidates face of the merge entry point: pick a task,
    act on it (the per-task actions are the merge model's own)."""

    def __init__(self, shell: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        self._entries: list[UnmergedEntry] = []
        self._busy = False

        self.setWindowTitle("Unmerged task branches")
        self.resize(760, 420)

        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self.action_resolve())
        hint = QLabel(
            "Resolve… opens the conflict screen · Merge / commit lands the "
            "branch (commit / ff-only / prepare) · r refresh"
        )
        hint.setStyleSheet("color: #6b7280;")

        bar = QHBoxLayout()
        self._resolve_btn = QPushButton("Resolve…")
        self._resolve_btn.clicked.connect(self.action_resolve)
        self._land_btn = QPushButton("Merge / commit")
        self._land_btn.clicked.connect(self.action_land)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bar.addWidget(self._resolve_btn)
        bar.addWidget(self._land_btn)
        bar.addStretch(1)
        bar.addWidget(refresh)
        bar.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)
        layout.addWidget(hint)
        layout.addLayout(bar)

        QShortcut(QKeySequence("R"), self, self._refresh)
        QShortcut(QKeySequence("Return"), self, self.action_resolve)

    # -- data -----------------------------------------------------------------

    def _collect(self) -> list[UnmergedEntry]:
        return collect_unmerged_entries(
            self._shell.project_root,
            self._shell.task_env,
            self._shell.manager.tasks,
        )

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Every open (and every return from the merge screen) recomputes
        the rows — the user acts the moment the list paints."""
        super().showEvent(event)  # type: ignore[arg-type]
        self._refresh()

    def _refresh(self) -> None:
        if self._busy:
            return
        self._entries = self._collect()
        current = self._selected()
        selected = current.task_id if current is not None else None
        self._list.clear()
        for entry in self._entries:
            item = QListWidgetItem(
                f"#{entry.task_id}  {entry.title}   —   "
                f"{entry.branch} · {entry.state_label}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.task_id)
            color = _KIND_COLORS.get(entry.kind)
            if color is not None:
                item.setForeground(color)
            self._list.addItem(item)
        if selected:
            for i, entry in enumerate(self._entries):
                if entry.task_id == selected:
                    self._list.setCurrentRow(i)
                    break
        elif self._entries:
            self._list.setCurrentRow(0)
        has = bool(self._entries)
        self._resolve_btn.setEnabled(has)
        self._land_btn.setEnabled(has)

    def _selected(self) -> UnmergedEntry | None:
        row = self._list.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    # -- actions ----------------------------------------------------------------

    def action_resolve(self) -> None:
        """Open the merge screen for the selected task (the dialog's
        nested exec keeps the list beneath — re-freshened on return by
        the showEvent of the next open)."""
        entry = self._selected()
        if entry is None or self._busy:
            return
        from .merge_screen import MergeScreenDialog

        dialog = MergeScreenDialog(self._shell, entry.task_id, self)
        dialog.exec()
        self._refresh()

    def action_land(self) -> None:
        """The "Merge / commit" dispatch, per row kind — every branch
        lands on the task's own branch, except the explicit ``--ff-only``
        fast-forward of the ff-ready state."""
        entry = self._selected()
        if entry is None or self._busy:
            return
        if entry.kind == "conflict":
            self.action_resolve()
            return
        self._busy = True
        self._resolve_btn.setEnabled(False)
        self._land_btn.setEnabled(False)
        from .merge_model import MergeModel

        model = MergeModel.for_task(self._shell, entry.task_id)
        if entry.kind == "staged":
            job = lambda: model.commit(  # noqa: E731 - a one-line dispatch
                f"Merge branch '{model.base_branch}' into {model.branch} "
                "(resolved, committed from the GUI unmerged list)"
            )
            done = self._commit_done
        elif entry.kind == "ff":
            job = model.ff_merge
            done = self._ff_done
        else:
            job = model.prepare
            done = self._prepare_done
        from .merge_screen import _Worker

        worker = _Worker(self)
        worker.done.connect(self._land_finished)
        worker.done.connect(done)
        worker.run(job, lambda _result: None)
        # Keep a reference: the worker must outlive the call frame.
        self._worker = worker

    def _land_finished(self, result: object) -> None:
        del result
        self._busy = False
        self._resolve_btn.setEnabled(True)
        self._land_btn.setEnabled(True)

    def _commit_done(self, result: object) -> None:
        ok, msg = _pair(result)
        if not ok:
            QMessageBox.warning(self, "Merge", f"commit failed: {msg}")
        self._refresh()

    def _ff_done(self, result: object) -> None:
        ok, msg = _pair(result)
        if not ok:
            QMessageBox.warning(
                self,
                "Merge",
                "git merge --ff-only refused: " + msg,
            )
        else:
            model = self._model_for(self._selected())
            if model is not None:
                model.clear_needs_human()
        self._refresh()

    def _prepare_done(self, result: object) -> None:
        """The unmerged branch was prepared: take what the report
        vouches for — a clean merge is ff-merged right away, a conflict
        is announced (Resolve… opens the screen)."""
        if isinstance(result, Exception):
            QMessageBox.warning(self, "Merge", f"prepare failed: {result}")
            self._refresh()
            return
        report = cast("MergeReport", result)
        if not report.ok:
            QMessageBox.warning(self, "Merge", f"prepare refused: {report.message}")
            self._refresh()
            return
        if not report.conflicted and report.fast_forward is True:
            entry = self._selected()
            if entry is not None:
                from .merge_model import MergeModel

                model = MergeModel.for_task(self._shell, entry.task_id)
                self._busy = True
                from .merge_screen import _Worker

                worker = _Worker(self)
                worker.done.connect(self._land_finished)
                worker.done.connect(self._ff_done)
                worker.run(model.ff_merge, lambda _result: None)
                self._worker = worker
                return
        self._refresh()

    def _model_for(self, entry: UnmergedEntry | None) -> Any:
        if entry is None:
            return None
        from .merge_model import MergeModel

        return MergeModel.for_task(self._shell, entry.task_id)

    # -- tests ------------------------------------------------------------

    def state_for_tests(self) -> list[tuple[str, str]]:
        """The headless inspection seam: (task_id, kind) rows."""
        return [(e.task_id, e.kind) for e in self._entries]


def _pair(result: object) -> tuple[bool, str]:
    if isinstance(result, Exception):
        return False, f"{type(result).__name__}: {result}"
    if isinstance(result, tuple) and len(result) == 2:
        return bool(result[0]), str(result[1])
    return False, str(result)
