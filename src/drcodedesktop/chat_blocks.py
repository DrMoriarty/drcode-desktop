"""Qt chat blocks (plan task 3) — the GUI twin of ``drcode.tui.blocks``.

Every timeline entry renders as one auto-height block widget; the live
streaming blocks implement the core's :class:`drcode.core.host.StreamBlock`
protocol exactly like the Textual ``ReasoningBlock``/``AssistantBlock``
pair do:

- ``append`` accumulates the delta immediately but the repaint is
  THROTTLED to a 100 ms flush timer — the markdown re-render reads the
  whole accumulated content, so doing it per delta (the pipeline can
  deliver ~50/s) would starve the loop that also drives the UI;
- the "thinking" spinner runs on its own 120 ms timer and lives in the
  header only — the body render is cached on (streaming, content), so a
  spinner tick never re-parses the markdown;
- ``finish`` stops the timers and renders the final full markdown.

The block bodies come from the same rich builders the TUI uses (via
:mod:`rich_to_qt`), so markdown, fenced code and tool-call JSON look
the way the terminal shows them.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QTextBrowser

from .rich_to_qt import json_body_html, markdown_body_html

# The chat pane palette (the terminal's dark theme, approximated).
CHAT_BG = "#111318"
CHAT_FG = "#c8ccd4"

# Tool-call header constants — the TUI's ToolCallBlock port.
_MAIN_PARAM_KEYS = ("path", "command", "pattern", "question")
_MAX_ARG_LEN = 60


def stamp_label(at: datetime | None) -> str:
    """The right-side header stamp (drcode.core.blocks.stamp_label)."""
    if at is None:
        return ""
    return at.astimezone().strftime("%H:%M:%S")


def tool_header_parts(call: str) -> tuple[str, str]:
    """Split "name(args)" into (header, args), the TUI's ``_split``
    plus the primary-argument suffix ("edit_file src/…py")."""
    open_idx = call.find("(")
    if open_idx != -1 and call.endswith(")"):
        name, args = call[:open_idx].rstrip(), call[open_idx + 1 : -1]
    else:
        name, args = call, ""
    return f"{name}{_primary_arg_suffix(args)}", args


def _primary_arg_suffix(args: str) -> str:
    try:
        parsed = json.loads(args) if args else None
    except ValueError:
        return ""
    if not isinstance(parsed, dict) or not parsed:
        return ""
    if len(parsed) == 1:
        value = next(iter(parsed.values()))
    else:
        value = next((parsed[k] for k in _MAIN_PARAM_KEYS if k in parsed), None)
    if value is None:
        return ""
    text = " ".join(str(value).split())
    if not text:
        return ""
    if len(text) > _MAX_ARG_LEN:
        text = text[: _MAX_ARG_LEN - 1] + "…"
    return f" {text}"


def _right_stamped(html: str, at: datetime | None) -> str:
    """One header line: content left, the HH:MM:SS stamp dim right."""
    stamp = stamp_label(at)
    if not stamp:
        return html
    return (
        f'<table width="100%" cellspacing="0" cellpadding="0"><tr>'
        f"<td>{html}</td>"
        f'<td align="right" style="color:#6b7280;font-style:italic">{stamp}</td>'
        f"</tr></table>"
    )


class ChatBlockBrowser(QTextBrowser):
    """One chat block: frameless, selectable, auto-height.

    A QTextBrowser gives text selection and clickable anchors for free;
    the widget sizes itself to its document (a QTextBrowser inside a
    layout otherwise scrolls instead of growing)."""

    #: A toggle anchor ("drcode:toggle") was clicked — the view swaps the
    #: block's collapsed state; any other anchor routes to the view.
    anchor_activated = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setOpenLinks(False)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDefaultFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(CHAT_BG))
        palette.setColor(QPalette.ColorRole.Text, QColor(CHAT_FG))
        self.setPalette(palette)
        self.viewport().setAutoFillBackground(False)
        self.anchorClicked.connect(self._on_anchor)
        self._toggle = False

    def _on_anchor(self, url: Any) -> None:
        self.anchor_activated.emit(url.toString())

    # -- auto height ----------------------------------------------------------

    def set_block_html(self, html: str) -> None:
        """(Re)render the block and re-fit its height to the document."""
        self.setHtml(html)
        self._fit_height()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        width = self.viewport().width()
        if width <= 0:
            # Not laid out yet (offscreen tests, pre-show construction).
            return
        self.document().setTextWidth(width)
        height = self.document().size().height() + 2 * self.frameWidth()
        self.setFixedHeight(int(height) + 2)


class StreamBlockBrowser(ChatBlockBrowser):
    """A live-streamed chat block — the ``StreamBlock`` protocol on Qt.

    Reasoning blocks are collapsible with an animated spinner while the
    model thinks; assistant blocks grow the markdown in place (complete
    fragments formatted, the still-producing tail plain — the TUI's
    streamable-markdown behaviour). The pipeline calls the protocol
    methods from the loop thread (qasync: the Qt main thread — no
    cross-thread signals needed, exactly the TUI's threading model).
    """

    def __init__(
        self,
        kind: str,
        task_id: str = "",
        at: datetime | None = None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.task_id = task_id
        self.at = at or datetime.now()
        # Chat-view epoch the block belongs to (the pipeline's orphan
        # check; the GUI never bumps the epoch — the view re-renders in
        # place and keeps the same block objects alive).
        self.stream_epoch = -1
        self._content = ""
        self._streaming = False
        self._thinking = False
        self._collapsed = kind == "reasoning"
        self._flush_timer: QTimer | None = None
        self._spin_timer: QTimer | None = None
        self._spin_frame = 0
        self._run_started: float | None = None
        self._body_html: str | None = None
        self._body_key: tuple[bool, str] | None = None

    # -- StreamBlock protocol ---------------------------------------------------

    def begin(self) -> None:
        self._streaming = True
        self.thinking(True)
        self._redraw()

    def append(self, text: str) -> None:
        """Accumulate immediately, repaint batched (100 ms flush)."""
        self._content += text
        if self._flush_timer is None:
            self._flush_timer = QTimer(self)
            self._flush_timer.setSingleShot(True)
            self._flush_timer.setInterval(100)
            self._flush_timer.timeout.connect(self._flush)
            self._flush_timer.start()

    def thinking(self, on: bool) -> None:
        """Spinner on/off while the stream is still running."""
        if not self._streaming:
            return
        if on and self._spin_timer is None:
            self._spin_timer = QTimer(self)
            self._spin_timer.setInterval(120)
            self._spin_timer.timeout.connect(self._spin)
            self._spin_timer.start()
        elif not on and self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
            self._redraw()

    def finish(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None
        self._streaming = False
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        self._redraw()

    def set_content(self, text: str) -> None:
        """Fill from history (not streaming)."""
        self._content = text
        self._redraw()

    def get_content(self) -> str:
        """The accumulated content so far (the pipeline's carry-over)."""
        return self._content

    # -- tool-call spinner (the TUI's ToolCallBlock API) ------------------------

    def start_spinner(self) -> None:
        """Tool execution in progress: animate the header marker."""
        if self._spin_timer is None:
            self._spin_timer = QTimer(self)
            self._spin_timer.setInterval(120)
            self._spin_timer.timeout.connect(self._spin)
            self._spin_timer.start()
            self._run_started = time.monotonic()

    def stop_spinner(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        self._redraw()

    # -- internals ---------------------------------------------------------------

    def detach(self) -> None:
        """Unmounted (a view switch): stop the animation so an orphaned
        widget doesn't keep ticking (the TUI's on_unmount)."""
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        if self._flush_timer is not None:
            self._flush_timer.stop()
            self._flush_timer = None

    def _spin(self) -> None:
        self._spin_frame = (self._spin_frame + 1) % 10
        self._redraw_header_only()

    def _flush(self) -> None:
        self._flush_timer = None
        self._redraw()

    def toggle(self) -> None:
        """Collapse/expand (reasoning and tool-call blocks)."""
        self._collapsed = not self._collapsed
        self._redraw()

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    def _redraw_header_only(self) -> None:
        """A spinner tick: rebuild without touching the cached body."""
        if self.kind == "assistant":
            return  # no animated header
        self._redraw()

    def _redraw(self) -> None:
        from .rich_to_qt import mention_text_html

        if self.kind == "assistant":
            self.set_block_html(self._assistant_html())
            return
        if self.kind == "tool-call":
            header = mention_text_html(f"⚙ {tool_header_parts(self._content)[0]}")
            body = ""
            if not self._collapsed and self._content:
                args = tool_header_parts(self._content)[1]
                if args:
                    body = json_body_html(args)
            self.set_block_html(
                _right_stamped(self._toggle_anchor(header), self.at)
                + (f'<div style="white-space:pre-wrap">{body}</div>' if body else "")
            )
            return
        # reasoning
        status = (
            f" {self._spin_frame_char()}" if self._spin_timer is not None else ""
        )
        header = mention_text_html(
            f"{'▸' if self._collapsed else '▾'} Reasoning{status}"
        )
        if self._collapsed:
            self.set_block_html(_right_stamped(self._toggle_anchor(header), self.at))
            return
        body = self._reasoning_body_html()
        self.set_block_html(
            _right_stamped(self._toggle_anchor(header), self.at)
            + f'<div style="white-space:pre-wrap">{body}</div>'
        )

    def _spin_frame_char(self) -> str:
        from drcode.core.blocks import SPINNER_FRAMES

        return SPINNER_FRAMES[self._spin_frame % len(SPINNER_FRAMES)]

    def _reasoning_body_html(self) -> str:
        if not self._content:
            return '<span style="color:#6b7280;font-style:italic">(empty)</span>'
        key = (self._streaming, self._content)
        if self._body_key != key or self._body_html is None:
            self._body_html = markdown_body_html(
                self._content, streaming=self._streaming
            )
            self._body_key = key
        return self._body_html

    def _assistant_html(self) -> str:
        if not self._content:
            return '<span style="color:#6b7280;font-style:italic">(empty)</span>'
        key = (self._streaming, self._content)
        if self._body_key != key or self._body_html is None:
            self._body_html = markdown_body_html(
                self._content, streaming=self._streaming
            )
            self._body_key = key
        return f'<div style="white-space:pre-wrap">{self._body_html}</div>'

    def _toggle_anchor(self, header: str) -> str:
        return f'<a href="drcode:toggle">{header}</a>'
