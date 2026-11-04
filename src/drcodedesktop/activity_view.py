"""The run-activity indicator (plan task 7): the GUI port of
``drcode.tui.activity_view`` (the sidebar-bottom indicator half — the
stall watchdog lives in :mod:`drcodedesktop.core_shell`).

Sits at the bottom of the queue pane and, once a second, reports
whether the running agent/tester run(s) make progress or have gone
silent (hung model request, long tool, deadlock):

- ``▶ agent: working`` — fresh (< 30 s since the last stream delta /
  tool event), with the current tool and its age;
- ``⚠ agent: stalled`` — stale, with the auto-nudge countdown (the
  watchdog in the shell actually fires it);
- network-retry / outage states of a run waiting for its delayed retry;
- ``⚠ agent: idle`` — a task claims to be active but no worker owns it
  (a stop or a stranded run) — surfaced instead of a fake idle;
- ``○ idle`` — nothing runs.

Several live runs (parallel development slots) collapse into a compact
summary line — the count plus the age of the most silent run. Below the
status line the widget carries the session statistics (tasks completed,
avg/median activation→completion time, bugs:features ratio and the
exact cumulative token usage of the session — the TUI header's expanded
lines) and the git unmerged-work counter (``⎇ N`` — completed tasks
still unmerged on their ``drcode/<id>`` branches; the git calls run in
a thread, never on the loop).

All data comes from the shell's core state (``drcode.core`` / the
session counters) — no ``tui/`` imports.
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import time
from typing import TYPE_CHECKING

from drcode.core.usage import NETWORK_RETRIES
from drcode.models import TaskKind
from drcode.version import APP_VERSION
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from .core_shell import CoreShell

#: A run silent for less than this is "fresh" (the TUI's constant).
_FRESH_SECONDS = 30
#: The git indicator's poll period (the TUI's 10 s poll).
_GIT_POLL_SECONDS = 10.0

_FRESH_COLOR = "#98c379"
_STALE_COLOR = "#e06c75"
_IDLE_COLOR = "#6b7280"


def fmt_age(seconds: int) -> str:
    """Compact human age ("45s", "6m12s", "2h05m") — the TUI's
    ``_fmt_age``: seconds drop out of the hour range, nobody reads them
    there."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def fmt_tokens(n: int) -> str:
    """Compact human token count ("980", "45k", "1.2M") — the TUI
    header's ``_fmt_tokens``."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        k = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        if float(k) < 1000:
            return f"{k}k"
    m = n / 1_000_000
    return f"{m:.1f}".rstrip("0").rstrip(".") + "M"


def fmt_duration(seconds: float) -> str:
    """Compact human duration ("45s", "6m12s") — the TUI header's
    ``_fmt_duration``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


