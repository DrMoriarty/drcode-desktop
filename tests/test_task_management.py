"""Plan task 5 acceptance tests: task-management operations from the GUI.

Every operation is driven through the exact production seam the widgets
route to — the :class:`~drcodedesktop.core_shell.CoreShell` methods that
port the TUI's ``tui.actions_lifecycle`` behaviour headless:

- creation of bug/feature/research tasks (with the
  ``validate_dependencies`` gate: a violation creates nothing);
- hold/unhold (queued and awaiting-clarification tasks; the question
  survives the park; an answer typed while held re-queues at the head);
- cancel (runs stopped, input dropped, completes without the tester);
- delete — only for held tasks;
- dependency add/remove (the gate on both the edit and the promotion);
- queue reordering (the bug block never crossed);
- the widgets (queue-view context actions, the new-task dialog) route
  to the same seam.

Where a test needs promotion, the model is mocked (``mocks.mock_env``)
— without one the shell keeps the queue parked (the task-1 rule), which
the state-machine tests rely on instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from drcode.agent import all_user_prompts
from drcode.models import Task, TaskKind, TaskStatus
from pydantic_ai.messages import TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.new_task_dialog import NewTaskDialog
from drcodedesktop.task_queue_view import TaskQueueView
from tests import mocks
from tests.test_user_input import (
    CLARIFY_ARGS,
    no_verification,
    run_shell_scenario,
    wait_status,
)


def collect_notes(hub: EventHub) -> list[tuple[str, str, str]]:
    """Capture the hub's notify emissions inside a scenario."""
    notes: list[tuple[str, str, str]] = []
    hub.notify.connect(lambda m, t, s: notes.append((m, t, s)))
    return notes


def park_queue(monkeypatch: Any) -> Callable[[CoreShell], None]:
    """A setup that forces the 'no model configured' state: the queue
    stays parked regardless of the host machine's real user config.
    These tests drive the lifecycle state machine, not the runs — the
    shell's own convention (advance only when a model is ready) must
    not depend on the machine the suite runs on."""

    def setup(shell: CoreShell) -> None:
        class _Parked:
            is_ready = False

        monkeypatch.setattr(shell, "resolved_config", lambda role: _Parked())

    return setup


# -- creation ---------------------------------------------------------------------


