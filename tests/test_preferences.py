"""The shell's own Preferences (gui.json): the shared chat/queue font
pair and the system-tray notification checkboxes.

Covers:

- GuiConfig: the new fields round-trip through gui.json (defaults on a
  fresh/legacy file, malformed values ignored);
- the font: a ChatBlockBrowser picks the configured font up (module
  default), the chat pane's apply_font restyles already-mounted blocks,
  and the Queue tree adopts the same pair;
- the Preferences dialog: OK/Apply write the form into the config and
  persist it, Cancel writes nothing;
- the tray channel: system-origin transitions are reported per the
  checkboxes (user-origin ones never are), the settled empty/blocked
  state is primed at startup and deduplicated to changes only (busy
  resets it), and a notification is suppressed while the main window is
  active.

All tray tests drive TrayNotifier directly (a hidden offscreen widget as
the "main window"); the full shell wiring is exercised once through a
scenario shell built with a tray.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from drcode.models import Task, TaskStatus, VerificationStatus
from drcode.settings import ProjectSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget
from qasync import QEventLoop

from drcodedesktop.chat_blocks import (
    ChatBlockBrowser,
    default_chat_font,
    set_chat_font,
)
from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.gui_config import GuiConfig
from drcodedesktop.preferences_dialog import PreferencesDialog, chat_font
from drcodedesktop.qt_host import QtHost
from drcodedesktop.tray_notifications import TrayEventBridge, TrayNotifier
from tests.test_task_management import park_queue
from tests.test_user_input import run_shell_scenario

_APP: QApplication | None = None


def _app() -> QApplication:
    global _APP
    _APP = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    return _APP  # type: ignore[return-value]


def _fixed_font(family: str = "Courier", size: int = 13) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setFamily(family)
    font.setPointSize(size)
    return font


@pytest.fixture(autouse=True)
def _sandboxed_gui_config(tmp_path: Path, monkeypatch: Any) -> None:
    """Every test gets its own gui.json directory: the tests that write
    the config (dialog OK/Apply, round-trips) must not leak their flags
    into the tray tests, which start from the all-off defaults."""
    monkeypatch.setattr("drcodedesktop.gui_config._gui_config_dir", lambda: tmp_path)


# -- GuiConfig round-trip ----------------------------------------------------------


def test_gui_config_defaults_without_file() -> None:
    """A fresh config: system fixed font ("" / 0) and every notification
    checkbox off — the pre-Preferences behaviour."""
    config = GuiConfig()
    assert config.font_family == ""
    assert config.font_size == 0
    assert not config.notify_completed
    assert not config.notify_tester_failed
    assert not config.notify_clarification
    assert not config.notify_pipeline


def test_gui_config_roundtrip(tmp_path: Path, monkeypatch: Any) -> None:
    """font_family/font_size/notify_* survive save + reload; a malformed
    entry falls back to the default instead of crashing."""
    del monkeypatch  # the autouse fixture already sandboxed the dir
    config = GuiConfig()
    config.font_family = "JetBrains Mono"
    config.font_size = 14
    config.notify_completed = True
    config.notify_pipeline = True
    config.save()

    reloaded = GuiConfig()
    assert reloaded.font_family == "JetBrains Mono"
    assert reloaded.font_size == 14
    assert reloaded.notify_completed
    assert not reloaded.notify_tester_failed
    assert not reloaded.notify_clarification
    assert reloaded.notify_pipeline

    # Malformed values (a bool size, a string flag): ignored, defaults win.
    data = json.loads((tmp_path / "gui.json").read_text(encoding="utf-8"))
    data["font_size"] = True
    data["notify_pipeline"] = "yes"
    (tmp_path / "gui.json").write_text(json.dumps(data), encoding="utf-8")
    broken = GuiConfig()
    assert broken.font_size == 0
    assert not broken.notify_pipeline


# -- font application ----------------------------------------------------------------


def test_chat_block_picks_up_configured_font() -> None:
    """New blocks render with the module font (set via
    set_chat_font / Preferences); None resets to the system fixed font."""
    _app()
    try:
        system = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        set_chat_font(None)
        assert (
            ChatBlockBrowser().document().defaultFont().family()
            == system.family()
        )
        font = _fixed_font(size=15)
        set_chat_font(font)
        block = ChatBlockBrowser()
        assert block.document().defaultFont().family() == font.family()
        assert block.document().defaultFont().pointSize() == 15
        assert default_chat_font().pointSize() == 15
    finally:
        set_chat_font(None)


def test_apply_font_restyles_open_views(tmp_path: Path, monkeypatch: Any) -> None:
    """The Preferences Apply on an open project: the chat pane's
    mounted blocks AND the queue tree switch to the new font pair at
    once (no project reopen)."""
    font = _fixed_font(size=16)

    async def scenario(shell: CoreShell, hub: EventHub) -> None:
        del hub
        from drcodedesktop.chat_view import ChatTimelineView
        from drcodedesktop.task_queue_view import TaskQueueView

        chat = ChatTimelineView(shell, shell._hub)
        queue = TaskQueueView(shell, shell._hub)
        try:
            # A mounted history-style block exists (the empty-pane hint).
            hint = None
            layout = chat._layout
            for i in range(layout.count()):
                widget = layout.itemAt(i).widget()
                if isinstance(widget, ChatBlockBrowser):
                    hint = widget
            assert hint is not None

            chat.apply_font(font)
            queue.apply_font(font)
            assert hint.document().defaultFont().pointSize() == 16
            assert queue._tree.font().pointSize() == 16

            # Blocks created afterwards use the same font.
            fresh = ChatBlockBrowser()
            assert fresh.document().defaultFont().pointSize() == 16
        finally:
            queue.shutdown()
            chat.shutdown()
        await _drain()

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)
    set_chat_font(None)


async def _drain() -> None:
    import asyncio

    await asyncio.sleep(0.05)
    await asyncio.sleep(0)


def test_chat_font_helper_falls_back_to_system() -> None:
    """chat_font(config): "" / 0 keep the system fixed font; a family
    and a size override it (the ONE pair shared by both views)."""
    _app()
    config = GuiConfig()
    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    fallback = chat_font(config)
    assert fallback.family() == system.family()
    config.font_family = "Monaco"
    config.font_size = 11
    tuned = chat_font(config)
    assert tuned.family() == "Monaco"
    assert tuned.pointSize() == 11


# -- preferences dialog ---------------------------------------------------------------


def test_preferences_dialog_ok_persists(monkeypatch: Any) -> None:
    """OK / Apply write the widgets into the GuiConfig and persist them
    to gui.json; Cancel leaves the config untouched."""
    _app()
    config = GuiConfig()
    dialog = PreferencesDialog(config)

    dialog._family.setCurrentFont(_fixed_font("Courier New", 0))
    dialog._size.setValue(17)
    dialog._notify_checks["notify_clarification"].setChecked(True)
    dialog._apply()
    assert config.font_family == "Courier New"
    assert config.font_size == 17
    assert config.notify_clarification
    assert not config.notify_completed
    # Persisted to disk, not just in memory.
    data = json.loads(config.path.read_text(encoding="utf-8"))
    assert data["font_size"] == 17
    assert data["notify_clarification"] is True

    # Cancel: change the widgets, reject — nothing is written.
    dialog._size.setValue(9)
    dialog._notify_checks["notify_clarification"].setChecked(False)
    dialog.reject()
    assert config.font_size == 17
    assert config.notify_clarification


# -- tray notifications ------------------------------------------------------------


class TrayHarness:
    """A TrayNotifier over a hidden offscreen "main window" plus the
    showMessage calls it made (monkeypatched into the tray icon)."""

    def __init__(self, config: GuiConfig) -> None:
        self.config = config
        self.window = QWidget()
        self.tray = TrayNotifier(config, self.window)
        self.shown: list[tuple[str, str]] = []
        self._tray = self.tray._tray
        monkeypatch_show(self._tray, self.shown)

    def close(self) -> None:
        self.tray._tray.hide()
        self.window.deleteLater()


def monkeypatch_show(tray_icon: Any, sink: list[tuple[str, str]]) -> None:
    def fake_show(title: str, body: str, *_args: Any, **_kwargs: Any) -> None:
        sink.append((title, body))

    tray_icon.showMessage = fake_show  # type: ignore[method-assign]


def _task(**kwargs: Any) -> Task:
    defaults: dict[str, Any] = {"title": "Fix the thing"}
    defaults.update(kwargs)
    return Task(**defaults)


def test_tray_transition_filter_by_origin_and_kind(tmp_path: Path) -> None:
    """System-origin transitions of the interesting kinds are shown per
    their checkbox; user-origin transitions are NEVER shown (the user
    just caused them), and uninteresting transitions are ignored."""
    _app()
    config = GuiConfig()
    config.notify_completed = True
    harness = TrayHarness(config)
    try:
        tray = harness.tray
        tray.set_project("proj")

        # User origin: suppressed even with the checkbox on.
        tray.task_transition(
            _task(), TaskStatus.TESTING, TaskStatus.COMPLETED, "user"
        )
        assert harness.shown == []

        # System origin, checkbox off: no message.
        clarify_task = _task()
        tray.task_transition(
            clarify_task,
            TaskStatus.QUEUED,
            TaskStatus.AWAITING_CLARIFICATION,
            "system",
        )
        assert harness.shown == []

        # System clarification with the checkbox on: shown.
        config.notify_clarification = True
        tray.task_transition(
            clarify_task,
            TaskStatus.QUEUED,
            TaskStatus.AWAITING_CLARIFICATION,
            "system",
        )
        assert len(harness.shown) == 1
        assert "awaiting an answer" in _status_line(
            harness.shown[0][1], clarify_task
        )

        # A FAILED-verdict completion is a tester failure, NOT a
        # plain completion (the core's own event mapping).
        failed = _task(verification=VerificationStatus.FAILED)
        tray.task_transition(failed, TaskStatus.TESTING, TaskStatus.COMPLETED, "system")
        assert len(harness.shown) == 1  # tester_failed checkbox is off

        config.notify_tester_failed = True
        tray.task_transition(failed, TaskStatus.TESTING, TaskStatus.COMPLETED, "system")
        assert len(harness.shown) == 2
        assert "FAILED" in harness.shown[-1][1]

        # A requeue (TESTING → QUEUED) is the tester-failure kind too.
        tray.task_transition(_task(), TaskStatus.TESTING, TaskStatus.QUEUED, "system")
        assert len(harness.shown) == 3

        # An ACTIVE transition is not a tray event at all.
        tray.task_transition(_task(), TaskStatus.QUEUED, TaskStatus.ACTIVE, "system")
        assert len(harness.shown) == 3
    finally:
        harness.close()


def _status_line(body: str, task: Task) -> str:
    return body.replace(f'#{task.id} "{task.title}" — ', "")


def test_tray_settled_prime_and_dedup(tmp_path: Path) -> None:
    """The settled empty/blocked state: the first evaluation only primes
    (a cold start on an empty queue stays silent), repeats of the same
    state are deduplicated, a CHANGE is reported, and a run start resets
    the state so the next settle reports again."""
    _app()
    config = GuiConfig()
    config.notify_pipeline = True
    harness = TrayHarness(config)
    try:
        tray = harness.tray
        tray.set_project("proj")
        # Priming call (the pipeline's first _notifications_settled).
        tray.settled(blocked=False, detail="", prime=True)
        assert harness.shown == []
        # Same state again: deduped.
        tray.settled(blocked=False, detail="", prime=False)
        assert harness.shown == []
        # A change: the blocked state is reported with its detail.
        tray.settled(blocked=True, detail="Reasons: on hold: 2.", prime=False)
        assert len(harness.shown) == 1
        assert "No tasks can be taken into development" in harness.shown[0][1]
        assert "on hold: 2" in harness.shown[0][1]
        # Back to empty: reported (a new settle of a DIFFERENT state).
        tray.settled(blocked=False, detail="", prime=False)
        assert len(harness.shown) == 2
        # Same state: deduped again.
        tray.settled(blocked=False, detail="", prime=False)
        assert len(harness.shown) == 2
        # A run started (pipeline_busy): the state resets, so the next
        # settle of the same state reports again.
        tray.reset_busy()
        tray.settled(blocked=False, detail="", prime=False)
        assert len(harness.shown) == 3
    finally:
        harness.close()


def test_tray_settled_respects_checkbox(tmp_path: Path) -> None:
    """With the pipeline checkbox off the settled state is still tracked
    (no stale burst when the user enables it later) but not shown."""
    _app()
    config = GuiConfig()
    harness = TrayHarness(config)
    try:
        tray = harness.tray
        tray.set_project("proj")
        tray.settled(blocked=False, detail="", prime=True)
        tray.settled(blocked=True, detail="", prime=False)
        assert harness.shown == []
        # Visibility follows the checkboxes: none enabled → no tray icon.
        assert not tray._tray.isVisible()
    finally:
        harness.close()


def test_tray_suppressed_while_window_active(tmp_path: Path) -> None:
    """A notification is not shown while the main window is active —
    the user is looking at the pipeline already."""
    _app()
    config = GuiConfig()
    config.notify_completed = True
    harness = TrayHarness(config)
    try:
        tray = harness.tray
        tray.set_project("proj")
        # The hidden offscreen window is not active: delivered.
        tray.task_transition(
            _task(), TaskStatus.TESTING, TaskStatus.COMPLETED, "system"
        )
        assert len(harness.shown) == 1
        # An active main window: suppressed at the delivery slot.
        harness.window.isActiveWindow = lambda: True  # type: ignore[method-assign]
        tray.task_transition(
            _task(), TaskStatus.TESTING, TaskStatus.COMPLETED, "system"
        )
        assert len(harness.shown) == 1
    finally:
        harness.close()


def test_tray_visibility_follows_config_and_project() -> None:
    """The tray icon exists only while a project is open AND at least
    one notification kind is enabled; unchecking everything removes it.
    (The setVisible calls are recorded — the offscreen platform has no
    real system tray to query.)"""
    _app()
    config = GuiConfig()
    harness = TrayHarness(config)
    visible: list[bool] = []
    harness.tray._tray.setVisible = visible.append  # type: ignore[method-assign]
    try:
        tray = harness.tray
        assert not tray.any_enabled
        tray.set_project("proj")
        assert visible == [False]
        config.notify_completed = True
        tray.refresh()
        assert visible[-1] is True
        tray.set_project(None)
        assert visible[-1] is False
        # Back with the checkbox still on: the icon returns; unchecking
        # everything removes it even with a project open.
        tray.set_project("proj")
        assert visible[-1] is True
        config.notify_completed = False
        tray.refresh()
        assert visible[-1] is False
    finally:
        harness.close()


def test_bridge_forwards_settled_and_busy(tmp_path: Path) -> None:
    """TrayEventBridge: the ntfy base class runs untouched (its dedup
    state advances) while the tray receives the same events; a busy
    reset reaches the tray too."""
    _app()
    config = GuiConfig()
    config.notify_pipeline = True
    harness = TrayHarness(config)
    try:
        settings = ProjectSettings()  # ntfy off by default: the base
        # class stays silent, only the tray half of the bridge is heard.
        bridge = TrayEventBridge(harness.tray, lambda: settings, project="proj")
        bridge.pipeline_idle(blocked=False, detail="", prime=True)
        assert harness.shown == []
        bridge.pipeline_idle(blocked=True, detail="Reasons: on hold: 1.", prime=False)
        assert len(harness.shown) == 1
        # Same state: the TRAY dedups independently of the base class.
        bridge.pipeline_idle(blocked=True, detail="Reasons: on hold: 1.", prime=False)
        assert len(harness.shown) == 1
        bridge.pipeline_busy()
        bridge.pipeline_idle(blocked=True, detail="Reasons: on hold: 1.", prime=False)
        assert len(harness.shown) == 2
    finally:
        harness.close()


def test_shell_wires_tray_listener(tmp_path: Path, monkeypatch: Any) -> None:
    """A shell CONSTRUCTED with a tray (the window's open_project path):
    the bridge replaces the plain Notifier, the tray is registered as a
    manager status listener, and a system-origin transition through the
    REAL manager funnel reaches the tray (a user-origin one does not)."""
    _app()
    loop = QEventLoop(_app())
    asyncio.set_event_loop(loop)
    project_root = tmp_path / "proj"
    project_root.mkdir()
    hub = EventHub(log_path=None)
    config = GuiConfig()
    config.notify_completed = True
    harness = TrayHarness(config)

    async def driver() -> None:
        try:
            shell = CoreShell(project_root, QtHost(hub), hub, tray=harness.tray)
            tray = harness.tray
            tray.set_project("proj")
            shell.start()
            task = shell.create_task("Watch me finish")
            assert task is not None
            # A user transition: filtered out.
            task.status = TaskStatus.COMPLETED
            shell.manager.fire_status_change(task, TaskStatus.QUEUED, "user")
            assert harness.shown == []
            # The same transition with system origin: shown.
            shell.manager.fire_status_change(task, TaskStatus.QUEUED, "system")
            assert len(harness.shown) == 1
            assert isinstance(shell.notifier, TrayEventBridge)
            shell.shutdown()
        finally:
            harness.close()
        await _drain()

    try:
        with loop:
            loop.run_until_complete(driver())
    finally:
        asyncio.set_event_loop(None)
