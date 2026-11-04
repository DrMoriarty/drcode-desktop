"""Plan task 7 acceptance tests: the parity widgets of the GUI.

Every widget is driven against the data sources it uses in production —
the shell's core state (``drcode.core``) and the two stores — never
against a ``tui`` import:

- the todo checklist panel renders the viewed task's ``task.todos``
  (the collapse rules of the TUI: auto-collapse when everything is
  done, the manual override reset on a task switch, hidden when empty);
- service events land in the chat pane (the interleaved ``event ›``
  lines — the live hub path rebuilds the pane);
- the activity indicator renders idle / fresh / stalled / summary
  states from the shell's run bookkeeping, and the stall watchdog
  aborts a run silent past the configured limit (the real
  ``_nudge_run`` path);
- the usage dialog renders the persistent ``usage.json`` days (the
  TUI's row order and the Σ-period total);
- the statistics dialog renders the ``stats.json`` day buckets;
- the notifications dialog collects the enable/topic pair and the Save
  seam persists it into the project settings (the file the TUI writes).

The queue stays parked in every shell scenario (``park_queue``): these
tests drive widgets and bookkeeping, not runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from drcode.models import TaskStatus, TodoItem
from drcode.settings import ProjectSettingsStore, StallNudge
from drcode.stats import StatsStore
from drcode.usage_store import UsageStore
from PySide6.QtWidgets import QApplication

from drcodedesktop.activity_view import ActivityIndicator, fmt_age, fmt_tokens
from drcodedesktop.chat_view import ChatTimelineView
from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.notifications_dialog import NotificationsDialog, NotificationsSetup
from drcodedesktop.stats_dialog import StatsDialog
from drcodedesktop.todo_panel import TodoPanel
from drcodedesktop.usage_dialog import UsageDialog
from tests.test_task_management import park_queue
from tests.test_user_input import run_shell_scenario

Scenario = Callable[[CoreShell, EventHub], Any]

# The QApplication must outlive every widget the tests create (the
# test_user_input harness convention).
_APP: QApplication | None = None


def run_widget_scenario(
    tmp_path: Path, monkeypatch: Any, scenario: Scenario
) -> Any:
    """run_shell_scenario with the queue parked: the widget tests drive
    widgets and bookkeeping, never runs."""
    return run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def cell(table: Any, row: int, column: int) -> Any:
    """A table cell asserted non-None (the renders above guarantee it)."""
    item = table.item(row, column)
    assert item is not None
    return item


def pane_texts(view: ChatTimelineView) -> list[str]:
    """The plain text of every mounted chat block (the assertions scan
    the rendered history)."""
    texts = []
    layout = view._layout
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        to_plain = getattr(widget, "toPlainText", None)
        if callable(to_plain):
            texts.append(to_plain())
    return texts


# -- todo panel --------------------------------------------------------------------


def test_todo_panel_collapse_rules() -> None:
    """The checklist strip: expanded while work remains, auto-collapsed
    once every item is done, the manual override survives a re-render
    but resets on a task switch, and an empty checklist hides it."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    panel = TodoPanel()
    todos = [TodoItem(text="first", done=False), TodoItem(text="second", done=False)]

    panel.update_todos("t1", todos)
    assert not panel.isHidden()
    assert panel.user_collapsed is None
    assert not panel._body.isHidden()  # the full item list is rendered

    # A manual collapse override: survives the next re-render of the
    # SAME task, resets when ANOTHER task is viewed.
    panel.toggle_collapsed()
    assert panel.user_collapsed is True
    assert panel._body.isHidden()
    panel.update_todos("t1", todos)
    assert panel.user_collapsed is True
    panel.update_todos("t2", todos)
    assert panel.user_collapsed is None

    # Everything done: the panel auto-collapses to the counter line.
    done = [TodoItem(text="first", done=True), TodoItem(text="second", done=True)]
    panel.update_todos("t2", done)
    assert panel.user_collapsed is None
    assert panel._body.isHidden()

    # Empty checklist: hidden entirely.
    panel.update_todos("t2", [])
    assert panel.isHidden()