def test_create_task_kinds_land_in_the_queue(tmp_path: Path, monkeypatch: Any) -> None:
    """bug/feature/research tasks are created through the shell: the
    text becomes the condition (the title — its first line), the brief
    waits in pending_input, the task is queued and opened in the pane;
    the bug sits at the queue head (the manager's kind rule)."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        feature = shell.create_task(
            "Add dark mode\nlonger description", kind=TaskKind.FEATURE
        )
        bug = shell.create_task("Fix the crash on start", kind=TaskKind.BUG)
        research = shell.create_task("Research the layout", kind=TaskKind.RESEARCH)
        assert feature is not None and bug is not None and research is not None
        for task in (feature, bug, research):
            assert task.status is TaskStatus.QUEUED
        assert feature.kind is TaskKind.FEATURE
        assert bug.kind is TaskKind.BUG
        assert research.kind is TaskKind.RESEARCH
        assert [e.text for e in feature.pending_input] == [
            "Add dark mode\nlonger description"
        ]
        assert feature.title == "Add dark mode"
        # The bug report runs first, the rest follow in creation order.
        assert [t.id for t in shell.manager.queued()] == [
            bug.id,
            feature.id,
            research.id,
        ]
        # The last created task is opened in the chat pane (the TUI's
        # submit → view_task move).
        assert shell.viewed_id == research.id

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_create_task_on_hold_parks_it(tmp_path: Path, monkeypatch: Any) -> None:
    """The "create on hold" path: the task is born HOLD with its brief
    kept, is not opened in the pane, and resume_task returns it to the
    queue."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.create_task("Parked work", hold=True)
        assert task is not None
        assert task.status is TaskStatus.HOLD
        assert [e.text for e in task.pending_input] == ["Parked work"]
        assert shell.viewed_id != task.id
        assert shell.resume_task(task.id) is True
        assert task.status is TaskStatus.QUEUED

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_create_task_rejects_invalid_dependency(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The validate_dependencies gate runs BEFORE anything is created:
    an unknown dep id leaves the store untouched and the error reaches
    the user as a notify (the TUI form's note line)."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        notes = collect_notes(hub)
        before = set(shell.manager.tasks)
        task = shell.create_task("Broken deps", depends_on=["nonexistent"])
        assert task is None
        assert set(shell.manager.tasks) == before
        assert any(
            "unknown task id: nonexistent" in message for message, _title, _sev in notes
        )
        # An empty text is refused the same way.
        assert shell.create_task("   ") is None

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_created_dependent_waits_for_its_dependency(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The dep-gate on promotions, through the GUI creation path: a task
    created with a dependency does not run until the dependency has
    COMPLETED — the runs are strictly ordered by the gate."""

    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [mocks.text_response("done a"), mocks.text_response("done b")]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        no_verification(shell)
        a = shell.create_task("do a")
        assert a is not None
        b = shell.create_task("do b", depends_on=[a.id])
        assert b is not None
        assert b.depends_on == [a.id]
        # a is taken into development while b waits queued: the gate
        # held the dependent back (the promotion worker never skipped
        # the dep check under the GUI's own create→advance path).
        try:
            await mocks.wait_until(lambda: len(state.calls) == 1, what="a's run")
        except AssertionError:
            print("EVENTS a:", [e.text for e in a.events])
            print("STATUS a:", a.status, "b:", b.status)
            raise
        assert a.status is TaskStatus.ACTIVE
        assert b.status is TaskStatus.QUEUED
        await wait_status(shell, a.id, TaskStatus.COMPLETED)
        await wait_status(shell, b.id, TaskStatus.COMPLETED)
        # The gate ordered the runs: the model saw a's brief first.
        assert "do a" in state.calls[0]
        assert "do b" not in state.calls[0]
        assert "do b" in state.calls[-1]

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- hold / resume ----------------------------------------------------------------


def test_hold_and_resume_queued_task(tmp_path: Path, monkeypatch: Any) -> None:
    """Hold parks a queued task out of the backlog; resume returns it.
    Both refusals (double hold, resume of a non-held task) are rejected
    by the shell guards."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.create_task("Work item")
        assert task is not None
        assert shell.hold_task(task.id) is True
        assert task.status is TaskStatus.HOLD
        # Already held: refused.
        assert shell.hold_task(task.id) is False
        assert shell.resume_task(task.id) is True
        assert task.status is TaskStatus.QUEUED
        # Not held anymore: refused.
        assert shell.resume_task(task.id) is False

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_hold_awaiting_clarification_preserves_the_question(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A task awaiting a clarifying answer can be shelved: the question
    survives the park and is restored on resume; an answer typed while
    held counts as an answered clarification (queue head) — the full
    manager.set_hold semantics through the GUI seam."""

    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [
        mocks.tool_response("ask_clarifying_question", CLARIFY_ARGS),
        mocks.text_response("уточняю"),
        # The answer run (after the answer typed while held).
        mocks.text_response("готово"),
    ]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        no_verification(shell)
        task = shell.create_task("покрась кнопку")
        assert task is not None
        shell.view_task(task.id)
        assert shell.submit_user_text(task.id, "начни") is True
        await wait_status(shell, task.id, TaskStatus.AWAITING_CLARIFICATION)
        await mocks.wait_until(
            lambda: not shell.agent_running, what="run 1 winding down"
        )
        assert task.clarification is not None

        # Park it: the question is preserved.
        assert shell.hold_task(task.id) is True
        assert task.status is TaskStatus.HOLD
        assert task.clarification is not None
        # Resume without an answer: back to awaiting clarification.
        assert shell.resume_task(task.id) is True
        assert task.status is TaskStatus.AWAITING_CLARIFICATION
        assert task.clarification is not None

        # Park again, answer while held, resume: queue head, question
        # cleared, the answer run completes the task.
        assert shell.hold_task(task.id) is True
        assert shell.submit_user_text(task.id, "синий") is True
        assert task.status is TaskStatus.HOLD  # input accumulates, no resume
        assert shell.resume_task(task.id) is True
        # The model is configured: the advance inside resume_task may
        # already have taken the task into development (the TUI's
        # behaviour too) — the re-queue facts are what the state shows.
        assert task.status in (TaskStatus.QUEUED, TaskStatus.ACTIVE)
        assert task.clarification is None
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        prompts = all_user_prompts(shell.manager.get(task.id).messages)
        assert any("синий" in p for p in prompts)

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- cancel -----------------------------------------------------------------------


def test_cancel_queued_task_completes_without_testing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Cancel drops the queued input and completes the task with the
    cancelled marker; the tester never runs; cancelling again is
    refused."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.create_task("Not needed anymore")
        assert task is not None
        assert shell.cancel_task(task.id) is True
        assert task.status is TaskStatus.COMPLETED
        assert task.cancelled is True
        assert [e.text for e in task.pending_input] == []
        assert any("Task cancelled by the user" in e.text for e in task.events)
        # Completed work is not cancellable.
        assert shell.cancel_task(task.id) is False

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_cancel_running_task_stops_the_run_and_flushes_the_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Cancelling a task mid-run stops its agent run (the exclusive
    ``agent-<id>`` group), flushes the consumed-but-untranscribed brief
    into the transcript, and completes the task as cancelled."""

    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    # The run is held mid-flight when the cancel fires.
    release = asyncio.Event()

    async def gated_stream(
        messages: list[Any], info: AgentInfo
    ) -> AsyncIterator[str | dict[int, Any]]:
        del info
        state.calls.append(mocks.last_user_text(messages))
        await release.wait()
        step = state.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        for part in step.parts:
            if isinstance(part, TextPart):
                yield part.content

    monkeypatch.setattr(
        state,
        "model",
        lambda: FunctionModel(state.fn, stream_function=gated_stream),
    )
    state.steps = [mocks.text_response("готово")]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        no_verification(shell)
        task = shell.create_task("long running work")
        assert task is not None
        await mocks.wait_until(lambda: len(state.calls) == 1, what="model call")
        assert task.status is TaskStatus.ACTIVE
        assert shell.cancel_task(task.id) is True
        assert task.status is TaskStatus.COMPLETED
        assert task.cancelled is True
        assert not shell.agent_running
        assert any("Task cancelled by the user" in e.text for e in task.events)
        # The in-flight prompt was flushed into the transcript before the
        # cancel cleared pending_input — the user's brief is not lost.
        prompts = all_user_prompts(task.messages)
        assert any("long running work" in p for p in prompts)
        # Let the cancelled worker unwind before the teardown.
        release.set()
        await asyncio.sleep(0.1)

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- delete -----------------------------------------------------------------------


def test_delete_only_held(tmp_path: Path, monkeypatch: Any) -> None:
    """The delete prohibition: a QUEUED task cannot be deleted; a held
    one is removed from memory and from disk storage, the hub announces
    the removal, and the shell drops the selection if the deleted task
    was the viewed one."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        removed: list[str] = []
        hub.task_removed.connect(removed.append)
        task = shell.create_task("Doomed work")
        assert task is not None
        # QUEUED → refused, the task survives.
        assert shell.delete_task(task.id) is False
        assert task.id in shell.manager.tasks
        assert removed == []
        shell.hold_task(task.id)
        shell.view_task(task.id)
        assert shell.delete_task(task.id) is True
        assert task.id not in shell.manager.tasks
        assert removed == [task.id]
        task_files = list((shell.project_root / ".drcode" / "tasks").glob("*.json"))
        assert task_files == []
        # The viewed selection was dropped with the task.
        assert shell.viewed_id is None

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


# -- dependencies -----------------------------------------------------------------


def test_add_and_remove_dependencies(tmp_path: Path, monkeypatch: Any) -> None:
    """Dependency editing on a queued task: an unknown id is refused
    (deps untouched, error notified), a valid id lands (the ``#id``
    mention form tolerated) with the TUI's service event; a task that
    already started (ACTIVE) refuses edits; removing logs the manual
    override."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        notes = collect_notes(hub)
        a = shell.create_task("first")
        b = shell.create_task("second")
        assert a is not None and b is not None
        # Unknown dep id: refused by the gate.
        assert shell.add_dependency(b.id, "zzz") is False
        assert b.depends_on == []
        assert any("unknown task id: zzz" in m for m, _t, _s in notes)
        # Valid add (mention form): the dep lands with its event.
        assert shell.add_dependency(b.id, f"#{a.id}") is True
        assert b.depends_on == [a.id]
        assert any(f"Dependency #{a.id} added" in e.text for e in b.events)
        # A started task refuses dependency edits (the TUI's
        # _can_edit_dependencies rule).
        shell.manager.promote_next()
        assert a.status is TaskStatus.ACTIVE
        assert shell.add_dependency(a.id, b.id) is False
        # Removing an id the task does not depend on: a quiet refusal.
        assert shell.remove_dependency(b.id, b.id) is False
        assert b.depends_on == [a.id]
        # The manual override: the dep comes off, the event is logged.
        assert shell.remove_dependency(b.id, a.id) is True
        assert b.depends_on == []
        assert any(
            f"Manual override: dependency #{a.id} removed" in e.text for e in b.events
        )

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_dependency_cycle_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """The cycle half of the gate: a → b → a is detected on write and
    surfaced as a notify, both deps lists stay unchanged."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        notes = collect_notes(hub)
        a = shell.create_task("a")
        b = shell.create_task("b")
        assert a is not None and b is not None
        assert shell.add_dependency(b.id, a.id) is True
        assert shell.add_dependency(a.id, b.id) is False
        assert a.depends_on == []
        assert any("dependency cycle detected" in m for m, _t, _s in notes)

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_removing_blocking_dependency_force_starts_the_task(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The dep-gate on promotions, both halves: a queued task whose dep
    sits on hold is blocked (recorded in the gate, announced once), and
    removing the blocking dependency makes it eligible immediately —
    the force-start runs it without any other change."""

    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [mocks.text_response("готово")]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        no_verification(shell)
        a = shell.create_task("parked dep", hold=True)
        assert a is not None
        b = shell.create_task("dependent work", depends_on=[a.id])
        assert b is not None
        # The held dep never completes: b stays queued, blocked, with
        # the gate recording why (the promotion worker's announcement).
        await asyncio.sleep(0.3)
        assert b.status is TaskStatus.QUEUED
        await mocks.wait_until(
            lambda: b.id in shell._dep_gate, what="the gate recording b"
        )
        texts = [e.text for e in b.events]
        assert sum("Task waits" in t for t in texts) == 1
        # The manual override: eligible immediately, run dispatched.
        assert shell.remove_dependency(b.id, a.id) is True
        await wait_status(shell, b.id, TaskStatus.COMPLETED)
        assert any(
            f"Manual override: dependency #{a.id} removed" in e.text for e in b.events
        )

    run_shell_scenario(tmp_path, no_verification, scenario)


def test_completed_dependency_unblocks_the_dependent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The status half of the gate end-to-end through the shell: b is
    not promoted while a is unfinished; once a completes, the next
    advance promotes b (its gate entry cleared, the "eligible" event
    logged)."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        a = shell.create_task("do a")
        assert a is not None
        b = shell.create_task("do b", depends_on=[a.id])
        assert b is not None
        # No model configured: the shell keeps the queue parked, the
        # test drives the promotion explicitly (the core's own
        # test_dependency_gate pattern). The promotion runs through the
        # dep worker (b carries deps): wait out its git-free pass.
        shell._advance()
        await mocks.wait_until(lambda: a.status is TaskStatus.ACTIVE, what="a promoted")
        assert b.status is TaskStatus.QUEUED
        await mocks.wait_until(
            lambda: b.id in shell._dep_gate, what="the gate recording b"
        )

        # a finishes (the no-tester completion path) → b unblocks.
        shell.manager.complete_without_verification(a.id)
        shell._advance()
        await wait_status(shell, b.id, TaskStatus.ACTIVE)
        assert b.id not in shell._dep_gate
        assert any("Dependencies satisfied" in e.text for e in b.events)

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


# -- queue reordering -------------------------------------------------------------


def test_move_in_queue_rules(tmp_path: Path, monkeypatch: Any) -> None:
    """The manual reorder: one step per move, the queue edges and the
    bug block refuse (bugs always run first), a non-queued task is not
    movable — the manager's move_in_queue rules through the shell."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        t1 = shell.create_task("one", kind=TaskKind.FEATURE)
        t2 = shell.create_task("two", kind=TaskKind.FEATURE)
        t3 = shell.create_task("three", kind=TaskKind.FEATURE)
        bug = shell.create_task("bug", kind=TaskKind.BUG)
        assert t1 and t2 and t3 and bug
        assert [t.id for t in shell.manager.queued()] == [
            bug.id,
            t1.id,
            t2.id,
            t3.id,
        ]
        # t3 is at the far edge of the queue; t1 cannot cross the bug
        # block; the bug itself is at the execution end.
        assert shell.move_task_in_queue(t3.id, up=True) is False
        assert shell.move_task_in_queue(t1.id, up=False) is False
        assert shell.move_task_in_queue(bug.id, up=False) is False
        # One step away from execution…
        assert shell.move_task_in_queue(t2.id, up=True) is True
        assert [t.id for t in shell.manager.queued()] == [
            bug.id,
            t1.id,
            t3.id,
            t2.id,
        ]
        # …and one step back towards it (the move is its own inverse).
        assert shell.move_task_in_queue(t2.id, up=False) is True
        assert [t.id for t in shell.manager.queued()] == [
            bug.id,
            t1.id,
            t2.id,
            t3.id,
        ]
        # A held task is not part of the queue: refused.
        assert shell.hold_task(t2.id) is True
        assert shell.move_task_in_queue(t2.id, up=True) is False

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


# -- the widgets route to the same seam --------------------------------------------


def test_queue_view_actions_and_new_task_dialog(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The queue view's context actions and the new-task dialog drive
    the shell: hold/delete through _run_action (with the delete guard),
    the dialog creating a task with deps and refusing invalid ones (the
    dialog stays open with the error shown)."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        view = TaskQueueView(shell, hub)
        a = shell.create_task("first")
        b = shell.create_task("second")
        assert a is not None and b is not None

        # Context actions → the shell seam.
        view._run_action("hold", a.id)
        assert a.status is TaskStatus.HOLD
        view._run_action("delete", b.id)  # queued → refused
        assert b.id in shell.manager.tasks
        view._run_action("delete", a.id)  # held → gone
        assert a.id not in shell.manager.tasks
        # The model dropped the row (the task_removed path).
        assert view.row_item(a.id) is None

        # The new-task dialog: valid form → the task exists with deps.
        dialog = NewTaskDialog(shell)
        dialog._text.setPlainText("Dialog task\nbody")
        dialog._deps.setText(f"#{b.id}")
        assert dialog._submit() is True
        created: Task | None = None
        for task in shell.manager.tasks.values():
            if task.title == "Dialog task":
                created = task
        assert created is not None
        assert created.status is TaskStatus.QUEUED
        assert created.depends_on == [b.id]
        assert created.kind is TaskKind.FEATURE

        # An invalid dep list: the dialog stays open (no accept), the
        # error is in the note line, nothing is created.
        dialog2 = NewTaskDialog(shell)
        dialog2._text.setPlainText("Broken")
        dialog2._deps.setText("zzz")
        assert dialog2._submit() is False
        assert dialog2._note.text() == "unknown task id: zzz"
        assert not any(t.title == "Broken" for t in shell.manager.tasks.values())
        # The "create on hold" checkbox reaches the shell.
        dialog3 = NewTaskDialog(shell)
        dialog3._text.setPlainText("Held via dialog")
        dialog3._hold.setChecked(True)
        assert dialog3._submit() is True
        held = next(
            t for t in shell.manager.tasks.values() if t.title == "Held via dialog"
        )
        assert held.status is TaskStatus.HOLD
        for widget in (view, dialog, dialog2, dialog3):
            widget.deleteLater()

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)
