"""The project-settings dialog (plan task 6): the GUI twin of the TUI's
``tui.task_settings_dialog`` — the single modal form for the
task-management project preferences.

The same semantics, ported to Qt widgets:

- works on a DRAFT (a deep copy of the saved settings): nothing is
  persisted until Apply (Cancel/ESC dismisses and discards everything);
- groups the field widgets into the three sections of the TUI dialog
  (Workflow / Testing / Git) and marks nothing silently: every value the
  user picks is persisted verbatim, even when it is currently dormant;
- re-renders a hint panel on every draft change from the ONE shared
  source of the coupling rules — :func:`drcode.settings.task_settings_hints`
  (the same function the TUI dialog uses), with an explicit fix button
  per hint the user must press (the user's choice is never silently
  rewritten).

On Apply the draft is handed to
:meth:`drcodedesktop.core_shell.CoreShell.apply_task_settings`, which
merges it into the live settings object, persists it once into
``.drcode/settings.json`` — the very file the TUI writes — and replays
the pipeline-gate / keep-awake side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from drcode.settings import (
    DEV_ROUNDS_OPTIONS,
    ESC_ABORT_OPTIONS,
    GIT_POLICY_OPTIONS,
    ISOLATION_OPTIONS,
    PARALLEL_TASKS_OPTIONS,
    RETENTION_OPTIONS,
    STALL_NUDGE_OPTIONS,
    SUBAGENTS_OPTIONS,
    VERIFICATION_OPTIONS,
    WORKTREE_REMOVAL_OPTIONS,
    WORKTREE_SNAPSHOT_OPTIONS,
    CompletedRetention,
    EscAbort,
    GitPolicy,
    ProjectSettings,
    StallNudge,
    TaskIsolation,
    Verification,
    WorktreeRemoval,
    WorktreeSnapshot,
    dev_rounds_label,
    parallel_tasks_label,
    subagents_label,
    task_settings_hints,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from drcode.settings import SettingsHint

    from .core_shell import CoreShell


def copy_task_settings(dst: ProjectSettings, src: ProjectSettings) -> None:
    """Copy the task-management fields of `src` into `dst` IN PLACE.

    Port of ``tui.task_settings_dialog.copy_task_settings``: the live
    settings object is shared (the TaskEnvironment holds the same
    instance so isolation toggles propagate to the next activation), so
    an accepted draft is merged field-by-field instead of replacing the
    object. Deliberately only the fields this dialog edits — the rest of
    ProjectSettings (GitHub, voice, role_max_tokens, …) is untouched,
    which keeps the GUI and the TUI dialogs interchangeable.
    """
    dst.completed_retention = src.completed_retention
    dst.verification = src.verification
    dst.git_policy.tester = src.git_policy.tester
    dst.git_policy.research = src.git_policy.research
    dst.max_dev_rounds = src.max_dev_rounds
    dst.stall_nudge = src.stall_nudge
    dst.task_isolation = src.task_isolation
    dst.parallel_tasks = src.parallel_tasks
    dst.subagents_coder = src.subagents_coder
    dst.subagents_researcher = src.subagents_researcher
    dst.agent_creates_drafts = src.agent_creates_drafts
    dst.generate_task_titles = src.generate_task_titles
    dst.worktree_removal = src.worktree_removal
    dst.worktree_snapshot = src.worktree_snapshot
    dst.isolation_start_branch = src.isolation_start_branch
    dst.keep_awake = src.keep_awake


# -- widget helpers ------------------------------------------------------------


def fill_enum_combo(options: tuple) -> QComboBox:
    """A combo whose items carry the enum VALUES as userData (PySide6
    hands them back as plain strings — the caller reads them back with
    ``EnumType(currentData())``)."""
    combo = QComboBox()
    for option in options:
        combo.addItem(option.label, option.value)
    return combo


def fill_int_combo(options: tuple[int, ...], label: Callable[[int], str]) -> QComboBox:
    """The int-valued combos (parallel tasks, subagents, dev rounds):
    the same userData convention, labels from the settings module's
    label helpers."""
    combo = QComboBox()
    for value in options:
        combo.addItem(label(value), value)
    return combo


def select_enum(combo: QComboBox, value: object) -> None:
    index = combo.findData(getattr(value, "value", value))
    combo.setCurrentIndex(index if index >= 0 else 0)


def add_labelled_row(
    grid: QGridLayout, row: int, label: str, widget: QWidget
) -> None:
    grid.addWidget(QLabel(label), row, 0)
    grid.addWidget(widget, row, 1)


# -- the dialog ------------------------------------------------------------------


class ProjectSettingsDialog(QDialog):
    """Modal "Project settings" form over
    ``CoreShell.apply_task_settings`` — draft semantics, live hints,
    Apply / Cancel."""

    def __init__(self, shell: CoreShell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        # Nothing is persisted until Apply: the dialog edits a deep copy.
        self._draft: ProjectSettings = shell.settings.model_copy(deep=True)
        self._git_available = shell.task_env.available
        self.setWindowTitle("Project settings")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        # -- Workflow ---------------------------------------------------------
        layout.addWidget(self._section_label("Workflow"))
        grid = QGridLayout()
        self._retention = fill_enum_combo(RETENTION_OPTIONS)
        select_enum(self._retention, self._draft.completed_retention)
        add_labelled_row(grid, 0, "Auto-delete completed:", self._retention)
        self._isolation = fill_enum_combo(ISOLATION_OPTIONS)
        select_enum(self._isolation, self._draft.task_isolation)
        add_labelled_row(grid, 1, "Task isolation:", self._isolation)
        self._parallel = fill_int_combo(PARALLEL_TASKS_OPTIONS, parallel_tasks_label)
        select_enum(self._parallel, self._draft.parallel_tasks)
        add_labelled_row(grid, 2, "Parallel tasks:", self._parallel)
        self._start_branch = QLineEdit(self._draft.isolation_start_branch)
        self._start_branch.setPlaceholderText("auto (HEAD branch)")
        add_labelled_row(grid, 3, "Isolation start branch:", self._start_branch)
        self._drafts = QCheckBox("Agent-created tasks wait as drafts")
        self._drafts.setChecked(self._draft.agent_creates_drafts)
        add_labelled_row(grid, 4, "", self._drafts)
        self._titles = QCheckBox("Generate task titles")
        self._titles.setChecked(self._draft.generate_task_titles)
        add_labelled_row(grid, 5, "", self._titles)
        self._keepawake = QCheckBox("Keep the computer awake while there is work")
        self._keepawake.setChecked(self._draft.keep_awake)
        add_labelled_row(grid, 6, "", self._keepawake)
        self._esc = fill_enum_combo(ESC_ABORT_OPTIONS)
        select_enum(self._esc, self._draft.esc_abort)
        add_labelled_row(grid, 7, "Stop (ESC) mode:", self._esc)
        layout.addLayout(grid)

        # -- Testing ----------------------------------------------------------
        layout.addWidget(self._section_label("Testing"))
        grid = QGridLayout()
        self._verification = fill_enum_combo(VERIFICATION_OPTIONS)
        select_enum(self._verification, self._draft.verification)
        add_labelled_row(grid, 0, "Verification:", self._verification)
        self._dev_rounds = fill_int_combo(DEV_ROUNDS_OPTIONS, dev_rounds_label)
        self._dev_rounds.setCurrentIndex(
            self._dev_rounds.findData(self._draft.max_dev_rounds)
        )
        add_labelled_row(grid, 1, "Fix-loop cap:", self._dev_rounds)
        self._stall = fill_enum_combo(STALL_NUDGE_OPTIONS)
        select_enum(self._stall, self._draft.stall_nudge)
        add_labelled_row(grid, 2, "Stall nudge:", self._stall)
        self._sub_coder = fill_int_combo(SUBAGENTS_OPTIONS, subagents_label)
        self._sub_coder.setCurrentIndex(
            self._sub_coder.findData(self._draft.subagents_coder)
        )
        add_labelled_row(grid, 3, "Coder subagents:", self._sub_coder)
        self._sub_researcher = fill_int_combo(SUBAGENTS_OPTIONS, subagents_label)
        self._sub_researcher.setCurrentIndex(
            self._sub_researcher.findData(self._draft.subagents_researcher)
        )
        add_labelled_row(grid, 4, "Research subagents:", self._sub_researcher)
        layout.addLayout(grid)

        # -- Git ---------------------------------------------------------------
        layout.addWidget(self._section_label("Git"))
        grid = QGridLayout()
        self._git_tester = fill_enum_combo(GIT_POLICY_OPTIONS)
        select_enum(self._git_tester, self._draft.git_policy.tester)
        add_labelled_row(grid, 0, "Git (tester):", self._git_tester)
        self._git_research = fill_enum_combo(GIT_POLICY_OPTIONS)
        select_enum(self._git_research, self._draft.git_policy.research)
        add_labelled_row(grid, 1, "Git (research):", self._git_research)
        self._wt_removal = fill_enum_combo(WORKTREE_REMOVAL_OPTIONS)
        select_enum(self._wt_removal, self._draft.worktree_removal)
        add_labelled_row(grid, 2, "Worktree removal:", self._wt_removal)
        self._wt_snapshot = fill_enum_combo(WORKTREE_SNAPSHOT_OPTIONS)
        select_enum(self._wt_snapshot, self._draft.worktree_snapshot)
        add_labelled_row(grid, 3, "Worktree snapshot:", self._wt_snapshot)
        layout.addLayout(grid)

        # -- live hints (the shared coupling rules) -----------------------------
        self._hints_box = QVBoxLayout()
        layout.addLayout(self._hints_box)

        # -- buttons -------------------------------------------------------------
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        apply = QPushButton("Apply")
        apply.setDefault(True)
        apply.clicked.connect(self._apply)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        # Every widget change re-syncs the draft and re-renders the hints.
        for widget in (
            self._retention,
            self._isolation,
            self._parallel,
            self._start_branch,
            self._drafts,
            self._titles,
            self._keepawake,
            self._esc,
            self._verification,
            self._dev_rounds,
            self._stall,
            self._sub_coder,
            self._sub_researcher,
            self._git_tester,
            self._git_research,
            self._wt_removal,
            self._wt_snapshot,
        ):
            _wire_change(widget, self._on_change)
        self._render_hints()

    # -- draft sync -----------------------------------------------------------

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold;")
        return label

    def _draft_from_widgets(self) -> ProjectSettings:
        """Read every widget back into a draft (the TUI's Select/Switch →
        draft sync)."""
        draft = self._draft
        draft.completed_retention = CompletedRetention(
            str(self._retention.currentData())
        )
        draft.verification = Verification(str(self._verification.currentData()))
        draft.git_policy.tester = GitPolicy(str(self._git_tester.currentData()))
        draft.git_policy.research = GitPolicy(str(self._git_research.currentData()))
        rounds = self._dev_rounds.currentData()
        draft.max_dev_rounds = int(rounds) if rounds is not None else 3
        draft.stall_nudge = StallNudge(str(self._stall.currentData()))
        draft.task_isolation = TaskIsolation(str(self._isolation.currentData()))
        parallel = self._parallel.currentData()
        draft.parallel_tasks = int(parallel) if parallel is not None else 1
        coder = self._sub_coder.currentData()
        draft.subagents_coder = int(coder) if coder is not None else 0
        researcher = self._sub_researcher.currentData()
        draft.subagents_researcher = int(researcher) if researcher is not None else 0
        draft.agent_creates_drafts = self._drafts.isChecked()
        draft.generate_task_titles = self._titles.isChecked()
        draft.worktree_removal = WorktreeRemoval(str(self._wt_removal.currentData()))
        draft.worktree_snapshot = WorktreeSnapshot(
            str(self._wt_snapshot.currentData())
        )
        draft.isolation_start_branch = self._start_branch.text().strip()
        draft.keep_awake = self._keepawake.isChecked()
        draft.esc_abort = EscAbort(str(self._esc.currentData()))
        return draft

    def _sync_widgets_from_draft(self) -> None:
        """The reverse of `_draft_from_widgets` (a hint fix mutated the
        draft — the widgets must show it)."""
        select_enum(self._retention, self._draft.completed_retention)
        select_enum(self._isolation, self._draft.task_isolation)
        select_enum(self._parallel, self._draft.parallel_tasks)
        self._start_branch.setText(self._draft.isolation_start_branch)
        self._drafts.setChecked(self._draft.agent_creates_drafts)
        self._titles.setChecked(self._draft.generate_task_titles)
        self._keepawake.setChecked(self._draft.keep_awake)
        select_enum(self._esc, self._draft.esc_abort)
        select_enum(self._verification, self._draft.verification)
        index = self._dev_rounds.findData(self._draft.max_dev_rounds)
        self._dev_rounds.setCurrentIndex(index if index >= 0 else 0)
        select_enum(self._stall, self._draft.stall_nudge)
        index = self._sub_coder.findData(self._draft.subagents_coder)
        self._sub_coder.setCurrentIndex(index if index >= 0 else 0)
        index = self._sub_researcher.findData(self._draft.subagents_researcher)
        self._sub_researcher.setCurrentIndex(index if index >= 0 else 0)
        select_enum(self._git_tester, self._draft.git_policy.tester)
        select_enum(self._git_research, self._draft.git_policy.research)
        select_enum(self._wt_removal, self._draft.worktree_removal)
        select_enum(self._wt_snapshot, self._draft.worktree_snapshot)

    def _on_change(self) -> None:
        self._draft = self._draft_from_widgets()
        self._render_hints()

    # -- hints -----------------------------------------------------------------

    def _render_hints(self) -> None:
        """Re-render the hint panel from `task_settings_hints` — the ONE
        shared source of the coupling rules (the TUI dialog uses the very
        same function). A hint with a fix gets its own explicit button:
        the user's choice is never rewritten automatically."""
        while self._hints_box.count():
            item = self._hints_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        hints = task_settings_hints(self._draft, git_available=self._git_available)
        for hint in hints:
            row = QHBoxLayout()
            label = QLabel(hint.text)
            label.setWordWrap(True)
            if hint.severity == "conflict":
                label.setStyleSheet("color: #cd3131;")
            elif hint.severity == "dormant":
                label.setStyleSheet("color: #cca700;")
            row.addWidget(label, 1)
            if hint.fix is not None:
                row.addWidget(self._fix_button(hint))
            container = QWidget()
            container.setLayout(row)
            self._hints_box.addWidget(container)

    def _fix_button(self, hint: SettingsHint) -> QPushButton:
        button = QPushButton(hint.fix_label or "Fix")
        fix = hint.fix

        def apply_fix() -> None:
            assert fix is not None  # guarded by the caller
            fix(self._draft)
            self._sync_widgets_from_draft()
            self._render_hints()

        button.clicked.connect(apply_fix)
        return button

    # -- apply ------------------------------------------------------------------

    def _apply(self) -> bool:
        """Hand the draft to the shell seam (which persists it into the
        TUI's settings.json) and close. Returns True when the dialog
        should close."""
        self._draft = self._draft_from_widgets()
        self._shell.apply_task_settings(self._draft)
        self.accept()
        return True


def _wire_change(widget: QWidget, callback) -> None:
    """Connect the change signal of a combo / line edit / checkbox to
    `callback` (QComboBox.currentIndexChanged passes an int, the rest
    pass nothing — a lambda normalises both)."""
    if isinstance(widget, QComboBox):
        widget.currentIndexChanged.connect(lambda _index: callback())
    elif isinstance(widget, QLineEdit):
        widget.textChanged.connect(lambda _text: callback())
    elif isinstance(widget, QCheckBox):
        widget.toggled.connect(lambda _checked: callback())