def test_todo_panel_mounted_in_chat_pane(tmp_path: Path, monkeypatch: Any) -> None:
    """The chat pane drives the panel from the viewed task's todos: the
    rebuild on task_updated refreshes the checklist."""
    todos = [TodoItem(text="port the widget", done=False)]

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.create_task("do things\n\nwith todos")
        assert task is not None
        task.todos = todos
        shell.manager.storage.save(task)
        view = ChatTimelineView(shell, hub)
        try:
            view.view_task(task.id)
            assert not view._todo_panel.isHidden()
            assert view._todo_panel.task_id == task.id
            header = view._todo_panel._header.text()
            assert "todos" in header and "0/1" in header
            # The agent checks an item off → the rebuild shows 1/1 and
            # the panel auto-collapses (its default for a finished list).
            task.todos[0].done = True
            shell.manager.storage.save(task)
            hub.emit_task_updated(task.id, task.status.value)
            view._rebuild_timer.start()
            await asyncio.sleep(0.4)  # the rebuild debounce
            assert "1/1" in view._todo_panel._header.text()
        finally:
            view.shutdown()

    run_widget_scenario(tmp_path, monkeypatch, scenario)


# -- service events in the chat pane --------------------------------------------------


def test_service_events_render_in_chat(tmp_path: Path, monkeypatch: Any) -> None:
    """A service event (``shell._show_event`` — the production seam every
    pipeline refusal/abort writes through) shows up in the chat pane as
    an ``event ›`` line with its timestamp, both after a rebuild and
    through the live hub path."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.create_task("eventful task")
        assert task is not None
        view = ChatTimelineView(shell, hub)
        try:
            view.view_task(task.id)
            shell._show_event(task, "run aborted by the watchdog", kind="system")
            # The live path rebuilds the pane (debounced).
            await asyncio.sleep(0.4)
            assert any(
                "event ›" in text and "run aborted by the watchdog" in text
                for text in pane_texts(view)
            )
            # A second event is interleaved into the persisted timeline.
            shell._show_event(task, "permission denied: write /etc", kind="system")
            view._rebuild()
            texts = pane_texts(view)
            assert any("permission denied" in t for t in texts)
        finally:
            view.shutdown()

    run_widget_scenario(tmp_path, monkeypatch, scenario)


# -- activity indicator ----------------------------------------------------------------


def test_fmt_age_and_tokens() -> None:
    """The compact formatters (the TUI's ``_fmt_age``/``_fmt_tokens``)."""
    assert fmt_age(45) == "45s"
    assert fmt_age(125) == "2m05s"
    assert fmt_age(7320) == "2h02m"
    assert fmt_tokens(980) == "980"
    assert fmt_tokens(45_000) == "45k"
    assert fmt_tokens(1_200_000) == "1.2M"


def test_activity_indicator_states(tmp_path: Path, monkeypatch: Any) -> None:
    """The 1 s poll's decision table from the shell's run bookkeeping:
    idle, fresh single run with a tool, stalled single run with the
    auto-nudge countdown, active-but-nothing-running, multi-run
    summary."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        indicator = ActivityIndicator(shell)
        try:
            # Nothing runs, nothing active: idle.
            indicator.poll_once()
            assert indicator.status_text == "○ idle"
            assert indicator.state is None

            # A task is ACTIVE but no worker owns it: the stale warning.
            task = shell.create_task("stranded")
            assert task is not None
            task.status = TaskStatus.ACTIVE
            shell.manager.fire_status_change(task, TaskStatus.QUEUED, "test")
            shell.manager.storage.save(task)
            indicator.poll_once()
            assert indicator.status_text == "⚠ agent: idle"
            assert indicator.state == "stale"
            assert "nothing running" in indicator.detail_text

            # A live agent run, fresh, mid-tool: the detailed line.
            task.status = TaskStatus.QUEUED
            shell.manager.fire_status_change(task, TaskStatus.ACTIVE, "test")
            shell._agent_runs[task.id] = 1
            shell._last_activity[task.id] = time.monotonic()
            shell._current_tool[task.id] = "run_bash"
            indicator.poll_once()
            assert indicator.status_text == "▶ agent: working"
            assert indicator.state == "fresh"
            assert "tool run_bash" in indicator.detail_text

            # Silent past the fresh threshold: stalled + countdown (the
            # shell's watchdog actually fires the nudge).
            shell._last_activity[task.id] = time.monotonic() - 120
            indicator.poll_once()
            assert indicator.status_text == "⚠ agent: stalled"
            assert indicator.state == "stale"
            assert "auto-nudge in" in indicator.detail_text
            assert "28m" in indicator.detail_text  # 30-min default, 2m gone

            # A second parallel run: the compact summary line (both runs
            # fresh — the first one's stamp is renewed).
            shell._last_activity[task.id] = time.monotonic()
            other = shell.create_task("second slot")
            assert other is not None
            other.status = TaskStatus.ACTIVE
            shell.manager.fire_status_change(other, TaskStatus.QUEUED, "test")
            shell._agent_runs[other.id] = 1
            shell._last_activity[other.id] = time.monotonic()
            indicator.poll_once()
            assert indicator.status_text == "▶ 2 runs: working"
            assert "2 agents working" in indicator.detail_text
        finally:
            indicator.shutdown()

    run_widget_scenario(tmp_path, monkeypatch, scenario)


def test_activity_indicator_session_line(tmp_path: Path, monkeypatch: Any) -> None:
    """The session-statistics line counts the tasks completed this
    session (the TUI expanded header's line)."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        indicator = ActivityIndicator(shell)
        try:
            task = shell.create_task("counted")
            assert task is not None
            task.status = TaskStatus.COMPLETED
            shell.manager.fire_status_change(task, TaskStatus.QUEUED, "test")
            shell.manager.storage.save(task)
            indicator.poll_once()
            text = indicator._session_label.text()
            assert "tasks done: 1" in text
            assert "bugs:features 0:1" in text
        finally:
            indicator.shutdown()

    run_widget_scenario(tmp_path, monkeypatch, scenario)


def test_stall_watchdog_nudges_silent_run(tmp_path: Path, monkeypatch: Any) -> None:
    """The shell's 1 s watchdog aborts a run silent past the stall
    limit through the REAL ``_nudge_run``: the worker slot is freed, a
    service event lands in the task's chat and the restart is
    scheduled; a run inside its budget is untouched."""
    monkeypatch.setattr(CoreShell, "run_agent", lambda self, task_id: None)

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.create_task("hung run")
        assert task is not None
        task.status = TaskStatus.ACTIVE
        shell.manager.fire_status_change(task, TaskStatus.QUEUED, "test")
        shell.manager.storage.save(task)
        shell._agent_runs[task.id] = 1
        shell._touch_activity(task.id, "read_file")
        shell.settings.stall_nudge = StallNudge.MIN_15

        # Inside the budget: nothing happens.
        shell._poll_stall()
        assert task.id in shell._agent_runs
        assert not task.events

        # Silent for 16 minutes: aborted + event + restart scheduled.
        shell._last_activity[task.id] = time.monotonic() - 16 * 60
        shell._poll_stall()
        assert task.id not in shell._agent_runs
        assert any("stalled for 16m" in e.text for e in task.events)
        assert any("stall limit: 15 min" in e.text for e in task.events)

    run_widget_scenario(tmp_path, monkeypatch, scenario)


def test_unmerged_tracking_inactive_isolation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Without worktree isolation there are no branches: the git
    indicator's source reports nothing and the per-task merge state
    stays empty."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        task = shell.create_task("no branches here")
        assert task is not None
        task.status = TaskStatus.COMPLETED
        shell.manager.fire_status_change(task, TaskStatus.QUEUED, "test")
        assert shell._unmerged_completed_ids() == []
        assert shell._merge_state == {}

    run_widget_scenario(tmp_path, monkeypatch, scenario)


# -- usage dialog ----------------------------------------------------------------------


def test_usage_dialog_renders_usage_store(tmp_path: Path) -> None:
    """The dialog table mirrors the persistent store: one row per
    (day, model), newest day first, the counters thousands-separated
    and the Σ-period total at the bottom."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    store_path = tmp_path / "usage.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "days": {
                    "2026-01-01": {
                        "coding/openai/gpt-5": {
                            "requests": 3,
                            "input": 1200,
                            "output": 45,
                            "cache_read": 90000,
                            "cache_write": 0,
                        },
                        "tester/openai/gpt-5": {
                            "requests": 1,
                            "input": 300,
                            "output": 15,
                            "cache_read": 0,
                            "cache_write": 0,
                        },
                    },
                    "2026-01-02": {
                        "coding/openai/gpt-5": {
                            "requests": 2,
                            "input": 800,
                            "output": 40,
                            "cache_read": 1000,
                            "cache_write": 500,
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    dialog = UsageDialog(store=UsageStore(path=store_path))
    dialog._apply(dialog._store.load())
    table = dialog._table
    # 3 (day, model) rows + the period total.
    assert table.rowCount() == 4
    # Newest day first; within a day the model keys are sorted.
    assert cell(table, 0, 0).text() == "2026-01-02"
    assert cell(table, 1, 0).text() == "2026-01-01"
    assert cell(table, 1, 1).text() == "coding/openai/gpt-5"
    assert cell(table, 2, 1).text() == "tester/openai/gpt-5"
    assert cell(table, 3, 0).text() == "Σ period"
    # Totals: requests 3+1+2 = 6, input 1200+300+800 = 2 300.
    assert cell(table, 3, 2).text() == "6"
    assert cell(table, 3, 3).text() == "2,300"
    # The status line summarises the period.
    status = dialog._status.text()
    assert "2 day(s)" in status and "2026-01-01 … 2026-01-02" in status
    dialog.deleteLater()


def test_usage_dialog_empty_store_hint(tmp_path: Path) -> None:
    """An empty archive: no rows, the dedicated hint."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    dialog = UsageDialog(store=UsageStore(path=tmp_path / "absent.json"))
    dialog._apply(dialog._store.load())
    assert dialog._table.rowCount() == 0
    assert dialog._status.text() == "No usage recorded yet."
    dialog.deleteLater()


# -- statistics dialog -----------------------------------------------------------------


def test_stats_dialog_renders_day_buckets(tmp_path: Path) -> None:
    """The dialog renders the StatsStore snapshot: 14 zero-filled days
    (newest on top), the on-the-fly averages and both sparkline
    series."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    store = StatsStore(tmp_path)
    store.record_created()
    store.record_completion(cancelled=False, failed=False)
    store.add_time("dev", 3600.0, rounds=1)
    store.add_time("testing", 600.0, rounds=2)

    dialog = StatsDialog(tmp_path)
    dialog._days = store.snapshot(14)
    dialog._render_days()
    table = dialog._table
    assert table.rowCount() == 14
    # Newest day on top: today carries the funnel numbers and the
    # on-the-fly averages (3600s / 1 dev stay, 600s / 2 testing stays).
    assert cell(table, 0, 1).text() == "1"  # created
    assert cell(table, 0, 2).text() == "1"  # completed
    assert cell(table, 0, 5).text() == "1 ч"  # 3600s dev
    assert cell(table, 0, 6).text() == "5 мин"  # 600s / 2
    # The sparkline series cover the same window, oldest → newest.
    assert len(dialog._created_spark.data) == 14
    assert dialog._created_spark.data[-1] == 1.0
    assert dialog._completed_spark.data[-1] == 1.0
    dialog.deleteLater()


def test_stats_dialog_period_toggle(tmp_path: Path) -> None:
    """The "p" toggle flips the window between 14 and 30 days."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    dialog = StatsDialog(tmp_path)
    assert dialog._period == 14
    dialog.action_toggle_period()
    assert dialog._period == 30
    dialog.action_toggle_period()
    assert dialog._period == 14
    dialog.deleteLater()


# -- notifications dialog -------------------------------------------------------------


def test_notifications_dialog_collection() -> None:
    """The form collects the pair; the status line live-renders whether
    the combination would deliver anything (no network probe)."""
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    dialog = NotificationsDialog(enabled=False, topic="")
    assert "off" in dialog._status.text()
    dialog._enabled.setChecked(True)
    assert "no topic" in dialog._status.text()
    dialog._topic.setText("my-topic")
    assert "ntfy.sh/my-topic" in dialog._status.text()
    assert dialog.setup == NotificationsSetup(enabled=True, topic="my-topic")
    dialog.deleteLater()


def test_notifications_save_persists_project_settings(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The Save seam writes the pair into the project settings file —
    the very file the TUI writes — and emits the confirmation notify;
    the notifier (a live settings-getter) picks the values up without a
    rebuild."""

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        notes: list[tuple[str, str, str]] = []
        hub.notify.connect(lambda m, t, s: notes.append((m, t, s)))
        shell.apply_notifications_settings(True, "gui-topic")
        assert shell.settings.notifications_enabled is True
        assert shell.settings.ntfy_topic == "gui-topic"
        on_disk = json.loads(
            (tmp_path / "proj" / ".drcode" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        assert on_disk["notifications_enabled"] is True
        assert on_disk["ntfy_topic"] == "gui-topic"
        assert any("ntfy.sh/gui-topic" in m for m, _t, _s in notes)

    run_widget_scenario(tmp_path, monkeypatch, scenario)


# -- formatting helpers of the session line -------------------------------------------


def test_project_settings_store_still_round_trips(tmp_path: Path) -> None:
    """Sanity: the notifications save rides the same store the project
    settings dialog uses (one file, no competing writers)."""
    store = ProjectSettingsStore(tmp_path)
    settings = store.load()
    settings.notifications_enabled = True
    settings.ntfy_topic = "t"
    store.save(settings)
    reloaded = ProjectSettingsStore(tmp_path).load()
    assert reloaded.notifications_enabled is True
    assert reloaded.ntfy_topic == "t"
