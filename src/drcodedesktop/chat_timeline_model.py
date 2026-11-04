"""UI-agnostic timeline of a task's transcript (plan task 3).

The direct port of ``drcode.tui.chat_timeline._build_task_timeline`` —
the chronological merge of every timestamped stream a task's chat
thread is built from — with the Textual machinery stripped out:

- the dev transcript (serialized pydantic-ai ``ModelMessages`` →
  ``extract_display`` triples);
- the tester / conflict-resolver rounds (own separator, prefixed tool
  calls, the synthetic assignment prompt hidden);
- the service events (task.events — human-only lines, interleaved by
  the time they happened);
- the queued user input (``pending_input`` placed by the moment it was
  TYPED, plus the in-flight consumed prompts of a running agent).

The result is a flat list of :class:`TimelineEntry` values
(timestamp, role, text) — no widgets, no markup — that any frontend can
render. The TUI renders it through rich widgets; the GUI maps it onto
Qt block widgets (``chat_blocks``/``chat_view``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from drcode.agent import all_user_prompts, extract_display, is_pending_retry
from drcode.models import Clarification, Task, TaskEvent, TaskStatus

#: Fallback timestamp for entries with none (sorts first — the TUI's
#: ``_MIN_TIME``).
MIN_TIME = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TimelineEntry:
    """One chronological entry of the task's chat thread."""

    at: datetime
    seq: int
    #: "user" / "user-pending" / "reasoning" / "assistant" / "tool-call"
    #: / "tool" / "tester" / "testing-sep" / "resolving-sep" / "summary"
    #: / "event" / "system" — the same role vocabulary the TUI's
    #: ``_write_history_line`` switches on.
    role: str
    text: str


def format_event_text(event: TaskEvent) -> str:
    """The TUI's ``_format_event``: a dim full timestamp + the text
    (mention linkification happens at render time)."""
    ts = event.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return f"{ts} {event.text}"


def build_timeline(
    task: Task,
    *,
    consumed_prompts: dict[str, tuple[str, list]] | None = None,
) -> list[TimelineEntry]:
    """The task's full history as ONE chronological timeline — the port
    of the TUI's ``_build_task_timeline`` (same sources, same sort: by
    timestamp, stable on the insertion order for equal stamps)."""
    timeline: list[TimelineEntry] = []
    seq = 0

    def add(at: datetime | None, role: str, text: str) -> None:
        nonlocal seq
        # Mixed-timestamp tolerance: old transcripts may carry naive
        # stamps (fromisoformat without an offset) — make every entry
        # aware so the sort (and the rendering) can compare them.
        if at is not None and at.tzinfo is None:
            at = at.astimezone()
        timeline.append(TimelineEntry(at or MIN_TIME, seq, role, text))
        seq += 1

    for at, role, text in extract_display(task.messages):
        add(at, role, text)
    # Tester rounds: the synthetic "Verify completed task #…" prompt is
    # not shown; each round is announced by its own separator anchored
    # at the round's first message, tool calls get the "tester: " prefix.
    _add_rounds(task.tester_messages, add, "testing-sep", "tester")
    # Resolver rounds: the same shape.
    _add_rounds(task.resolver_messages, add, "resolving-sep", "resolver")
    # Service events (failures, skips) — interleaved where they occurred.
    for event in task.events:
        add(event.at, "event", format_event_text(event))
    # Queued user input — placed by the moment it was TYPED.
    for entry in task.pending_input:
        if is_pending_retry(task.messages, entry.text):
            # A failed/interrupted run already persisted this prompt into
            # the transcript — the pending copy would duplicate it.
            continue
        add(entry.at, "user-pending", entry.text)
    # A prompt a running agent has consumed but that is not persisted
    # into the transcript yet (the first node boundary saves it).
    in_flight = (consumed_prompts or {}).get(task.id)
    if in_flight is not None and in_flight[0] not in all_user_prompts(task.messages):
        for entry in in_flight[1]:
            add(entry.at, "user-pending", entry.text)
    timeline.sort(key=lambda item: (item.at, item.seq))
    return timeline


def _add_rounds(
    rounds: list[list[dict]],
    add: object,
    sep_role: str,
    prefix: str,
) -> None:
    """One synthetic-round family (tester / resolver) into the timeline."""
    for round_messages in rounds:
        if not round_messages:
            continue
        items = extract_display(round_messages)
        round_at = next((at for at, _, _ in items if at is not None), None)
        add(round_at, sep_role, "")  # type: ignore[operator]
        for at, role, text in items:
            if role == "user":
                continue
            if role == "tool-call":
                text = f"{prefix}: {text}"
            add(at, role, text)  # type: ignore[operator]


def clarification_entries(clarification: Clarification) -> list[str]:
    """The clarification package rendered into line texts (the GUI port
    of the TUI's ``_write_clarification``, non-interactive: the input
    flow is plan task 4). One string per question plus its options and
    the hints; the agent-proposal ★ markers preserved structurally."""
    questions = clarification.questions
    lines: list[str] = []
    packaged = len(questions) > 1
    for qi, item in enumerate(questions):
        header = f"{qi + 1}. {item.question}" if packaged else item.question
        lines.append(f"drcode ❓ {header}")
        proposals = list(item.agent_proposals)
        for option in item.options:
            lines.append(f"  ▸ {option}")
        for proposal in proposals:
            lines.append(f"  ▸ ★ {proposal}")
        if proposals:
            lines.append(
                "★ — option proposed by the agent on its own "
                "initiative, not part of your request."
            )
        if item.multiple:
            lines.append(
                "Multiple choices allowed — click every option you "
                "need, then press Enter; nothing selected means "
                "\u201cnone of them\u201d."
            )
    if len(questions) > 1:
        lines.append(
            "Click an option per question or type answers — one line "
            "per question — then press Enter to send them all at once."
        )
    elif not any(q.multiple for q in questions):
        lines.append("Click an option or answer in the input below.")
    return lines


@dataclass
class RenderedTimeline:
    """The render-ready chat state of one task: the chronological
    timeline plus the trailing clarification package (if the task waits
    for one) and the chat's content flag (the TUI's ``_chat_has_content``
    — a scroll-hint for an empty view)."""

    entries: list[TimelineEntry] = field(default_factory=list)
    clarification: list[str] = field(default_factory=list)

    @classmethod
    def for_task(
        cls,
        task: Task,
        *,
        consumed_prompts: dict[str, tuple[str, list]] | None = None,
    ) -> RenderedTimeline:
        rendered = cls(build_timeline(task, consumed_prompts=consumed_prompts))
        if task.status is TaskStatus.AWAITING_CLARIFICATION and task.clarification:
            rendered.clarification = clarification_entries(task.clarification)
        return rendered
