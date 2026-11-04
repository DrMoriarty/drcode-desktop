"""The Qt implementation of the pipeline host seam.

The exact analogue of ``drcode.tui.host.TextualHost``: a thin adapter
over the UI toolkit's primitives, no pipeline logic. Where the TUI
forwards to ``App.notify`` / ``App.set_timer`` / ``App.run_worker``,
this class forwards to QTimer, ``QTimer.singleShot(0)`` and an
asyncio-task worker pool with textual's group/exclusive semantics.

Threading: with qasync the pipeline's asyncio loop rides the Qt event
loop on the main thread, so timers, worker bookkeeping and callbacks
all run on ONE thread — the same single-thread semantics the pipeline
expects (worker bodies run there, exactly like the textual ``@work``
workers).
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
import logging
from typing import Any

from PySide6.QtCore import QObject, QTimer

from .event_hub import EventHub
from .stream_block import StreamBlockStub


class QtHost(QObject):
    """Implements the ``PipelineHost`` protocol over Qt primitives."""

    def __init__(self, hub: EventHub) -> None:
        super().__init__()
        self._hub = hub
        # Deferred ticks / worker starts are skipped once the shell is
        # being dismantled (textual's ``_exit`` / ``is_running`` pair).
        self._active = False
        self._model_status_is_error = False
        # Attribution of newly created stream blocks. Task 3 wires this
        # to the viewed task; the skeleton keeps it empty (blocks are
        # logged, not mounted into a task view).
        self.current_task_id = ""
        # Worker pool: group name -> the in-flight asyncio task. Textual's
        # worker registry is the semantic base of the per-task run groups
        # (``agent-<task-id>``) and the single tester/resolver slots.
        self._workers: dict[str, dict[str, asyncio.Task[None]]] = {}
        self._timers: set[QTimer] = set()

    # -- lifecycle ------------------------------------------------------------

    @property
    def host_active(self) -> bool:
        return self._active

    def start(self) -> None:
        self._active = True

    def stop(self) -> None:
        """Dismantle: stop timers and cancel every in-flight worker."""
        self._active = False
        for timer in list(self._timers):
            timer.stop()
        self._timers.clear()
        for group in self._workers.values():
            for task in group.values():
                task.cancel()

    # -- toasts ---------------------------------------------------------------

    def host_notify(
        self,
        message: str,
        *,
        title: str | None = None,
        severity: str = "information",
        timeout: float | None = None,
    ) -> None:
        del timeout  # The debug console keeps every toast; no auto-hide.
        self._hub.emit_notify(message, title or "", severity)

    # -- deferred calls ---------------------------------------------------------

    def host_set_timer(self, delay: float, callback: Any) -> Any:
        """Schedule a deferred callback on the UI event loop (the
        textual ``App.set_timer`` analogue)."""
        return self._single_shot(delay, callback)

    def host_call_after_refresh(self, callback: Any, *args: Any) -> Any:
        """Run after the next UI refresh pass: a zero-timeout single
        shot re-enters the event loop once (the textual
        ``App.call_after_refresh`` analogue)."""
        return QTimer.singleShot(0, lambda: self._guard(callback, *args))

    def _single_shot(self, delay: float, callback: Any) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(max(0, int(delay * 1000)))
        # Every timer tick is guarded: a callback raising must not kill
        # the event loop (textual wraps worker/timer errors the same way).
        # The timer is retired from the bookkeeping once it fired.
        def _tick() -> None:
            self._timers.discard(timer)
            self._guard(callback)

        timer.timeout.connect(_tick)
        self._timers.add(timer)
        timer.start()
        return timer

    @staticmethod
    def _guard(callback: Any, *args: Any) -> None:
        try:
            result = callback(*args)
        except Exception:
            logging.exception("host timer/callback failed")
            return
        # Timer callbacks may be coroutine functions (textual's set_timer
        # accepts them): schedule the coroutine on the loop.
        if inspect.iscoroutine(result):
            asyncio.get_running_loop().create_task(result)

    # -- worker pool ------------------------------------------------------------

    def host_spawn_worker(
        self,
        factory: Any,
        *,
        name: str = "",
        group: str = "default",
        exclusive: bool = False,
    ) -> Any:
        """Start a background worker running ``factory()`` (an awaitable
        factory) — the textual ``App.run_worker`` dispatch. With
        ``exclusive=True`` a starting worker cancels the previous one in
        the same group."""
        if not self._active:
            return None
        bucket = self._workers.setdefault(group, {})
        if exclusive:
            for task in bucket.values():
                task.cancel()
            bucket.clear()
        task = asyncio.get_running_loop().create_task(self._run_worker(factory))
        bucket[task.get_name() or name or str(id(task))] = task
        task.add_done_callback(lambda _t: self._worker_done(group, task))
        return task

    def cancel_worker_group(self, group: str) -> None:
        """Cancel every in-flight worker of `group` (the textual
        ``workers.cancel_group`` analogue, used by the abort paths)."""
        for task in self._workers.get(group, {}).values():
            task.cancel()
        self._workers.get(group, {}).clear()

    def _worker_done(self, group: str, task: asyncio.Task[None]) -> None:
        bucket = self._workers.get(group)
        if bucket is not None:
            for key, value in list(bucket.items()):
                if value is task:
                    del bucket[key]
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._hub.emit_host_log(f"worker {group} failed: {exc!r}")

    async def _run_worker(self, factory: Any) -> None:
        result = factory()
        # Textual workers accept awaitable factories; guard a plain value
        # the same way.
        if inspect.isawaitable(result):
            await result

    # -- diagnostics -------------------------------------------------------------

    def host_log(self, message: Any) -> None:
        self._hub.emit_host_log(str(message))

    # -- header connection indicator (model-status probe) --------------------

    def host_model_status_is_error(self) -> bool:
        return self._model_status_is_error

    def host_publish_model_status(self, label: str, state: str) -> None:
        self._model_status_is_error = state == "error"
        self._hub.emit_host_log(f"model_status: {label} ({state})")
        self._hub.emit_notify(label, "Model status", state)

    # -- live chat blocks ------------------------------------------------------

    def host_new_reasoning_block(self, at: datetime.datetime | None = None) -> Any:
        return StreamBlockStub(self._hub, self.current_task_id, "reasoning", at)

    def host_new_assistant_block(self) -> Any:
        return StreamBlockStub(self._hub, self.current_task_id, "assistant")

    def host_new_tool_call_block(self, call_text: str) -> Any:
        block = StreamBlockStub(self._hub, self.current_task_id, "tool-call")
        block.set_content(call_text)
        self._hub.emit_service_event(self.current_task_id, "tool", call_text)
        return block
