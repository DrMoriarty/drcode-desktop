"""The user-input seam (plan task 4): prompts, clarification answers and
run control driven through the shell exactly as the GUI widgets route
them.

Covers the task-4 acceptance criteria:

- a prompt goes through the core with the TUI semantics: queued input
  (``PendingInput``) is consumed as ONE merged prompt, the duplicate
  re-send is a no-op, and the run completes headless;
- ``ask_clarifying_question`` puts the task into awaiting-clarification
  and an answer typed in the GUI re-queues it (queue head) and resumes
  the run;
- a stopped run (the ESC stop) marks the attempt ``unfinished`` and a
  resume (Ctrl+R) continues it;
- the chat pane's widgets (composer, clarification option buttons)
  reach the same shell methods.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from drcode.agent import all_user_prompts
from drcode.models import TaskStatus
from drcode.settings import Verification
from pydantic_ai.messages import TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from drcodedesktop.chat_view import ChatTimelineView
from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.qt_host import QtHost
from tests import mocks

Setup = Callable[[CoreShell], None]
Scenario = Callable[[CoreShell, EventHub], Awaitable[None]]

CLARIFY_ARGS = {
    "questions": [
        {
            "question": "Какой цвет кнопки?",
            "options": ["красный", "синий"],
            "proposals": ["фиолетовый"],
            "multiple": False,
        }
    ]
}

# The QApplication must outlive every widget the tests create: a widget
# outliving its application is a hard Qt crash at teardown. The harness
# keeps one app per process (each QEventLoop binds to it).
_APP: QApplication | None = None


class EventSink:
    """Collects the hub's emissions (the assertions rarely need them;
    the class keeps the harness shape of test_core_shell)."""

    def __init__(self, hub: EventHub) -> None:
        self.notifications: list[tuple[str, str, str]] = []
        hub.notify.connect(lambda m, t, s: self.notifications.append((m, t, s)))


def run_shell_scenario(
    tmp_path: Path, setup: Setup, scenario: Scenario
) -> tuple[CoreShell, EventHub, EventSink]:
    """Build a shell on a tmp project, run `setup` right after the start
    and drive `scenario` on the qasync loop (Qt timers and workers run).
    `scenario` must deleteLater() every widget it created before
    returning — the awaited sleeps below give the loop the ticks to
    perform the deletions while the application is alive."""
    global _APP
    app: QApplication = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    _APP = app
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    hub = EventHub(log_path=None)
    sink = EventSink(hub)
    shell = CoreShell(project_root, QtHost(hub), hub)

    async def driver() -> None:
        shell.start()
        setup(shell)
        await scenario(shell, hub)
        shell.shutdown()
        # Two loop ticks: the deferred widget deletions (deleteLater)
        # run while the application is alive — never at process exit.
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    with loop:
        loop.run_until_complete(driver())
    return shell, hub, sink


def no_verification(shell: CoreShell) -> None:
    """Skip the tester: a scripted dev run ends in an automatic
    completion (the tester's own scripted rounds are not the subject
    here)."""
    shell.settings.verification = Verification.NONE


async def wait_status(shell: CoreShell, task_id: str, status: TaskStatus) -> None:
    await mocks.wait_until(
        lambda: shell.manager.get(task_id).status is status,
        what=f"task {task_id} reaching {status}",
    )


# -- prompt path --------------------------------------------------------------


def test_prompt_consumed_as_one_merged_prompt_and_completes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [mocks.text_response("готово")]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.manager.create("Feature", "сделай фичу", origin="user")
        shell.view_task(task.id)
        # The original brief is already queued (create put it into
        # pending_input): re-sending it verbatim must be a no-op...
        assert shell.submit_user_text(task.id, "сделай фичу") is False
        assert [e.text for e in task.pending_input] == ["сделай фичу"]
        # ...and a comment joins the queue and starts the run: the model
        # must see the brief and the comment merged into ONE prompt.
        assert shell.submit_user_text(task.id, "и ещё комментарий") is True
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        assert [e.text for e in t.pending_input] == []
        prompts = all_user_prompts(t.messages)
        assert any("сделай фичу" in p for p in prompts)
        assert any("и ещё комментарий" in p for p in prompts)
        assert "сделай фичу" in state.calls[0]
        assert "и ещё комментарий" in state.calls[0]

    run_shell_scenario(tmp_path, no_verification, scenario)


def test_prompt_to_unfinished_active_task_resumes_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Auto-resume unfinished: an ACTIVE task left "unfinished, nothing
    running" (a previous stop) continues as a continuation turn when new
    input arrives — the TUI's was_active branch."""
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [mocks.text_response("готово")]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.manager.create("Feature", "начни работу", origin="user")
        shell.view_task(task.id)
        assert shell.submit_user_text(task.id, "начни работу") is False
        # Simulate the post-stop state (stop_viewed_run's own test
        # covers the real path): interrupted attempt, run bookkeeping
        # already torn down.
        task.unfinished = True
        task.touch()
        shell.manager.storage.save(task)
        assert shell.submit_user_text(task.id, "продолжай") is True
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        assert t.unfinished is False
        prompts = all_user_prompts(t.messages)
        assert any("начни работу" in p for p in prompts)
        assert any("продолжай" in p for p in prompts)

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- clarification path ---------------------------------------------------------


