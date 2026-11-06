"""The live task-queue model (plan task 2).

The GUI-side analogue of the TUI sidebar's task list
(``drcode.tui.sidebar_view``): the same pipeline sections in the same
order, the same row markers (status glyphs, kind letters, verification
suffix, the resolver gear, the dependency/goal/needs-human markers) and
the same queue ordering — built from the SAME core state
(``TaskManager``) the TUI reads.

The model is UI-agnostic (a plain :class:`QObject` with one signal, no
widgets): it maps task objects to :class:`TaskRow` values and keeps them
fresh from TWO sources, mirroring the TUI's dual update paths:

- the ``task_updated`` events of the core's event seam (hub signal) —
  the real-time path (status transitions, creations, the shell's
  periodic coarse refresh);
- a lightweight fingerprint poll (:meth:`refresh`) — the safety net the
  TUI sidebar uses too, for flag/state mutations that do not fire a
  status transition (``unfinished`` set on abort, queue re-stamps, the
  resolver slot starting/stopping).

Read-only: the model never mutates tasks (creation/hold/cancel is plan
task 5).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from drcode.models import Task, TaskKind, TaskStatus, VerificationStatus
from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from .core_shell import CoreShell
    from .event_hub import EventHub

#: Sidebar section names and their order (the TUI drops empty sections;
#: so does the model — see :meth:`TaskQueueModel.sections`).
SECTION_NAMES: tuple[str, ...] = (
    "DRAFTS",
    "QUEUE",
    "NEEDS CLARIFICATION",
    "ACTIVE",
    "TESTING",
    "COMPLETED",
    "ON HOLD",
)

#: Status → glyph, exactly the TUI's ``_task_row_text`` marker map
#: (plain U+2699 for the resolver gear — terminals render it narrow, and
#: the GUI rows share the width budget of the console tab).
STATUS_MARKERS: dict[str, str] = {
    "queued": "◷",
    "draft": "✎",
    "hold": "⏸",
    "active": "▶",
    "testing": "⚑",
    "completed": "✓",
    "clarification": "❓",
}

#: Kind → one-letter column, the TUI's B/R/F letters (color lives in the
#: view layer — Qt has no rich markup in tree cells).
KIND_LETTERS: dict[TaskKind, str] = {
    TaskKind.BUG: "B",
    TaskKind.RESEARCH: "R",
    TaskKind.FEATURE: "F",
}

#: Verification label → the ✅/❌ suffix of the TUI rows.
VERIFICATION_GLYPHS: dict[VerificationStatus, str] = {
    VerificationStatus.PASSED: "✅",
    VerificationStatus.FAILED: "❌",
}

#: Kind → column color role names (red B / yellow R / green F, the TUI's
#: kind-letter colors). Resolved in the view via QColor roles.
KIND_COLORS: dict[TaskKind, str] = {
    TaskKind.BUG: "red",
    TaskKind.RESEARCH: "yellow",
    TaskKind.FEATURE: "green",
}


@dataclass(frozen=True)
class TaskRow:
    """One task row of the queue screen — the columns the plan asks for
    (id, kind, status, verification label, the
    unfinished/needs_human/hold flags) plus the parity extras the TUI
    sidebar shows on the same row (title, dependency/goal markers, the
    composed ``label``)."""

    task_id: str
    kind: TaskKind
    kind_letter: str
    kind_color: str
    status: TaskStatus
    #: TUI marker + status value, e.g. ``"⚑ testing"`` (the resolver's
    #: ⚙ replaces the status marker while it works on the task).
    status_label: str
    #: Human label of the verification verdict ("Verified successfully"
    #: / "Verification failed") or "" when the tester has not labelled
    #: the task yet.
    verification: str
    #: ✅/❌ glyph for the verdict, "" when none.
    verification_glyph: str
    unfinished: bool
    needs_human: bool
    hold: bool
    cancelled: bool
    #: Mergedness of a COMPLETED task's ``drcode/<id>`` branch, the
    #: shell's git snapshot verdict: ``"merged"`` (влито в базу),
    #: ``"unmerged"`` (коммит есть, мержа нет), ``"none"`` (ветки нет —
    #: коммита не было и не нужно, напр. research) — или ``""``, когда
    #: вердикта ещё нет (изоляция выключена / снапшот не считался).
    merge_state: str
    title: str
    #: The dependency-gate suffix ("🔒 ⇢ <ids>") or "".
    deps_marker: str
    #: The goal-link suffix ("◆<goal-id>") or "".
    goal_marker: str
    #: True while the automatic conflict resolver works on this task.
    resolving: bool
    #: The whole row rendered the way the TUI sidebar renders it (plain
    #: text, no markup) — the parity reference the tests assert on.
    label: str = field(compare=False)

    def flag_fragments(self) -> list[str]:
        """The Flags column: the same markers the TUI's task header
        carries (``task_status_view``: "✋ needs human", "⏸ unfinished"),
        plus hold/cancelled for the statuses that mean them."""
        bits: list[str] = []
        if self.hold:
            bits.append("⏸ hold")
        if self.unfinished:
            bits.append("⏸ unfinished")
        if self.needs_human:
            bits.append("✋ needs human")
        if self.cancelled:
            bits.append("✖ cancelled")
        return bits


@dataclass(frozen=True)
class TaskSection:
    """A sidebar section: its header name and the task rows in display
    order (the header text ``QUEUE (3)`` — the TUI's ``-``/``+``
    collapse glyph is NOT reproduced: the GUI collapses with its own
    chevron, so the leftover dash would be noise)."""

    name: str
    rows: list[TaskRow]

    def header(self) -> str:
        return f"{self.name} ({len(self.rows)})"


class TaskQueueModel(QObject):
    """Task objects → rows, kept live by the hub's ``task_updated``."""

    #: The row set changed (a status flip, a promotion, a new task, …).
    rows_changed = Signal()

    def __init__(self, shell: CoreShell, hub: EventHub) -> None:
        super().__init__()
        self._shell = shell
        self._hub = hub
        self._fingerprint: tuple = ()
        # The real-time path: every status transition of the core fires
        # the hub signal (CoreShell's status listener); the shell's
        # refresh_sidebar broadcasts a coarse all-tasks snapshot on the
        # pipeline's non-status reshuffles (promotions, queue moves).
        # A deletion fires task_removed — no task_updated exists for a
        # task that is no longer there.
        hub.task_updated.connect(self._on_task_updated)
        hub.task_removed.connect(self._on_task_removed)

    # -- public surface --------------------------------------------------------

    def sections(self) -> list[TaskSection]:
        """The non-empty sidebar sections, TUI order (see
        ``SidebarLayer._sidebar_sections`` — the GUI model is a direct
        port of it over the same manager state)."""
        manager = self._shell.manager
        by_status: dict[TaskStatus, list[Task]] = {}
        for t in manager.all_tasks():
            by_status.setdefault(t.status, []).append(t)

        # QUEUE: the TUI shows the queue bottom-up (the next task to be
        # promoted sits at the top of the section) — hence reversed().
        queue = list(reversed(manager.queued()))
        clarification = sorted(
            by_status.get(TaskStatus.AWAITING_CLARIFICATION, []),
            key=lambda t: t.updated_at,
            reverse=True,
        )
        active = by_status.get(TaskStatus.ACTIVE, [])
        # Normally one task is being tested; several stuck in TESTING
        # keep the tester's current task at the bottom of the section —
        # same parity rule as the TUI.
        testing_active_id = self._shell._tester_task_id
        testing_all = by_status.get(TaskStatus.TESTING, [])
        testing = [t for t in testing_all if t.id != testing_active_id] + [
            t for t in testing_all if t.id == testing_active_id
        ]
        completed = sorted(
            by_status.get(TaskStatus.COMPLETED, []),
            key=lambda t: t.updated_at,
            reverse=True,
        )
        hold = by_status.get(TaskStatus.HOLD, [])
        drafts = sorted(
            by_status.get(TaskStatus.DRAFT, []),
            key=lambda t: t.created_at,
            reverse=True,
        )

        grouped: dict[str, list[Task]] = {
            "DRAFTS": drafts,
            "QUEUE": queue,
            "NEEDS CLARIFICATION": clarification,
            "ACTIVE": active,
            "TESTING": testing,
            "COMPLETED": completed,
            "ON HOLD": hold,
        }
        return [
            TaskSection(name, [self._row_for(t) for t in tasks])
            for name, tasks in grouped.items()
            if tasks
        ]

    def rows(self) -> list[TaskRow]:
        """All rows flattened in section order (tests, simple consumers)."""
        return [row for section in self.sections() for row in section.rows]

    def refresh(self) -> None:
        """The poll path: recompute the fingerprint and notify when the
        rendered state changed. Cheap (no rendering) — the view's 1 s
        timer calls this like the TUI's sidebar poller does."""
        fp = self._compute_fingerprint()
        if fp != self._fingerprint:
            self._fingerprint = fp
            self.rows_changed.emit()

    def detach(self) -> None:
        """Disconnect from the hub (the project closed): no signal may
        fire into a dead shell afterwards."""
        with contextlib.suppress(RuntimeError):
            self._hub.task_updated.disconnect(self._on_task_updated)
            self._hub.task_removed.disconnect(self._on_task_removed)

    def sync_task(self, task_id: str) -> None:
        """The event path: a ``task_updated`` for `task_id` arrived —
        re-render if the row content changed."""
        del task_id  # the fingerprint covers every task at once
        self.refresh()

    def row(self, task_id: str) -> TaskRow | None:
        for row in self.rows():
            if row.task_id == task_id:
                return row
        return None

    # -- internals ---------------------------------------------------------------

    def _on_task_updated(self, task_id: str, status: str) -> None:
        del status
        self.sync_task(task_id)

    def _on_task_removed(self, task_id: str) -> None:
        del task_id  # the fingerprint covers every task at once
        self.refresh()

    def _compute_fingerprint(self) -> tuple:
        """Everything the rows render — the GUI port of the TUI sidebar's
        ``_sidebar_fingerprint`` (status, verification, title, queue
        stamp/head, needs_human, mergedness) extended with the flags the
        GUI shows as separate columns (unfinished, cancelled) and the
        session state that swaps markers (the resolver slot)."""
        shell = self._shell
        return (
            tuple(
                (
                    t.id,
                    t.status.value,
                    t.kind.value,
                    t.verification,
                    t.title_display,
                    t.queue_seq,
                    t.queue_head,
                    t.needs_human,
                    t.unfinished,
                    t.cancelled,
                    t.goal_id,
                    shell._merge_state.get(t.id, ""),
                )
                for t in self._shell.manager.all_tasks()
            ),
            shell._resolver_running,
            shell._resolver_task_id,
            tuple(
                (task_id, tuple(sorted(gate)))
                for task_id, gate in shell._dep_gate.items()
            ),
            shell.viewed_id,
        )

    def _row_for(self, task: Task) -> TaskRow:
        shell = self._shell
        resolving = shell._resolver_running and shell._resolver_task_id == task.id
        marker = "⚙" if resolving else STATUS_MARKERS.get(task.status.value, "⏳")
        kind_letter = KIND_LETTERS[task.kind]
        merge_state = (
            shell._merge_state.get(task.id, "")
            if task.status is TaskStatus.COMPLETED
            else ""
        )
        if task.verification is VerificationStatus.PASSED:
            verification, glyph = task.verification.value, "✅"
        elif task.verification is VerificationStatus.FAILED:
            verification, glyph = task.verification.value, "❌"
        else:
            verification, glyph = "", ""
        deps_marker = self._dependency_marker(task)
        goal_marker = self._goal_marker(task)
        human_suffix = " ✋" if task.needs_human else ""
        viewed = " ●" if task.id == shell.viewed_id else ""
        title = task.title_display
        label = (
            f"{marker} {kind_letter} #{task.id} "
            f"{title}{glyph}{deps_marker}{goal_marker}{human_suffix}{viewed}"
        )
        return TaskRow(
            task_id=task.id,
            kind=task.kind,
            kind_letter=kind_letter,
            kind_color=KIND_COLORS[task.kind],
            status=task.status,
            status_label=f"{marker} {task.status.value}",
            verification=verification,
            verification_glyph=glyph,
            unfinished=task.unfinished,
            needs_human=task.needs_human,
            hold=task.status is TaskStatus.HOLD,
            cancelled=task.cancelled,
            merge_state=merge_state,
            title=title,
            deps_marker=deps_marker,
            goal_marker=goal_marker,
            resolving=resolving,
            label=label,
        )

    def _dependency_marker(self, task: Task) -> str:
        """The dependency-gate suffix, the TUI's ``_dependency_marker``
        verbatim: the pipeline's last computed gate covers the git half
        (a dep whose branch still holds unmerged work), the manager's
        status check is the fallback."""
        if not task.depends_on:
            return ""
        gate = self._shell._dep_gate.get(task.id)
        if gate:
            waiting = list(gate)
        elif self._shell.manager.blocking_dependencies(task.id):
            waiting = list(task.depends_on)
        else:
            return ""
        return f" 🔒 ⇢ {','.join(waiting)}"

    def _goal_marker(self, task: Task) -> str:
        """The goal-link suffix (" ◆<goal-id>"), dangling ids tolerated
        but not shown — the TUI's ``_goal_marker`` rule."""
        if not task.goal_id or task.goal_id not in self._shell.manager.goals:
            return ""
        return f" ◆{task.goal_id}"
