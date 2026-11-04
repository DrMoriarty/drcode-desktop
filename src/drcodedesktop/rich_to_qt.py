"""Rich markup → Qt (HTML for QTextDocument) adapter — plan task 3.

The chat transcript is rendered by the TUI through rich primitives: the
UI-neutral builders in :mod:`drcode.core.blocks` produce rich ``Text``
objects (markdown render, syntax-highlighted JSON, linkified task-id
mentions) and the Textual widgets paint them as styled terminal cells.
The GUI reuses the SAME builders verbatim and converts the resulting
``Text`` into HTML for a ``QTextBrowser``:

- every rich style attribute maps onto the CSS subset QTextDocument
  understands (bold/italic/underline/strike, fg/bg colors);
- the ``@click`` meta the TUI's linkify rides in becomes an ``<a>``
  anchor (``drcode:task/<id>`` / ``drcode:pick/<q>/<i>``), resolved by
  the chat view's ``anchorClick`` handler;
- markdown bodies (incl. fenced code blocks with pygments colors) and
  JSON tool-call arguments come out of the very same
  ``markdown_text`` / ``json_text`` builders the TUI uses, so code
  blocks render with the same syntax highlighting palette.

The module is pure (no Qt imports): it emits HTML strings, which keeps
it testable offscreen and independent of the widget layer.
"""

from __future__ import annotations

import html
import io
from typing import Any

from rich.color import Color, ColorType
from rich.console import Console
from rich.style import Style
from rich.text import Text

#: Render width for the markdown pass (the TUI renders at the terminal
#: width, ~100 columns). Fixed for the GUI: deterministic, width-stable
#: across window resizes (QTextDocument only wraps further if the pane
#: is narrower).
RENDER_WIDTH = 100

# The dark palette the TUI's themes resolve to (approximation of the
# terminal's xterm-256 base colors — ansi_* names in pygments' ansi_dark
# theme land here via ColorType.STANDARD).
_ANSI16 = (
    "#000000",
    "#cd3131",
    "#0dbc79",
    "#e5e510",
    "#2472c8",
    "#bc3fbc",
    "#11a8cd",
    "#e5e5e5",
    "#666666",
    "#f14c4c",
    "#23d18b",
    "#f5f543",
    "#3b8eea",
    "#d670d6",
    "#29b8db",
    "#ffffff",
)

_BG = "#111318"  # the chat pane's dark background (dim/reverse blending)
_FG = "#c8ccd4"  # the default foreground

_SegmentConsole = Console(
    width=RENDER_WIDTH * 2, legacy_windows=False, file=io.StringIO()
)


