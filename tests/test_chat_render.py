"""Plan task 3 acceptance tests: the Rich→Qt chat render.

Covers:

- the timeline model (the ``_build_task_timeline`` port): transcript +
  tester rounds + service events + pending input merged into one
  chronological role/text stream;
- the Rich→Qt adapter: styles, task-id mention links, markdown with
  fenced code blocks, highlighted JSON tool-call arguments;
- the rendered pane on fixture transcripts: block order, collapsible
  tool calls, testing separators, event lines;
- the StreamBlock protocol on Qt with a ~50 deltas/s feed: content
  complete, repaints batched by the 100 ms flush (the stream must not
  saturate the UI), the sink wiring of the QtHost.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from drcode.agent import serialize_messages
from drcode.models import (
    Clarification,
    ClarificationItem,
    PendingInput,
    Task,
    TaskEvent,
    TaskStatus,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from drcodedesktop.chat_blocks import ChatBlockBrowser, StreamBlockBrowser
from drcodedesktop.chat_timeline_model import build_timeline
from drcodedesktop.chat_view import ChatTimelineView
from drcodedesktop.event_hub import EventHub
from drcodedesktop.qt_host import QtHost
from drcodedesktop.rich_to_qt import (
    json_body_html,
    markdown_body_html,
    mention_text_html,
    rich_text_to_html,
)

_BASE = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)


def _stamp(minutes: float) -> datetime:
    return _BASE + timedelta(minutes=minutes)


# -- fixture transcripts (the drcode/tests/test_agent.py shapes) ---------------


def user_msg(text: str, at: datetime) -> ModelRequest:
    # The user-prompt PART stamp wins in extract_display (the TUI's
    # "resumed request uses prompt stamp" rule) — stamp it explicitly.
    return ModelRequest(
        parts=[UserPromptPart(content=text, timestamp=at)], timestamp=at
    )


def text_response(text: str, at: datetime) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)], timestamp=at)


def thinking_response(thinking: str, text: str, at: datetime) -> ModelResponse:
    return ModelResponse(
        parts=[ThinkingPart(content=thinking), TextPart(content=text)], timestamp=at
    )


def tool_call_response(tool: str, args: dict[str, Any], at: datetime) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id="c1")], timestamp=at
    )


def tool_return_msg(content: str, at: datetime) -> ModelRequest:
    return ModelRequest(
        parts=[
            ToolReturnPart(
                tool_name="run_bash", content=content, tool_call_id="c1", timestamp=at
            )
        ],
        timestamp=at,
    )


CODE_BLOCK = '```python\nprint("hello <world>")\n```'


def fixture_task() -> Task:
    """A transcript covering every block kind the TUI renders (stamps
    run 10:00–10:05; the pending comment is typed at 11:00, the service
    event happens at 12:00 — the chronological merge places them at the
    tail, exactly like the TUI)."""
    task = Task(title="Render parity")
    task.messages = serialize_messages(
        [
            user_msg("Fix the widget, see #d0e5df1a", _stamp(0)),
            thinking_response(
                "Let me look at the code.", "On it — working.", _stamp(1)
            ),
            tool_call_response("run_bash", {"command": "ls -la"}, _stamp(2)),
            tool_return_msg("total 0", _stamp(2.5)),
            text_response(f"Done.\n\n{CODE_BLOCK}", _stamp(3)),
        ]
    )
    task.tester_messages = [
        serialize_messages(
            [
                user_msg("Verify completed task", _stamp(4)),
                tool_call_response("run_bash", {"command": "pytest"}, _stamp(4.5)),
                tool_return_msg("1 passed", _stamp(5)),
                text_response("Looks good.", _stamp(5.5)),
            ]
        )
    ]
    task.events.append(TaskEvent(at=_stamp(60), text="Network retry"))
    task.pending_input = [
        PendingInput(text="One more comment", at=_stamp(30))
    ]
    return task


def make_view(task: Task) -> tuple[ChatTimelineView, EventHub]:
    """A chat pane over a task fixture (no core start: the view reads
    the manager's task objects and the shell's consumed prompts)."""
    app = QApplication.instance() or QApplication([])
    del app
    hub = EventHub(log_path=None)
    shell = SimpleNamespace(
        manager=SimpleNamespace(tasks={task.id: task}), _consumed_prompts={}
    )
    view = ChatTimelineView(shell, hub)  # type: ignore[arg-type]
    return view, hub


def pane_texts(view: ChatTimelineView) -> list[str]:
    texts = []
    layout = view._layout
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if isinstance(widget, ChatBlockBrowser):
            texts.append(widget.toPlainText())
    return [t for t in texts if t.strip()]


# -- timeline model -------------------------------------------------------------


def test_timeline_merges_every_stream_chronologically() -> None:
    task = fixture_task()
    entries = build_timeline(task)
    roles = [e.role for e in entries]
    # user → reasoning → assistant → tool-call → the tester round (its
    # separator first) → the pending comment (typed at 11:00) → the event.
    assert roles[0] == "user"
    assert roles[1] == "reasoning"
    assert roles[2] == "assistant"
    assert roles[3] == "tool-call"
    assert "testing-sep" in roles
    assert roles[-2:] == ["user-pending", "event"]
    pending = [e for e in entries if e.role == "user-pending"]
    assert [e.text for e in pending] == ["One more comment"]
    # The synthetic tester prompt is hidden; its tool calls are prefixed.
    assert all(e.text != "Verify completed task" for e in entries)
    tester_calls = [e for e in entries if e.text.startswith("tester: ")]
    assert any("pytest" in e.text for e in tester_calls)
    # Chronological order overall.
    stamps = [e.at for e in entries]
    assert stamps == sorted(stamps)


def test_timeline_consumed_prompt_rendered_once() -> None:
    task = fixture_task()
    prompt = task.pending_input[0].text
    # An interrupted attempt already persisted the prompt into the
    # transcript (it died mid-tool-call): the pending copy must not
    # duplicate it.
    task.messages = serialize_messages(
        [user_msg(prompt, _stamp(0)), tool_call_response("run_bash", {}, _stamp(1))]
    )
    entries = build_timeline(task)
    users = [e.text for e in entries if e.role in ("user", "user-pending")]
    assert users == [prompt]


# -- Rich → Qt adapter ----------------------------------------------------------


def test_rich_text_styles_map_to_html() -> None:
    from rich.text import Text

    text = Text("hello world")
    text.stylize("bold red", 0, 5)
    html = rich_text_to_html(text)
    assert "font-weight:bold" in html
    assert "color:#cd3131" in html
    assert "hello" in html and "world" in html


def test_mention_becomes_a_task_link() -> None:
    html = mention_text_html("See #D0E5DF1A and #nosuch (not an id: #12345)")
    # The link keeps the mention's original case; the href is lowercase.
    assert '<a href="drcode:task/d0e5df1a">#D0E5DF1A</a>' in html
    assert "#nosuch" in html
    assert "#12345" in html


def test_markdown_renders_code_blocks_with_the_tui_builder() -> None:
    html = markdown_body_html(CODE_BLOCK)
    # The fenced block is highlighted (pygments spans from the same
    # rich builder the TUI uses) and HTML-special chars survive.
    assert "hello &lt;world&gt;" in html
    assert "color:#" in html  # syntax colors present


def test_json_body_highlighted() -> None:
    html = json_body_html(json.dumps({"command": "ls", "path": "a.py"}))
    assert "command" in html
    assert "color:#" in html  # the pygments palette
    assert "\n" in html or "<br>" in html  # re-indented for readability


# -- rendered pane ----------------------------------------------------------------


def test_pane_renders_fixture_transcript() -> None:
    task = fixture_task()
    view, hub = make_view(task)
    view.view_task(task.id)
    texts = pane_texts(view)
    joined = "\n".join(texts)
    # Order parity with the TUI: prompt, reasoning, answer, tool call,
    # the testing separator, the tester output, the pending comment, the
    # event line.
    assert texts[0].strip().startswith("Fix the widget")
    assert any("Reasoning" in t for t in texts)
    assert any("Done." in t for t in texts)
    assert any("run_bash" in t and "⚙" in t for t in texts)
    assert any("──── testing ────" in t for t in texts)
    assert any("tester: run_bash" in t for t in texts)
    assert any("One more comment" in t for t in texts)
    assert any("event ›" in t and "Network retry" in t for t in texts)
    assert _stamp(60).astimezone().strftime("%Y-%m-%d %H:%M:%S") in joined
    # The collapsed tool call hides its JSON body (the TUI default).
    tool = next(t for t in texts if "⚙" in t)
    assert "ls -la" in tool
    assert "command" not in tool
    # The mention is a link in the rendered HTML.
    layout = view._layout
    html = "\n".join(
        widget.toHtml()
        for i in range(layout.count())
        if isinstance(widget := layout.itemAt(i).widget(), ChatBlockBrowser)
    )
    assert "drcode:task/d0e5df1a" in html
    # The hub path (a task_updated for the viewed task) is safe.
    hub.emit_task_updated(task.id, task.status.value)
    view.shutdown()


def test_pane_collapsible_tool_call_expands() -> None:
    task = fixture_task()
    view, _hub = make_view(task)
    view.view_task(task.id)
    tool_index = next(i for i, e in enumerate(view._entries) if e.role == "tool-call")
    view._expanded.add(tool_index)
    view._rerender_entry(tool_index)
    expanded = "\n".join(pane_texts(view))
    assert '"command"' in expanded and "ls -la" in expanded
    view.shutdown()


def test_pane_renders_clarification_package() -> None:
    task = Task(title="Waiting")
    task.status = TaskStatus.AWAITING_CLARIFICATION
    task.clarification = Clarification(
        questions=[
            ClarificationItem(
                question="Which variant?",
                options=["A", "B"],
                agent_proposals=["C"],
            )
        ]
    )
    view, _hub = make_view(task)
    view.view_task(task.id)
    texts = "\n".join(pane_texts(view))
    assert "Which variant?" in texts
    assert "▸ A" in texts and "★" in texts
    view.shutdown()


# -- StreamBlock protocol on Qt ----------------------------------------------------


def run_qasync(scenario: Callable[[], Any]) -> None:
    """Drive `scenario` on the qasync loop: Qt timers fire during the
    awaits (the same driver pattern the core tests use)."""
    app = QApplication.instance() or QApplication([])
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(scenario())


def test_stream_block_protocol_batches_repaints() -> None:
    """~50 deltas/s: content complete, redraws batched by the flush."""

    async def scenario() -> None:
        block = StreamBlockBrowser("assistant")
        block.stream_epoch = 0
        original = block._redraw
        count = {"n": 0}

        def counted() -> None:
            count["n"] += 1
            original()

        block._redraw = counted  # type: ignore[method-assign]
        block.begin()
        chunk = "x" * 10
        # 50 deltas/s for 2 seconds = 100 deltas.
        for _ in range(100):
            block.append(chunk)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)  # let the last flush land
        assert block.get_content() == chunk * 100
        # The 100 ms flush batching: ~20 repaints for 100 deltas.
        assert count["n"] < 40, count["n"]
        block.finish()
        assert not block.is_streaming
        assert "xxxxx" in block.toPlainText()

    run_qasync(scenario)


def test_stream_block_reasoning_spinner_and_collapse() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    block = StreamBlockBrowser("reasoning", "t1")
    assert block.collapsed  # the TUI default
    block.begin()
    block.append("thinking hard about ")
    block.append("the plan")
    block.finish()
    assert "Reasoning" in block.toPlainText()
    assert block.get_content() == "thinking hard about the plan"
    block.toggle()
    assert not block.collapsed
    assert "thinking hard" in block.toPlainText()
    block.set_content("replaced")
    assert block.get_content() == "replaced"
    block.detach()  # no timers left ticking


def test_shell_view_task_and_sink_end_to_end(tmp_path: Any) -> None:
    """The real shell wires the pane: view_task attributes the host's
    live blocks to the selected task and they mount into the view."""
    from drcodedesktop.core_shell import CoreShell

    app = QApplication.instance() or QApplication([])
    del app
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hub = EventHub(log_path=None)
    host = QtHost(hub)
    shell = CoreShell(project_root, host, hub)
    try:
        task = shell.manager.create("Pane integration", "d", origin="user")
        task.messages = serialize_messages(
            [user_msg("Fix the widget", _stamp(0)), text_response("Done", _stamp(1))]
        )
        view = ChatTimelineView(shell, hub)
        host.set_stream_sink(view)
        view.view_task(task.id)
        shell.view_task(task.id)
        # A live run on the viewed task: the host's block mounts and grows.
        block = host.host_new_assistant_block()
        block.begin()
        block.append("live ")
        block.append("stream text")
        block.finish()
        texts = pane_texts(view)
        assert texts[0].strip().startswith("Fix the widget")
        assert "live stream text" in texts[-1]
        # The hub path (a task_updated for the viewed task) schedules the
        # debounced rebuild without a crash.
        hub.emit_task_updated(task.id, task.status.value)
        view.shutdown()
    finally:
        shell.shutdown()


def test_qt_host_routes_blocks_to_the_sink() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    hub = EventHub(log_path=None)
    host = QtHost(hub)
    mounted: list[StreamBlockBrowser] = []

    class FakeView:
        task_id = "t1"

        def mount_stream_block(self, block: StreamBlockBrowser) -> None:
            mounted.append(block)

    view = FakeView()
    host.set_stream_sink(view)
    host.current_task_id = "t1"
    reasoning = host.host_new_reasoning_block()
    assistant = host.host_new_assistant_block()
    tool = host.host_new_tool_call_block('edit_file("x.py")')
    assert mounted == [reasoning, assistant, tool]
    assert reasoning.kind == "reasoning"
    assert tool.get_content() == 'edit_file("x.py")'
    # Off-screen blocks (another task) are not mounted.
    host.current_task_id = "other"
    assert host.host_new_assistant_block() not in mounted
    # Closing the sink detaches it.
    host.clear_stream_sink(view)
    assert host._stream_sink is None