def test_clarification_pauses_and_gui_answer_resumes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [
        mocks.tool_response("ask_clarifying_question", CLARIFY_ARGS),
        # Run 1 makes TWO model requests: the initial one (the tool
        # call) and the turn-end one after the tool result. Run 2 (the
        # answer) consumes the third step.
        mocks.text_response("уточняю"),
        mocks.text_response("готово"),
    ]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        task = shell.manager.create("Feature", "покрась кнопку", origin="user")
        shell.view_task(task.id)
        assert shell.submit_user_text(task.id, "начни") is True
        await wait_status(shell, task.id, TaskStatus.AWAITING_CLARIFICATION)
        t = shell.manager.get(task.id)
        assert t.clarification is not None
        questions = t.clarification.questions
        assert [q.question for q in questions] == ["Какой цвет кнопки?"]
        assert questions[0].options == ["красный", "синий"]
        assert questions[0].agent_proposals == ["фиолетовый"]
        # The run's turn-end request follows the tool call; wait for the
        # run to wind down before counting (the status flips mid-run).
        await mocks.wait_until(
            lambda: not shell.agent_running, what="run 1 winding down"
        )
        # Run 1's two requests saw the same brief (the tool feedback is
        # not a user prompt); the answer run has not happened yet.
        assert len(state.calls) == 2
        assert not shell.tester_running  # pipeline settled on the pause

        # The GUI answer: the same submit seam — send() re-queues the
        # task at the queue head, clears the clarification and the next
        # _advance() resumes development.
        assert shell.submit_user_text(task.id, "синий") is True
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        assert t.clarification is None
        prompts = all_user_prompts(t.messages)
        assert any("синий" in p for p in prompts)
        assert len(state.calls) == 3
        assert "синий" in state.calls[2]

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- stop / resume ---------------------------------------------------------------


def test_stop_marks_unfinished_and_resume_continues(
    tmp_path: Path, monkeypatch: Any
) -> None:
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    # Gate the first model call: the run is mid-flight when the stop
    # fires. The call is recorded BEFORE the gate (the stream has
    # started, the response is pending).
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
        task = shell.manager.create("Feature", "начни работу", origin="user")
        shell.view_task(task.id)
        assert shell.submit_user_text(task.id, "начни работу") is False
        assert shell.submit_user_text(task.id, "старт") is True
        await mocks.wait_until(lambda: len(state.calls) == 1, what="model call")
        await asyncio.sleep(0.05)  # let the run worker reach the gate

        # ESC: the stop marks the interrupted attempt and reports it.
        assert shell.stop_viewed_run() is True
        t = shell.manager.get(task.id)
        assert t.status is TaskStatus.ACTIVE
        assert t.unfinished is True
        assert any("Run aborted by user" in e.text for e in t.events)
        assert not shell.agent_running
        # A stop with nothing running is a no-op.
        assert shell.stop_viewed_run() is False

        # Ctrl+R: the nudge restarts the run; the interrupted attempt
        # resumes (the cancelled run's handler re-queued the consumed
        # prompt into pending_input; the continuation consumes it).
        release.set()
        assert shell.resume_viewed_run() is True
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        assert t.unfinished is False
        prompts = all_user_prompts(t.messages)
        assert any("старт" in p for p in prompts)

    run_shell_scenario(tmp_path, no_verification, scenario)