def _color_css(color: Color | None) -> str | None:
    """A rich Color → CSS color, any of its three color systems."""
    if color is None:
        return None
    if color.type is ColorType.STANDARD and color.number is not None:
        return _ANSI16[color.number] if color.number < 16 else _FG
    if color.type is ColorType.EIGHT_BIT and color.number is not None:
        return _ansi256(color.number)
    triplet = color.triplet
    if triplet is None:  # a DEFAULT color: leave to the document default
        return None
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def _ansi256(n: int) -> str:
    """The xterm-256 palette entry as hex (16 base + 6x6x6 cube + grays)."""
    if n < 16:
        return _ANSI16[n]
    if n < 232:
        n -= 16
        steps = (0, 95, 135, 175, 215, 255)
        r, g, b = (steps[n // 36], steps[(n // 6) % 6], steps[n % 6])
        return f"#{r:02x}{g:02x}{b:02x}"
    gray = 8 + (n - 232) * 10
    return f"#{gray:02x}{gray:02x}{gray:02x}"


def _blend(fg: str, alpha: float, bg: str) -> str:
    """Blend a hex fg toward a hex bg (dim = fade toward the background)."""
    fr, fg_, fb = int(fg[1:3], 16), int(fg[3:5], 16), int(fg[5:7], 16)
    br, bg_, bb = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    mix = lambda f, b: round(b + (f - b) * alpha)  # noqa: E731 - local mapping
    return f"#{mix(fr, br):02x}{mix(fg_, bg_):02x}{mix(fb, bb):02x}"


def _style_css(style: Style | None, meta: dict[str, Any] | None) -> str:
    """One rich Style → a CSS declaration list (no element wrapping)."""
    if style is None:
        return ""
    color = _color_css(style.color)
    bg = _color_css(style.bgcolor)
    if style.dim and color:
        color = _blend(color, 0.55, bg or _BG)
    if style.reverse:
        color, bg = bg or _FG, color or _BG
    css: list[str] = []
    if color:
        css.append(f"color:{color}")
    if bg:
        css.append(f"background-color:{bg}")
    if style.bold:
        css.append("font-weight:bold")
    if style.italic:
        css.append("font-style:italic")
    deco = []
    if style.underline:
        deco.append("underline")
    if style.strike:
        deco.append("line-through")
    if deco:
        css.append(f"text-decoration:{' '.join(deco)}")
    # The TUI's clickable spans carry their action in the style META (see
    # drcode.core.blocks._mention_click_style): turn it into an anchor the
    # chat view resolves on anchorClick.
    click = (meta or {}).get("@click")
    if click:
        if not deco:
            css.append("text-decoration:underline")
        css.append("color:#29b8db")
    return ";".join(css)


def _anchor_href(meta: dict[str, Any] | None) -> str | None:
    """The rich ``@click`` action → an href the chat view understands."""
    click = (meta or {}).get("@click")
    if not click:
        return None
    action, _, args = str(click).partition("(")
    args = args.rstrip(")")
    if action.endswith("open_task"):
        task_id = args.strip(" '\"")
        return f"drcode:task/{task_id}"
    if action.endswith("pick_option"):
        parts = [p.strip() for p in args.split(",")]
        return "drcode:pick/" + "/".join(parts)
    return "drcode:noop"


def rich_text_to_html(text: Text) -> str:
    """A rich ``Text`` (spans may overlap, styles may carry META) → HTML.

    Rendered through a detached Console into flat segments (non-
    overlapping styles — the same segmentation rich paints the terminal
    with), then each segment becomes an HTML-escaped ``<span>``. Runs
    inside ``white-space:pre-wrap`` containers, so segment newlines are
    literal.
    """
    segments = list(_SegmentConsole.render(text, _SegmentConsole.options))
    out: list[str] = []
    for seg in segments:
        if not seg.text:
            continue
        escaped = html.escape(seg.text, quote=False)
        meta = seg.style.meta if isinstance(seg.style, Style) else None
        css = _style_css(seg.style if isinstance(seg.style, Style) else None, meta)
        href = _anchor_href(meta)
        if href is not None and href != "drcode:noop":
            escaped = f'<a href="{href}">{escaped}</a>'
        out.append(f'<span style="{css}">{escaped}</span>' if css else escaped)
    return "".join(out).replace("\n", "<br>")


def markdown_body_html(text: str, *, streaming: bool = False) -> str:
    """Markdown (assistant answer / reasoning body) → HTML, via the SAME
    rich builder the TUI renders with — fenced code blocks come out with
    the identical pygments palette. While streaming, only the complete
    fragments are formatted (the TUI's streamable_markdown trade-off)."""
    from drcode.core.blocks import markdown_text, streamable_markdown

    rendered = (
        streamable_markdown(text, RENDER_WIDTH)
        if streaming
        else markdown_text(text, RENDER_WIDTH)
    )
    from drcode.core.blocks import linkify

    return rich_text_to_html(linkify(rendered))


def json_body_html(text: str) -> str:
    """Tool-call arguments → syntax-highlighted HTML (the TUI's
    json_text: re-indented, pygments-colored, unparsable → plain)."""
    from drcode.core.blocks import json_text

    return rich_text_to_html(json_text(text))


def mention_text_html(text: str) -> str:
    """Plain text (already verbatim, no markdown) with task-id mentions
    (#69c8bd30) linkified — the escape()+_mention_markup pair of the TUI
    as a single rich pass."""
    from drcode.core.blocks import linkify

    return rich_text_to_html(linkify(Text(text)))


def segments_html(*parts: tuple[str, str]) -> str:
    """A composed line of (text, rich-style) pairs → HTML (the markup
    lines of the TUI — "tester › …", "──── testing ────" — as explicit
    segments instead of rich markup strings; no parser in the loop)."""
    text = Text()
    for content, style in parts:
        text.append(content, style)
    return rich_text_to_html(text)
