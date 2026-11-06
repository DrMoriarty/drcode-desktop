"""The chat pane (plan task 3): the viewed task's transcript + live stream.

``ChatTimelineView`` renders the :mod:`chat_timeline_model` timeline of
the selected task as a scrollable column of block widgets
(:mod:`chat_blocks`), one QTextBrowser per entry — the GUI twin of the
TUI's chat log. Interactive parity the pane carries now:

- the collapsible reasoning / tool-call blocks (▸ click to expand, the
  tool arguments shown as highlighted JSON);
- the task-id mention links (#69c8bd30) — clicking opens the mentioned
  task (the ``open_task_requested`` signal);
- live streaming: the view is the QtHost's stream sink, so a run on the
  viewed task grows reasoning/assistant/tool-call blocks in place while
  the deltas arrive (the per-block flush throttling keeps ~50 deltas/s
  off the render path); the tail follows the stream only while the
  reader is at the bottom (the TUI's follow rule).

Updates ride the hub: ``task_updated``/``service_event`` for the viewed
task trigger a debounced rebuild (the node boundaries persist the
transcript, then the timeline covers the finished blocks).

Plan task 4 adds the interactive half: the message composer (Enter —
send, Shift+Enter — newline, Esc — stop the run, Ctrl+R — resume it)
and the clickable options of a pending clarification package. Both
route to :class:`drcodedesktop.core_shell.CoreShell` — the state
semantics live in the shell, the pane only forwards.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .chat_blocks import (
    CHAT_BG,
    ChatBlockBrowser,
    StreamBlockBrowser,
    _right_stamped,
    set_chat_font,
    tool_header_parts,
)
from .chat_input import ChatInput
from .chat_timeline_model import (
    MIN_TIME,
    RenderedTimeline,
    TimelineEntry,
    clarification_entries,
)
from .rich_to_qt import (
    json_body_html,
    markdown_body_html,
    mention_text_html,
    segments_html,
)
from .todo_panel import TodoPanel

if TYPE_CHECKING:
    from .core_shell import CoreShell
    from .event_hub import EventHub

#: Debounce of the rebuild-on-task_updated (node boundaries save the
#: transcript a few times per turn — coalesce the rebuilds).
_REBUILD_DEBOUNCE_MS = 250
#: Rows below the viewport edge that still count as "at the bottom"
#: (the TUI's _SCROLL_FOLLOW_THRESHOLD).
_FOLLOW_THRESHOLD = 8

_MAGENTA = "#d670d6"
_RED = "#cd3131"


class ChatTimelineView(QWidget):
    """The chat pane of the Queue tab: transcript + live stream."""

    #: The user clicked a task-id mention (#69c8bd30) — open that task.
    open_task_requested = Signal(str)

    def __init__(self, shell: CoreShell, hub: EventHub) -> None:
        super().__init__()
        self._shell = shell
        self._hub = hub
        self._task_id: str | None = None
        # The timeline snapshot the current widgets render (the collapse
        # toggle re-renders single entries from it).
        self._entries: list[TimelineEntry] = []
        # Live stream blocks mounted in this view (the QtHost sink hands
        # them over as the pipeline creates them; a rebuild drops the
        # ones the persisted transcript already covers).
        self._live_blocks: list[StreamBlockBrowser] = []
        # Per-entry collapse state of the history (reasoning / tool-call),
        # keyed by timeline index — survives the debounced rebuilds.
        self._expanded: set[int] = set()
        # Collapsible widgets by timeline index (the toggle re-renders
        # through this map).
        self._collapsible: dict[int, ChatBlockBrowser] = {}
        self._rendered_clarification: object | None = None
        # The clarification-answer draft state (the TUI's
        # _clarification_picks / _clarification_multi): per-question
        # free-text overrides and the accumulated multiple-choice sets,
        # keyed by question index — survives the debounced rebuilds,
        # reset when the package changes (view_task / a new package).
        self._clarification_picks: dict[int, str] = {}
        self._clarification_multi: dict[int, set[int]] = {}

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._container = QWidget()
        self._container.setStyleSheet(f"background-color: {CHAT_BG};")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(8, 6, 8, 6)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self._scroll.setWidget(self._container)

        # The input row (plan task 4): the composer + the run controls.
        # The buttons always fire — the shell's stop/resume report a
        # no-op through the notify seam instead of the view guessing the
        # run state from a snapshot.
        self._input = ChatInput()
        self._input.submitted.connect(self._submit_input)
        self._input.stop_requested.connect(self._stop_run)
        self._input.resume_requested.connect(self._resume_run)
        self._stop_button = QPushButton("⏹ Stop")
        self._stop_button.setToolTip("Stop the viewed task's run (Esc)")
        self._stop_button.clicked.connect(self._stop_run)
        self._resume_button = QPushButton("↻ Resume")
        self._resume_button.setToolTip(
            "Abort a hung run and restart it / resume an aborted attempt (Ctrl+R)"
        )
        self._resume_button.clicked.connect(self._resume_run)
        input_row = QHBoxLayout()
        input_row.addWidget(self._stop_button)
        input_row.addWidget(self._resume_button)
        input_row.addWidget(self._input, stretch=1)
        input_row.setContentsMargins(0, 4, 0, 0)

        # The todo checklist strip (plan task 7): sits between the chat
        # history and the input row (the TUI's #todo-panel slot), driven
        # by the same task_updated/view_task events as the rebuild.
        self._todo_panel = TodoPanel()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll, stretch=1)
        outer.addWidget(self._todo_panel)
        outer.addLayout(input_row)

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(_REBUILD_DEBOUNCE_MS)
        self._rebuild_timer.timeout.connect(self._rebuild)

        self._set_empty_hint()
        hub.task_updated.connect(self._on_task_updated)
        hub.service_event.connect(self._on_service_event)
        hub.task_removed.connect(self._on_task_removed)

    # -- public surface ----------------------------------------------------------

    @property
    def task_id(self) -> str | None:
        return self._task_id

    def view_task(self, task_id: str) -> None:
        """Show the task's transcript (full rebuild; live stream blocks
        of this task survive — see ``_reconcile_live_blocks``)."""
        self._task_id = task_id
        self._expanded.clear()
        self._rendered_clarification = None
        self._clarification_picks.clear()
        self._clarification_multi.clear()
        self._rebuild()

    def apply_font(self, font: QFont) -> None:
        """The Preferences Apply: restyle every mounted block (history,
        live stream, the empty-pane hint) and set the font future blocks
        render with (the module-level default in chat_blocks)."""
        set_chat_font(font)
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if isinstance(widget, ChatBlockBrowser):
                widget.apply_font(font)
        for block in self._live_blocks:
            block.apply_font(font)

    def clear_view(self) -> None:
        """No task selected: an empty pane (detaches live blocks)."""
        self._task_id = None
        self._entries = []
        self._clear_layout()
        self._todo_panel.update_todos(None, [])
        self._set_empty_hint()

    def mount_stream_block(self, block: StreamBlockBrowser) -> None:
        """The QtHost sink: a live block of the viewed task was created —
        mount it after the history (reasoning/assistant/tool-call)."""
        if self._task_id is None or block.task_id != self._task_id:
            return
        self._live_blocks.append(block)
        block.anchor_activated.connect(self._on_anchor)
        self._insert_widget(block)
        self._follow_tail(force=True)

    def live_history_line(self, role: str, text: str, at: object = None) -> None:
        """The CoreShell sink for the pipeline's live history writes
        (consumed prompts, round separators): append the entry without a
        full rebuild — the next rebuild renders it from the timeline."""
        if self._task_id is None:
            return
        entry = TimelineEntry(
            at if isinstance(at, datetime) else MIN_TIME, 10**9, role, text
        )
        self._insert_widget(self._entry_block(entry, -1))
        self._follow_tail()

    def live_clarification(self, clarification: object) -> None:
        """The pipeline asked a clarifying question: append the package."""
        if self._task_id is None or clarification is self._rendered_clarification:
            return
        self._rendered_clarification = clarification
        self._clarification_picks.clear()
        self._clarification_multi.clear()
        for line in clarification_entries(clarification):  # type: ignore[arg-type]
            self.live_history_line("system", line)

    def stop_streaming(self) -> None:
        """The shell's ``_clear_stream_label`` sink: finish every live
        block (spinner off, tail flushed) — an aborted/superseded run
        must not leave a spinning block behind."""
        for block in self._live_blocks:
            block.finish()

    def shutdown(self) -> None:
        """Project closed: detach from the hub, stop the timers and
        schedule the pane's own deletion — the widget tree (transcript
        blocks, composer, run controls) must never outlive the
        application object (a hard Qt crash at teardown otherwise)."""
        self._rebuild_timer.stop()
        self._todo_panel.shutdown()
        self._clear_layout()
        with contextlib.suppress(RuntimeError):
            self._hub.task_updated.disconnect(self._on_task_updated)
            self._hub.service_event.disconnect(self._on_service_event)
            self._hub.task_removed.disconnect(self._on_task_removed)
        self.deleteLater()

    # -- hub paths ---------------------------------------------------------------

    def _on_task_updated(self, task_id: str, status: str) -> None:
        del status
        if task_id == self._task_id:
            self._rebuild_timer.start()

    def _on_service_event(self, task_id: str, kind: str, text: str) -> None:
        del kind, text
        if task_id == self._task_id:
            self._rebuild_timer.start()

    def _on_task_removed(self, task_id: str) -> None:
        """The viewed task was deleted: the pane goes empty (the TUI
        switches to the queue neighbour; here the queue selection change
        drives the next view_task)."""
        if task_id == self._task_id:
            self.clear_view()

    # -- rendering ----------------------------------------------------------------

    def _clear_layout(self) -> None:
        for block in self._live_blocks:
            block.detach()
        self._live_blocks.clear()
        self._collapsible = {}
        while self._layout.count() > 1:  # keep the trailing stretch
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _insert_widget(self, widget: QWidget) -> None:
        """Append a widget above the trailing stretch."""
        self._layout.insertWidget(self._layout.count() - 1, widget)

    def _set_empty_hint(self) -> None:
        hint = ChatBlockBrowser()
        hint.set_block_html(
            '<span style="color:#6b7280">Select a task in the queue to '
            "see its transcript.</span>"
        )
        self._insert_widget(hint)

    def _rebuild(self) -> None:
        """Full re-render from the task's current state."""
        if self._task_id is None:
            return
        task = self._shell.manager.tasks.get(self._task_id)
        if task is None:
            self.clear_view()
            return
        follow = self._at_bottom()
        rendered = RenderedTimeline.for_task(
            task, consumed_prompts=self._shell._consumed_prompts
        )
        self._clear_layout()
        self._entries = rendered.entries
        for index, entry in enumerate(rendered.entries):
            self._insert_widget(self._entry_block(entry, index))
        for line in rendered.clarification:
            self._insert_widget(self._line_block("system", line))
        if rendered.clarification:
            # A fresh package replaced a previously rendered one: drop
            # the accumulated answer draft (the picks refer to the old
            # questions' indexes).
            if task.clarification is not self._rendered_clarification:
                self._rendered_clarification = task.clarification
                self._clarification_picks.clear()
                self._clarification_multi.clear()
            self._mount_option_buttons(task.clarification)
        self._reconcile_live_blocks(rendered.entries)
        # The todo checklist strip rides the same rebuild (the
        # todo_create / todo_update tool calls persist through
        # task_updated, which schedules the rebuild).
        self._todo_panel.update_todos(task.id, task.todos)
        if follow:
            self._follow_tail(force=True)

    def _entry_block(self, entry: TimelineEntry, index: int) -> ChatBlockBrowser:
        """One timeline entry → a block widget (index -1: a live append
        not present in the snapshot — no collapse bookkeeping)."""
        at = None if entry.at == MIN_TIME else entry.at
        role, text = entry.role, entry.text
        if role in ("user", "user-pending"):
            return self._user_block(text)
        if role == "reasoning":
            return self._collapsible_block(
                index, "Reasoning", markdown_body_html(text), at
            )
        if role == "tool-call":
            header_text, args = tool_header_parts(text)
            body = json_body_html(args) if args else ""
            return self._collapsible_block(index, f"⚙ {header_text}", body, at)
        if role in ("assistant", "summary"):
            return self._markdown_block(text)
        return self._line_block(role, text, at)

    def _user_block(self, text: str) -> ChatBlockBrowser:
        block = ChatBlockBrowser()
        block.set_block_html(
            '<table width="100%" cellspacing="0" cellpadding="4">'
            '<tr><td style="background-color:#1d2027; '
            'border-left:3px solid #3b8eea; padding-left:6px">'
            f"{mention_text_html(text)}</td></tr></table>"
        )
        block.anchor_activated.connect(self._on_anchor)
        return block

    def _markdown_block(self, text: str) -> ChatBlockBrowser:
        block = ChatBlockBrowser()
        block.set_block_html(
            f'<div style="white-space:pre-wrap">{markdown_body_html(text)}</div>'
        )
        block.anchor_activated.connect(self._on_anchor)
        return block

    def _collapsible_block(
        self, index: int, label: str, body: str, at: object
    ) -> ChatBlockBrowser:
        """A collapsed-by-default reasoning/tool-call entry: one widget,
        the collapse toggle re-renders it in place (state keyed by the
        timeline index, so a rebuild preserves the open blocks)."""
        block = ChatBlockBrowser()
        block.anchor_activated.connect(self._on_anchor)
        self._render_collapse(block, index, label, body, at)
        if index >= 0:
            self._collapsible[index] = block
        return block

    def _render_collapse(
        self, block: ChatBlockBrowser, index: int, label: str, body: str, at: object
    ) -> None:
        expanded = index in self._expanded
        arrow = "▾" if expanded else "▸"
        header = mention_text_html(f"{arrow} {label}")
        html = _right_stamped(
            f'<a href="drcode:toggle:{index}">{header}</a>',
            at if isinstance(at, datetime) else None,
        )
        if expanded:
            html += f'<div style="white-space:pre-wrap">{body}</div>'
        block.set_block_html(html)

    def _line_block(self, role: str, text: str, at: object = None) -> ChatBlockBrowser:
        """The markup lines of the TUI, as explicit styled segments."""
        stamp = at if isinstance(at, datetime) else None
        if role == "tester":
            html = segments_html(("tester › ", f"bold {_MAGENTA}"), (text, ""))
        elif role == "testing-sep":
            label = f"testing: {text}" if text else "testing"
            html = segments_html((f"──── {label} ────", f"bold {_MAGENTA}"))
        elif role == "resolving-sep":
            label = f"conflict resolution: {text}" if text else "conflict resolution"
            html = segments_html((f"──── {label} ────", f"bold {_MAGENTA}"))
        elif role == "event":
            html = segments_html(("event › ", f"bold {_RED}"), (text, ""))
        elif role == "system":
            html = segments_html(("error › ", f"bold {_RED}"), (text, ""))
        else:  # "tool" and anything unstyled: dim plain text with mentions
            html = mention_text_html(text)
        block = ChatBlockBrowser()
        block.set_block_html(_right_stamped(html, stamp))
        block.anchor_activated.connect(self._on_anchor)
        return block

    def _reconcile_live_blocks(self, entries: list[TimelineEntry]) -> None:
        """Keep the still-streaming / not-yet-persisted live blocks: drop
        the ones the rebuilt transcript now covers (their text matches a
        timeline entry of the same role — the node boundary persisted
        it), remount the rest after the history."""
        covered: dict[str, list[str]] = {
            "reasoning": [],
            "assistant": [],
            "tool-call": [],
        }
        for entry in entries:
            if entry.role in covered:
                covered[entry.role].append(entry.text)
        kept: list[StreamBlockBrowser] = []
        for block in self._live_blocks:
            content = block.get_content()
            texts = covered.get(block.kind, [])
            if not block.is_streaming and content in texts:
                texts.remove(content)
                block.detach()
                continue
            block.anchor_activated.connect(self._on_anchor)
            self._insert_widget(block)
            kept.append(block)
        self._live_blocks = kept

    def _mount_option_buttons(self, clarification: object) -> None:
        """The clickable options of the pending clarification package
        (the TUI's starred option block): one button per combined option
        — options first, then the agent proposals with a ★. A click on a
        flat single question submits immediately; a package's /
        multiple-choice clicks accumulate into the input draft (see
        ``_pick_option``)."""
        questions = getattr(clarification, "questions", [])
        for qi, question in enumerate(questions):
            options = question.combined_options
            if not options:
                continue
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(16, 0, 0, 0)
            row_lay.setSpacing(4)
            for oi, option in enumerate(options):
                star = "★ " if oi >= len(question.options) else ""
                button = QPushButton(f"{star}{option}")
                button.setToolTip(f"Answer {qi + 1}: {option}")
                button.clicked.connect(
                    lambda _checked=False, a=qi, b=oi: self._pick_option(a, b)
                )
                row_lay.addWidget(button)
            row_lay.addStretch(1)
            self._insert_widget(row)

    # -- interaction ---------------------------------------------------------------

    def _submit_input(self, raw: str) -> None:
        """The chat composer's Enter: submit the draft to the viewed
        task — the GUI port of the TUI's ``submit_chat_input``. An empty
        Enter over an open MULTIPLE-choice clarification is a valid
        "nothing selected" answer ("(nothing)", the tool docstring tells
        the agent so); without one an empty input stays a no-op."""
        if self._task_id is None:
            return
        task = self._shell.manager.tasks.get(self._task_id)
        if task is None:
            return
        stripped = raw.strip()
        if not stripped:
            if task.clarification is not None and any(
                q.multiple for q in task.clarification.questions
            ):
                stripped = "(nothing)"
            else:
                return
        self._input.clear()
        self._clarification_picks.clear()
        self._clarification_multi.clear()
        self._shell.submit_user_text(self._task_id, stripped)

    def _pick_option(self, question_index: int, option_index: int) -> None:
        """A clarification option was clicked — the port of the TUI's
        ``action_pick_option`` (the flat one-question flow plus the
        package/multiple draft accumulation; the typed-line adoption is
        kept in a simplified form: a manually edited question line wins
        over the click set of a multiple question).

        A single-question package of a NON-multiple question submits the
        option text immediately (the legacy one-question flow). A
        batched package's click does NOT send alone: it fills the
        question's line in the composer (one line per question, the
        option text and/or the user's free text), and Enter submits all
        the lines as ONE message. A MULTIPLE-choice question never
        submits on click: the click TOGGLES the option in the
        question's accumulated set, re-rendered into the question's line
        as a "; "-joined text; Enter submits the draft as usual (an
        empty one means "nothing selected")."""
        if self._task_id is None:
            return
        task = self._shell.manager.tasks.get(self._task_id)
        if task is None or task.clarification is None:
            return
        questions = task.clarification.questions
        if not 0 <= question_index < len(questions):
            return
        combined = questions[question_index].combined_options
        if not 0 <= option_index < len(combined):
            return
        text = combined[option_index]
        if len(questions) == 1 and not questions[0].multiple:
            self._input.clear()
            self._clarification_picks.clear()
            self._clarification_multi.clear()
            self._shell.submit_user_text(self._task_id, text)
            return

        def _joined(i: int) -> str:
            """The "; "-joined rendering of question i's click set."""
            opts = questions[i].combined_options
            return "; ".join(
                opts[j] for j in sorted(self._clarification_multi.get(i, set()))
            )

        # Adopt the currently typed lines first (free-text answers
        # already typed into question lines): a non-empty line that is
        # not the click set's own rendering is a manual override.
        lines = self._input.toPlainText().splitlines()
        for i in range(min(len(lines), len(questions))):
            line = lines[i].strip()
            if not line or (questions[i].multiple and line == _joined(i)):
                continue
            self._clarification_picks[i] = lines[i]
        if questions[question_index].multiple:
            # A click on the question itself expresses "use the click
            # set": drop any manual override for it, then toggle.
            self._clarification_picks.pop(question_index, None)
            selected = self._clarification_multi.setdefault(question_index, set())
            if option_index in selected:
                selected.discard(option_index)
            else:
                selected.add(option_index)
        else:
            self._clarification_picks[question_index] = text
        draft: list[str] = []
        for i, q in enumerate(questions):
            if q.multiple:
                manual = self._clarification_picks.get(i)
                draft.append(manual if manual is not None else _joined(i))
            else:
                draft.append(self._clarification_picks.get(i, ""))
        while draft and not draft[-1].strip():
            draft.pop()
        self._input.setPlainText("\n".join(draft))
        self._input.setFocus()

    def _stop_run(self) -> None:
        """Stop the viewed task's run (Esc / ⏹ Stop): the shell decides
        what the stop means (single slot — the live run; parallel slots —
        the viewed task's own) and reports a no-op itself."""
        self._shell.stop_viewed_run()

    def _resume_run(self) -> None:
        """Resume the viewed task's run (Ctrl+R / ↻ Resume): the shell's
        nudge — abort a hung run and restart it, or resume an aborted
        attempt as a continuation turn."""
        self._shell.resume_viewed_run()

    def _on_anchor(self, href: str) -> None:
        if href.startswith("drcode:toggle:"):
            with contextlib.suppress(ValueError):
                index = int(href.rsplit(":", 1)[1])
                if index in self._expanded:
                    self._expanded.discard(index)
                else:
                    self._expanded.add(index)
                self._rerender_entry(index)
            return
        if href.startswith("drcode:task/"):
            self.open_task_requested.emit(href.rsplit("/", 1)[1])

    def _rerender_entry(self, index: int) -> None:
        """Re-render one history entry in place (the collapse toggle)."""
        old = self._collapsible.get(index)
        if not 0 <= index < len(self._entries) or old is None:
            return
        entry = self._entries[index]
        at = None if entry.at == MIN_TIME else entry.at
        role, text = entry.role, entry.text
        if role == "reasoning":
            label, body = "Reasoning", markdown_body_html(text)
        elif role == "tool-call":
            header_text, args = tool_header_parts(text)
            label, body = f"⚙ {header_text}", json_body_html(args) if args else ""
        else:
            return
        fresh = ChatBlockBrowser()
        fresh.anchor_activated.connect(self._on_anchor)
        self._render_collapse(fresh, index, label, body, at)
        self._collapsible[index] = fresh
        layout_index = self._layout.indexOf(old)
        if layout_index >= 0:
            self._layout.takeAt(layout_index)
            old.deleteLater()
            self._layout.insertWidget(layout_index, fresh)

    # -- scroll follow ----------------------------------------------------------------

    def _at_bottom(self) -> bool:
        bar = self._scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - _FOLLOW_THRESHOLD

    def _follow_tail(self, force: bool = False) -> None:
        """Keep the tail visible while streaming — only for a reader who
        was tracking it (the TUI's _stream_follow rule)."""
        if force or self._at_bottom():
            bar = self._scroll.verticalScrollBar()
            QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