class ActivityIndicator(QWidget):
    """The sidebar-bottom run-activity indicator of the queue pane."""

    def __init__(self, shell: CoreShell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        self._git_ticks = 0
        # Strong references to the in-flight git-indicator reads.
        self._git_jobs: set[asyncio.Task[None]] = set()
        # The last rendered state, for tests and styling.
        self.state: str | None = None
        self.status_text = ""
        self.detail_text = ""

        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setStyleSheet("QFrame { background: #16181f; border: none; }")
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(6, 4, 6, 4)
        inner.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(4)
        self._status_label = QLabel()
        self._git_label = QLabel()
        self._version_label = QLabel(f"DrCode {APP_VERSION}")
        self._version_label.setStyleSheet(f"color: {_IDLE_COLOR};")
        top.addWidget(self._status_label)
        top.addStretch(1)
        top.addWidget(self._git_label)
        top.addWidget(self._version_label)
        inner.addLayout(top)

        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(f"color: {_IDLE_COLOR};")
        inner.addWidget(self._detail_label)

        self._session_label = QLabel()
        self._session_label.setStyleSheet(f"color: {_IDLE_COLOR};")
        inner.addWidget(self._session_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(frame)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1000)
        self._poll_timer.timeout.connect(self.poll_once)
        self._poll_timer.start()
        self.poll_once()

    # -- test surface ------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the timers (project closed)."""
        self._poll_timer.stop()

    # -- the 1 s poll ------------------------------------------------------------

    def poll_once(self) -> None:
        """One activity tick (also called directly by tests): fold the
        task-tree changes into the session counters, then render."""
        shell = self._shell
        shell._collect_session_stats()
        self._render_session_line()
        self._git_ticks += 1
        if self._git_ticks * 1.0 >= _GIT_POLL_SECONDS:
            self._git_ticks = 0
            self._poll_git_indicator()
        self._poll_activity()

    # -- rendering ---------------------------------------------------------------

    def _render_activity(self, status: str, detail: str, state: str | None) -> None:
        """One activity line: the status on the left (colored by
        state), the live detail below. `state` ("fresh"/"stale"/None)
        picks the status color — kept as attributes for tests."""
        self.status_text = status
        self.detail_text = detail
        self.state = state
        color = {"fresh": _FRESH_COLOR, "stale": _STALE_COLOR}.get(
            state or "", _IDLE_COLOR
        )
        self._status_label.setText(f'<span style="color:{color}">{_esc(status)}</span>')
        self._detail_label.setText(_esc(detail))

    def _render_single_run_activity(self, task_id: str, who: str) -> None:
        """Detailed single-run rendering: current tool, age, stall
        countdown (the TUI's ``_render_single_run_activity``)."""
        shell = self._shell
        stamped = shell._last_activity.get(task_id)
        if stamped is None:
            stamped = time.monotonic()
        age = int(time.monotonic() - stamped)
        tool = shell._current_tool.get(task_id)
        if tool:
            detail = f"tool {tool} running {fmt_age(age)}"
        else:
            detail = f"last event {fmt_age(age)} ago"
        if age < _FRESH_SECONDS:
            self._render_activity(f"▶ {who}: working", detail, "fresh")
        else:
            limit = shell.settings.stall_nudge.minutes
            if limit is not None:
                left = max(1, limit * 60 - age)
                detail += f" · auto-nudge in {fmt_age(int(left))}"
            else:
                detail += " (Ctrl+R nudges the run)"
            self._render_activity(f"⚠ {who}: stalled", detail, "stale")

    def _poll_activity(self) -> None:
        """The 1 s status decision — the port of
        ``tui.activity_view._poll_activity`` (see the module docstring
        for the state table)."""
        shell = self._shell
        agent_ids = list(shell._agent_runs)
        tester_id = shell._tester_task_id if shell.tester_running else None
        if not (agent_ids or tester_id):
            active_tasks = shell.manager.active_tasks()
            now = time.monotonic()
            for active in active_tasks:
                # Network-retried run waiting for its delayed retry: the
                # task is fine, the re-run timer just has not fired yet.
                retry_at = shell._agent_net_retry_at.get(active.id)
                if retry_at is not None and retry_at > now:
                    left = max(1, int(retry_at - now))
                    attempt = shell._agent_net_count.get(active.id, 0)
                    if attempt > NETWORK_RETRIES:
                        self._render_activity(
                            "⏳ agent: network outage",
                            f"#{active.id} offline — waiting {left}s",
                            None,
                        )
                    else:
                        self._render_activity(
                            "⏳ agent: network retry",
                            f"#{active.id} attempt {attempt}/{NETWORK_RETRIES}"
                            f" in {left}s",
                            None,
                        )
                    return
            if active_tasks:
                # A task claims to be active but no worker owns it (the
                # user aborted the run, or an older build stranded it).
                first = active_tasks[0]
                more = (
                    f" (+{len(active_tasks) - 1} more)" if len(active_tasks) > 1 else ""
                )
                self._render_activity(
                    "⚠ agent: idle",
                    f"#{first.id}{more} active, nothing running — "
                    "send a message or Ctrl+R to (re)start",
                    "stale",
                )
                return
            self._render_activity("○ idle", "", None)
            return
        if len(agent_ids) + (1 if tester_id else 0) == 1:
            # Single live run: the detailed rendering.
            if agent_ids:
                task_id, who = agent_ids[0], "agent"
            else:
                task_id, who = tester_id or "", "tester"
            self._render_single_run_activity(task_id, who)
            return
        # Several runs at once (parallel slots): a compact summary — the
        # count plus the age of the most silent run, so a hung one still
        # surfaces without a per-run detail wall.
        n_agents = len(agent_ids)
        parts = [f"{n_agents} agent" + ("s" if n_agents != 1 else "")]
        if tester_id is not None:
            parts.append("1 tester")
        all_running = agent_ids + ([tester_id] if tester_id else [])
        oldest = min(
            (shell._last_activity.get(t, time.monotonic()) for t in all_running),
            default=time.monotonic(),
        )
        age = int(time.monotonic() - oldest)
        detail = " + ".join(parts) + " working"
        if age < _FRESH_SECONDS:
            self._render_activity(
                f"▶ {len(all_running)} runs: working",
                f"{detail} · oldest event {fmt_age(age)} ago",
                "fresh",
            )
        else:
            limit = shell.settings.stall_nudge.minutes
            if limit is not None:
                left = max(1, limit * 60 - age)
                detail += f" · auto-nudge in {fmt_age(int(left))}"
            else:
                detail += " (Ctrl+R nudges the viewed task)"
            self._render_activity(
                f"⚠ {len(all_running)} runs: stalled",
                f"{detail} · oldest event {fmt_age(age)} ago",
                "stale",
            )

    # -- session statistics line ---------------------------------------------------

    def _render_session_line(self) -> None:
        """The session-statistics line under the activity detail (the
        TUI expanded header's session line, reduced to the task counters
        plus the exact token usage): tasks done, avg/median
        activation→completion time, bugs:features ratio of the session's
        creations, and the four per-response token counters."""
        shell = self._shell
        durations = shell._session_durations
        if durations:
            avg = statistics.fmean(durations)
            median = statistics.median(durations)
            times = f"avg {fmt_duration(avg)} | median {fmt_duration(median)}"
        else:
            times = "avg — | median —"
        bugs = shell._session_created[TaskKind.BUG]
        features = shell._session_created[TaskKind.FEATURE]
        usage = shell._session_usage
        tokens: list[str] = []
        if usage.input_tokens:
            tokens.append(f"tok in {fmt_tokens(usage.input_tokens)}")
        if usage.cache_read_tokens or usage.cache_write_tokens:
            tokens.append(
                f"cache {fmt_tokens(usage.cache_read_tokens)}"
                f"/{fmt_tokens(usage.cache_write_tokens)}"
            )
        if usage.output_tokens:
            tokens.append(f"out {fmt_tokens(usage.output_tokens)}")
        line = (
            f"tasks done: {shell._session_completed} | {times} | "
            f"bugs:features {bugs}:{features}"
        )
        if tokens:
            line += f" | {' | '.join(tokens)}"
        self._session_label.setText(_esc(line))

    # -- git unmerged-work indicator -------------------------------------------------

    def _poll_git_indicator(self) -> None:
        """Refresh the ``⎇ N`` counter: completed tasks still unmerged
        on their ``drcode/<id>`` branches. The git calls are blocking —
        they run in a thread via the qasync loop, the label updates on
        the next loop turn; a slow repo never stalls the UI."""
        shell = self._shell

        async def job() -> None:
            ids = await asyncio.to_thread(shell._unmerged_completed_ids)
            count = len(ids)
            self._git_label.setText(f"⎇ {count}" if count else "")
            if count:
                plural = "" if count == 1 else "s"
                self._git_label.setToolTip(
                    f"{count} completed task{plural} not merged into "
                    "the base branch yet (git merge drcode/<id>)"
                )
            else:
                self._git_label.setToolTip("")

        with contextlib.suppress(RuntimeError):
            job_task = asyncio.ensure_future(job())
            self._git_jobs.add(job_task)
            job_task.add_done_callback(self._git_jobs.discard)


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
