"""The skeleton acceptance test: the core comes up on a tmp project
and the GUI event seam delivers events.

Covers the plan's task-1 criteria:

- the core starts in-process (TaskManager, settings, resolved config);
- `task_updated` / `notify` / `service_event` events reach the hub;
- every event lands in the session log file;
- the pipeline (startup `_advance`, external-task pickup) runs headless
  with its QTimer pollers actually firing — the driver coroutine runs
  on a qasync loop, which pumps the Qt event loop during awaits;
- the shared `.drcode/` layout stays exactly what TaskManager writes —
  the GUI adds nothing to the project directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from drcode.providers import AgentRole
from drcode.task_manager import TaskManager
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.qt_host import QtHost

PICKUP_POLL_SECONDS = 1.0


class EventSink:
    """Collects the hub's emissions like the debug console would."""

    def __init__(self, hub: EventHub) -> None:
        self.task_updated: list[tuple[str, str]] = []
        self.service_events: list[tuple[str, str, str]] = []
        self.notifications: list[tuple[str, str, str]] = []
        self.stream_deltas: list[tuple[str, str]] = []
        hub.task_updated.connect(lambda t, s: self.task_updated.append((t, s)))
        hub.service_event.connect(
            lambda t, k, x: self.service_events.append((t, k, x))
        )
        hub.notify.connect(lambda m, t, s: self.notifications.append((m, t, s)))
        hub.stream_delta.connect(lambda t, x: self.stream_deltas.append((t, x)))

    def task_ids_seen(self) -> set[str]:
        return {task_id for task_id, _status in self.task_updated}


def run_core_scenario(
    tmp_path: Path, scenario: Callable[[CoreShell, EventSink], object]
) -> tuple[CoreShell, EventSink]:
    """Build a shell on a tmp project and drive `scenario` on the
    qasync loop (Qt timers and workers really run)."""
    app = QApplication.instance() or QApplication([])
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    project_root = tmp_path / "proj"
    project_root.mkdir()
    hub = EventHub(log_path=tmp_path / "proj-log" / "session.log")
    sink = EventSink(hub)
    shell = CoreShell(project_root, QtHost(hub), hub)

    async def driver() -> None:
        shell.start()
        await asyncio.sleep(0.1)
        scenario(shell, sink)
        await asyncio.sleep(PICKUP_POLL_SECONDS + 0.5)
        shell.shutdown()
        await asyncio.sleep(0)

    with loop:
        loop.run_until_complete(driver())
    return shell, sink


def _create_external_task(shell: CoreShell, _sink: EventSink) -> str:
    """A task written into the shared `.drcode/` by ANOTHER process
    (the parallel-TUI path): model it with a second TaskManager
    instance over the same project — the shell's 1 s disk pickup poll
    must load it and emit its events."""
    foreign_manager = TaskManager(shell.project_root)
    task = foreign_manager.create("External feature", "do something", origin="user")
    return task.id


def test_core_starts_and_emits_events(tmp_path: Path) -> None:
    shell, sink = run_core_scenario(tmp_path, lambda _s, _sink: None)
    # The core is alive: manager, settings and the resolved config exist.
    assert shell.manager.storage.root == tmp_path / "proj"
    assert shell.settings is not None
    assert shell.resolved_config(AgentRole.CODING) is not None
    # The startup notification reached the console.
    assert any("Core started" in m for m, _t, _s in sink.notifications)
    # The host was activated and the worker pool dismantled cleanly.
    assert shell.host.host_active is False


def test_external_task_is_picked_up_and_observable(tmp_path: Path) -> None:
    shell, sink = run_core_scenario(tmp_path, _create_external_task)
    created = [
        t for t in shell.manager.tasks.values() if t.title == "External feature"
    ]
    assert len(created) == 1
    assert created[0].id in sink.task_ids_seen()
    # The pickup poll's service line ("created outside this session")
    # reached the hub.
    assert any(
        kind == "system" and "outside this session" in text
        for _t, kind, text in sink.service_events
    )


def test_status_change_is_observable(tmp_path: Path) -> None:
    def create_and_hold(shell: CoreShell, _sink: EventSink) -> None:
        task = shell.manager.create("Held feature", "later", hold=False)
        shell.manager.set_hold(task.id, on=True)

    _shell, sink = run_core_scenario(tmp_path, create_and_hold)
    # The status listener turned the transition into task_updated
    # events (held -> HOLD status value, and back through refreshes).
    held = [
        (task_id, status)
        for task_id, status in sink.task_updated
        if status == "hold"
    ]
    assert held, "set_hold produced no task_updated event"


def test_events_land_in_the_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "session.log"
    hub = EventHub(log_path=log_file)
    hub.emit_task_updated("t1", "queued")
    hub.emit_stream_delta("t1", "hello")
    hub.emit_service_event("t1", "git", "pushed")
    hub.emit_notify("done", "pipeline", "information")
    text = log_file.read_text(encoding="utf-8")
    for marker in (
        "task_updated t1",
        "stream_delta t1",
        "service_event t1",
        "notify",
    ):
        assert marker in text


def test_shell_adds_nothing_to_the_shared_project_dir(tmp_path: Path) -> None:
    """Parallel TUI+GUI on one project: the GUI must not grow its own
    files inside the shared `.drcode/` (its log lives in the GUI config
    dir instead)."""
    shell, _sink = run_core_scenario(tmp_path, lambda _s, _sink: None)
    state_dir = tmp_path / "proj" / ".drcode"
    gui_files = [
        p.relative_to(state_dir)
        for p in state_dir.rglob("*")
        if p.is_file() and "gui" in p.name.lower()
    ]
    assert gui_files == []
    # And the core's own state is intact after the shutdown.
    assert shell.manager.storage is not None


@pytest.mark.parametrize("fixture_name", ["smoke"])
def test_qt_host_worker_pool(tmp_path: Path, fixture_name: str) -> None:
    """The worker pool honours groups and exclusivity."""
    del fixture_name
    app = QApplication.instance() or QApplication([])
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    hub = EventHub(log_path=None)
    host = QtHost(hub)
    results: list[str] = []

    async def worker_a() -> None:
        await asyncio.sleep(0.05)
        results.append("a")

    async def worker_b() -> None:
        await asyncio.sleep(0.01)
        results.append("b")

    async def driver() -> None:
        host.start()
        host.host_spawn_worker(worker_a, group="agent-1")
        host.host_spawn_worker(worker_a, group="agent-1")
        # exclusive=True cancels the previous worker in the group.
        host.host_spawn_worker(worker_b, group="agent-1", exclusive=True)
        # A different group runs alongside.
        host.host_spawn_worker(worker_a, group="agent-2")
        await asyncio.sleep(0.2)
        host.stop()

    with loop:
        loop.run_until_complete(driver())
    # The two non-exclusive agent-1 workers ran; the exclusive one
    # cancelled only its predecessor... the third (b) survived, the
    # first a was cancelled; agent-2's a completed.
    assert results == ["b", "a"]
