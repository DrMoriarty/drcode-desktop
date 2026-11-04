"""The new-task dialog (plan task 5): creation of bug/feature/research
tasks from the GUI.

The GUI twin of the TUI's new-task form header (``tui.actions_forms``):
the condition text (the title derives from its first line), the kind
selector (feature / bug / research), the optional "depends on" field
(space/comma separated task ids, validated by the core's
:func:`drcode.task_manager.validate_dependencies` gate — an invalid list
keeps the dialog open with the error shown, nothing is created) and the
"create on hold" checkbox (the editor's Ctrl+H).

The dialog holds no semantics of its own: submission routes to
:meth:`drcodedesktop.core_shell.CoreShell.create_task` — the shell
re-validates and drives the pipeline, exactly like the TUI form does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from drcode.models import TaskKind
from drcode.task_manager import validate_dependencies
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from .core_shell import CoreShell

#: The kind selector's entries (label, kind) — the TUI form's cycle set.
_KIND_CHOICES: tuple[tuple[str, TaskKind], ...] = (
    ("Feature", TaskKind.FEATURE),
    ("Bug report", TaskKind.BUG),
    ("Research", TaskKind.RESEARCH),
)


class NewTaskDialog(QDialog):
    """Modal "New task" form over ``CoreShell.create_task``."""

    def __init__(self, shell: CoreShell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        self.setWindowTitle("New task")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Kind:"))
        self._kind = QComboBox()
        for label, kind in _KIND_CHOICES:
            self._kind.addItem(label, kind)
        # The shell remembers the last-used kind (the TUI form's
        # new_task_kind) — start there. (PySide6 hands combo userData
        # back as the enum's value string.)
        index = self._kind.findData(shell.new_task_kind.value)
        if index >= 0:
            self._kind.setCurrentIndex(index)
        top.addWidget(self._kind)
        top.addStretch(1)
        layout.addLayout(top)

        layout.addWidget(QLabel("Task:"))
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            "Describe the task — the first line becomes the title."
        )
        self._text.setMinimumHeight(120)
        layout.addWidget(self._text)

        layout.addWidget(QLabel("Depends on (optional):"))
        self._deps = QLineEdit()
        self._deps.setPlaceholderText("task ids, space- or comma-separated")
        layout.addWidget(self._deps)

        self._hold = QCheckBox("Create on hold (parked out of the backlog)")
        layout.addWidget(self._hold)

        # The validation-error line (the TUI form's #nt-deps-note): a
        # violation keeps the dialog open with the message shown.
        self._note = QLabel("")
        self._note.setStyleSheet("color: #cd3131;")
        layout.addWidget(self._note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        submit = QPushButton("Create")
        submit.setDefault(True)
        submit.clicked.connect(self._submit)
        buttons.addWidget(submit)
        layout.addLayout(buttons)

    # -- submission -----------------------------------------------------------

    def _submit(self) -> bool:
        """Validate the form and create the task. Returns True when the
        dialog should close (the task exists now)."""
        text = self._text.toPlainText().strip()
        if not text:
            self._note.setText("Task text is empty.")
            return False
        deps = self._deps_from_form()
        if deps is None:
            return False
        kind = TaskKind(str(self._kind.currentData()))
        task = self._shell.create_task(
            text, kind=kind, hold=self._hold.isChecked(), depends_on=deps
        )
        if task is None:
            # Defensive: the shell re-validates; a refusal here keeps the
            # form open like a validation error would.
            self._note.setText("The task was not created.")
            return False
        # The kind choice is remembered for the next task (the TUI
        # form's behaviour).
        self._shell.new_task_kind = kind
        self.accept()
        return True

    def _deps_from_form(self) -> list[str] | None:
        """Parse + validate the Depends-on field.

        Returns the cleaned id list, or None on a violation — the error
        is shown in the note label so the user can fix it without losing
        the composed text. The same ``validate_dependencies`` call the
        TUI form makes (creation time: task_id=None — self-reference and
        cycles are impossible for a task that does not exist yet)."""
        raw = self._deps.text()
        parts = raw.replace(",", " ").split()
        if not parts:
            self._note.setText("")
            return []
        cleaned: list[str] = []
        for part in parts:
            dep = part.lstrip("#")
            if dep:
                cleaned.append(dep)
        try:
            return validate_dependencies(self._shell.manager.tasks, None, cleaned)
        except ValueError as e:
            self._note.setText(str(e))
            return None
