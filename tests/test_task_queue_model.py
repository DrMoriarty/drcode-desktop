"""Plan task 2 acceptance tests: the live task-queue model and view.

Covers:

- the mapping of the hub's ``task_updated`` events into the row model
  (the acceptance criterion): a status transition fired by the core's
  manager reaches the model's rows without any poller;
- the section/order parity with the TUI sidebar (DRAFTS → QUEUE → … →
  ON HOLD, the queue shown bottom-up, completed newest-first);
- the field parity: status markers, kind letters, verification labels
  (✅/❌ + the human text), the unfinished/needs_human/hold flags;
- the QTreeWidget rendering of the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from drcode.models import TaskStatus
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.qt_host import QtHost
from drcodedesktop.task_queue_model import TaskQueueModel
from drcodedesktop.task_queue_view import TaskQueueView


def run_queue_scenario(
    tmp_path: Path, scenario: Callable[[CoreShell, TaskQueueModel], object]
) -> tuple[CoreShell, TaskQueueModel]:
    """A shell + its queue model on a tmp project, driven on the qasync
    loop (the hub's signals are delivered synchronously on this thread —
    the same single-thread model as the TUI)."""
    app = QApplication.instance() or QApplication([])
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    hub = EventHub(log_path=tmp_path / "session.log")
    shell = CoreShell(project_root, QtHost(hub), hub)
    model = TaskQueueModel(shell, hub)

    async def driver() -> None:
        shell.start()
        await asyncio.sleep(0.1)
        scenario(shell, model)
        await asyncio.sleep(0.2)
        model.detach()
        shell.shutdown()
        await asyncio.sleep(0)

    with loop:
        loop.run_until_complete(driver())
    return shell, model


def test_task_updated_event_maps_into_the_model(tmp_path: Path) -> None:
    """The acceptance test: events → model rows, no polling involved.

    The model starts empty (a project without tasks); a task created by
    the manager (its creation listener emits ``task_updated``) and a
    subsequent status transition (hold) must appear in the rows through
    the event path alone."""

    def create_and_hold(shell: CoreShell, model: TaskQueueModel) -> None:
        assert model.rows() == []
        task = shell.manager.create("Event-mapped task", "desc", origin="user")
        # The creation listener already emitted task_updated synchronously.
        created = model.row(task.id)
        assert created is not None
        assert created.status_label.endswith("queued")
        # A status transition: the manager's status listener fires the
        # hub event, the model re-renders.
        shell.manager.set_hold(task.id, on=True)
        row = model.row(task.id)
        assert row is not None
        assert row.status.value == "hold"
        assert row.hold is True
        assert "⏸" in row.status_label

    _shell, _model = run_queue_scenario(tmp_path, create_and_hold)


def test_hub_signal_updates_the_model_outside_the_manager(tmp_path: Path) -> None:
    """A task_updated emitted straight through the hub (any seam source
    — the shell's coarse refresh, a future layer) re-renders too."""

    def drive(shell: CoreShell, _model: TaskQueueModel) -> None:
        task = shell.manager.create("Hub-driven task", "desc", origin="user")
        # Simulate a mutation that does not fire a status transition:
        # flip the unfinished flag and announce it via the hub.
        task.unfinished = True
        shell._hub.emit_task_updated(task.id, task.status.value)
        row = _model.row(task.id)
        assert row is not None
        assert row.unfinished is True
        assert "⏸ unfinished" in row.flag_fragments()

    _shell, _model = run_queue_scenario(tmp_path, drive)


def test_sections_match_the_tui_sidebar(tmp_path: Path) -> None:
    """Section names, order and internal ordering: the TUI sidebar's
    ``_sidebar_sections`` port (empty sections dropped, QUEUE bottom-up,
    COMPLETED newest-first, hold parked at the end)."""

    def build(shell: CoreShell, model: TaskQueueModel) -> None:
        shell.manager.create("First queued", "d1", origin="user")
        second = shell.manager.create("Second queued", "d2", origin="user")
        shell.manager.set_hold(second.id, on=True)
        done = shell.manager.create("Done feature", "d3", origin="user")
        shell.manager.mark_done(done.id)  # → TESTING (dev finished)
        shell.manager.set_verification(done.id, True)  # → COMPLETED ✅
        draft = shell.manager.create("Agent draft", "d4", origin="system", draft=True)
        draft.status = TaskStatus.DRAFT
        shell.manager.storage.save(draft)

    _shell, model = run_queue_scenario(tmp_path, build)
    names = [s.name for s in model.sections()]
    assert names == ["DRAFTS", "QUEUE", "COMPLETED", "ON HOLD"]
    by_name = {s.name: s for s in model.sections()}
    # QUEUE bottom-up: with one queued task it is simply there, with the
    # status marker of the TUI's queued row.
    queue_rows = by_name["QUEUE"].rows
    assert [r.title for r in queue_rows] == ["First queued"]
    assert queue_rows[0].status_label.startswith("◷")
    assert [r.title for r in by_name["ON HOLD"].rows] == ["Second queued"]
    assert [r.title for r in by_name["DRAFTS"].rows] == ["Agent draft"]
    done_row = by_name["COMPLETED"].rows[0]
    assert done_row.status_label.startswith("✓")


def test_verification_labels_match_the_tui(tmp_path: Path) -> None:
    """Verification parity: the tester's verdict surfaces with the same
    labels the TUI sidebar uses (✅ PASSED / ❌ FAILED + the enum text)."""

    def verify(shell: CoreShell, model: TaskQueueModel) -> None:
        passed = shell.manager.create("Passed task", "d", origin="user")
        failed = shell.manager.create("Failed task", "d", origin="user")
        # The honest pipeline path: dev finishes (→ TESTING), then the
        # tester stamps the verdict (→ COMPLETED ✅/❌).
        for task in (passed, failed):
            shell.manager.mark_done(task.id)
        shell.manager.set_verification(passed.id, True)
        shell.manager.set_verification(failed.id, False)

    _shell, model = run_queue_scenario(tmp_path, verify)
    rows = {r.title: r for r in model.rows()}
    passed, failed = rows["Passed task"], rows["Failed task"]
    assert passed.verification == "Verified successfully"
    assert passed.verification_glyph == "✅"
    assert passed.status.value == "completed"
    assert failed.verification == "Verification failed"
    assert failed.verification_glyph == "❌"
    # The ✅/❌ suffix lands in the composed label exactly like the TUI.
    assert passed.label.endswith("✅")
    assert failed.label.endswith("❌")


def test_row_label_parity_with_the_tui(tmp_path: Path) -> None:
    """The composed row label follows the TUI's ``_task_row_text``
    template: marker, kind letter, #id, title, suffixes."""

    def label_task(shell: CoreShell, _model: TaskQueueModel) -> None:
        task = shell.manager.create("Research the layout", "d", origin="user")
        task.kind = task.kind.__class__("research")
        shell.manager.storage.save(task)
        task.needs_human = True
        shell._hub.emit_task_updated(task.id, task.status.value)

    shell, model = run_queue_scenario(tmp_path, label_task)
    research_id = next(
        t.id for t in shell.manager.tasks.values() if t.title == "Research the layout"
    )
    row = model.row(research_id)
    assert row is not None
    assert row.kind_letter == "R"
    assert row.status_label.startswith("◷ queued")
    assert "✋" in row.label
    assert row.label.startswith("◷ R #")
    assert "Research the layout" in row.label


def test_dependency_marker_blocks_until_completed(tmp_path: Path) -> None:
    """The dependency-gate suffix: a queued task with an unmet dep shows
    "🔒 ⇢ <dep>"; once the dep completes the marker clears."""

    def wire(shell: CoreShell, model: TaskQueueModel) -> None:
        dep = shell.manager.create("The dep", "d", origin="user")
        dependent = shell.manager.create("The dependent", "d", origin="user")
        shell.manager.set_dependencies(dependent.id, [dep.id])

    shell, model = run_queue_scenario(tmp_path, wire)
    rows = {r.title: r for r in model.rows()}
    assert "🔒" in rows["The dependent"].deps_marker
    dep_id = next(t.id for t in shell.manager.tasks.values() if t.title == "The dep")
    assert dep_id in rows["The dependent"].deps_marker
    # Complete the dep (the no-tester completion path).
    dep_id = next(
        t.id for t in shell.manager.tasks.values() if t.title == "The dep"
    )
    shell.manager.complete_without_verification(dep_id)
    row = next(r for r in model.rows() if r.title == "The dependent")
    assert row.deps_marker == ""


def test_view_renders_the_model_tree(tmp_path: Path) -> None:
    """The QTreeWidget mirrors the model: one top-level item per
    section, the task rows as children with the plan's columns."""

    def fill(shell: CoreShell, model: TaskQueueModel) -> None:
        shell.manager.create("Queued feature", "d", origin="user")
        shell.manager.create("Held feature", "d", origin="user")
        held = [t for t in shell.manager.tasks.values() if t.title == "Held feature"]
        shell.manager.set_hold(held[0].id, on=True)

    shell, _model = run_queue_scenario(tmp_path, fill)
    app = QApplication.instance() or QApplication([])
    hub = EventHub(log_path=None)
    view = TaskQueueView(shell, hub)
    tree = view._tree
    assert tree.topLevelItemCount() == 2  # QUEUE + ON HOLD
    queue = tree.topLevelItem(0)
    assert queue.text(0) == "QUEUE (1)"
    # The group header spans every column: the name + count never
    # truncate against the id column width.
    assert queue.isFirstColumnSpanned()
    row = queue.child(0)
    # Columns: markers | done | kind+id | title.
    assert row.text(0) == ""
    assert row.text(1) == ""
    assert row.text(2) == f"F #{row.data(0, Qt.ItemDataRole.UserRole)}"
    assert row.text(3) == "Queued feature"
    # The tooltip carries the FULL title, not the on-screen head.
    assert row.toolTip(3) == "Queued feature"
    held_section = tree.topLevelItem(1)
    assert held_section.text(0) == "ON HOLD (1)"
    held_row = held_section.child(0)
    assert held_row.text(1) == ""  # not completed → no ✓
    assert held_row.text(3) == "Held feature"
    view.shutdown()
    del app


def test_completed_checkmark_colors_and_markers(tmp_path: Path) -> None:
    """The ✓ of a COMPLETED row is colored by mergedness (the shell's
    git snapshot verdict: green merged / yellow unmerged / gray no
    branch); the markers column hosts the verification glyph and the
    cancelled cross in one cell."""

    def complete(shell: CoreShell, _model: TaskQueueModel) -> None:
        merged = shell.manager.create("Merged task", "d", origin="user")
        unmerged = shell.manager.create("Unmerged task", "d", origin="user")
        for task in (merged, unmerged):
            shell.manager.complete_without_verification(task.id)
        # The wholesale git snapshot verdict the activity poll computes
        # (the GUI test stubs it — the real git walk is another test's
        # job).
        shell._merge_state = {merged.id: "merged", unmerged.id: "unmerged"}

    shell, _model = run_queue_scenario(tmp_path, complete)
    app = QApplication.instance() or QApplication([])
    hub = EventHub(log_path=None)
    view = TaskQueueView(shell, hub)
    completed = view._tree.topLevelItem(0)
    assert completed.text(0).startswith("COMPLETED (")
    rows = {
        completed.child(i).text(3): completed.child(i)
        for i in range(completed.childCount())
    }
    merged_row = rows["Merged task"]
    unmerged_row = rows["Unmerged task"]
    assert merged_row.text(1) == "✓"
    assert merged_row.foreground(1).color().name() == "#98c379"  # green
    assert unmerged_row.text(1) == "✓"
    assert unmerged_row.foreground(1).color().name() == "#e5c07b"  # yellow
    # No branch (research, no commits): the gray ✓.
    shell._merge_state = {}
    view.rebuild()
    completed = view._tree.topLevelItem(0)
    gray_row = completed.child(0)
    assert gray_row.text(1) == "✓"
    assert gray_row.foreground(1).color().name() == "#787c82"  # gray
    # Markers: ✅/❌ verification and ✖ cancelled — one column.
    merged_id = next(
        t.id for t in shell.manager.tasks.values() if t.title == "Merged task"
    )
    shell.manager.tasks[merged_id].cancelled = True
    shell._hub.emit_task_updated(merged_id, "completed")
    view.rebuild()
    completed = view._tree.topLevelItem(0)
    cancelled_row = next(
        completed.child(i)
        for i in range(completed.childCount())
        if completed.child(i).text(3) == "Merged task"
    )
    assert "✖" in cancelled_row.text(0)
    view.shutdown()
    del app