# -- the widgets route to the same seam --------------------------------------------


def test_chat_view_widgets_submit_and_answer(
    tmp_path: Path, monkeypatch: Any
) -> None:
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    state.steps = [
        mocks.tool_response("ask_clarifying_question", CLARIFY_ARGS),
        mocks.text_response("уточняю"),
        mocks.text_response("готово"),
    ]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.manager.create("Feature", "покрась кнопку", origin="user")
        shell.view_task(task.id)
        view = ChatTimelineView(shell, hub)
        view.view_task(task.id)

        # The composer's Enter path: signal → _submit_input → shell.
        view._input.setPlainText("начни")
        view._input.submitted.emit(view._input.toPlainText())
        await wait_status(shell, task.id, TaskStatus.AWAITING_CLARIFICATION)
        await mocks.wait_until(
            lambda: not shell.agent_running, what="run 1 winding down"
        )
        assert len(state.calls) == 2  # run 1: initial + tool-feedback request

        # A single-question, non-multiple package: the option click
        # submits the answer immediately — "синий" (option index 1).
        view._pick_option(0, 1)
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        prompts = all_user_prompts(t.messages)
        assert any("синий" in p for p in prompts)
        assert "синий" in state.calls[2]
        view.deleteLater()

    run_shell_scenario(tmp_path, no_verification, scenario)


def test_chat_view_package_click_accumulates_draft(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A batched package's click fills the composer draft (one line per
    question) instead of sending; Enter submits the draft as ONE
    message; the multiple-choice clicks toggle."""
    mocks.mock_env(monkeypatch)
    state = mocks.scripted_model(monkeypatch)
    package = {
        "questions": [
            {"question": "Первый вопрос?", "options": ["первый", "второй"]},
            {
                "question": "Второй (несколько)?",
                "options": ["раз", "два", "три"],
                "multiple": True,
            },
        ]
    }
    state.steps = [
        mocks.tool_response("ask_clarifying_question", package),
        mocks.text_response("уточняю"),
        mocks.text_response("готово"),
    ]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.manager.create("Feature", "покрась кнопку", origin="user")
        shell.view_task(task.id)
        assert shell.submit_user_text(task.id, "начни") is True
        await wait_status(shell, task.id, TaskStatus.AWAITING_CLARIFICATION)
        view = ChatTimelineView(shell, hub)
        view.view_task(task.id)
        # A rebuild (task_updated → debounce) mounts the option buttons;
        # wait out the debounce so the state is the production one.
        await asyncio.sleep(0.4)
        await mocks.wait_until(
            lambda: not shell.agent_running, what="run 1 winding down"
        )
        assert len(state.calls) == 2  # run 1: initial + tool-feedback request

        # Package click on question 1: fills the draft, does not send.
        view._pick_option(0, 0)
        assert view._input.toPlainText() == "первый"
        assert shell.manager.get(task.id).status is TaskStatus.AWAITING_CLARIFICATION
        # Multiple-choice click on question 2: toggles into its line.
        view._pick_option(1, 1)
        draft = view._input.toPlainText().splitlines()
        assert draft[0] == "первый"
        assert draft[1] == "два"
        # A second click on the same option toggles it OFF (the line
        # empties and, being trailing, is trimmed from the draft).
        view._pick_option(1, 1)
        assert view._input.toPlainText() == "первый"
        # A click on a multiple question expresses "use the click set":
        # a manually typed line is dropped and the option toggles in
        # (the TUI's documented rule).
        lines = [*view._input.toPlainText().splitlines(), ""]
        lines[1] = "два вручную"
        view._input.setPlainText("\n".join(lines))
        view._pick_option(1, 0)
        # The accumulated click set survives across clicks (the earlier
        # "two" was toggled off, "one" is on now): the draft line is the
        # click set's rendering.
        assert view._input.toPlainText().splitlines()[1] == "раз"
        # Enter: the whole draft as ONE message.
        view._input.submitted.emit(view._input.toPlainText())
        await wait_status(shell, task.id, TaskStatus.COMPLETED)
        t = shell.manager.get(task.id)
        prompts = all_user_prompts(t.messages)
        assert any("первый" in p and "раз" in p for p in prompts)
        view.deleteLater()

    run_shell_scenario(tmp_path, no_verification, scenario)
