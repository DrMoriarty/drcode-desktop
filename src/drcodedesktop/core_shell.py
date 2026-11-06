"""CoreShell: the GUI-side composition of the drcode core.

The exact analogue of the Textual app's layer stack: the UI-neutral
:class:`drcode.core.pipeline.PipelineLayer` (CoreState + every pipeline
sub-layer) composed with the shell's own minimal implementations of the
UI-layer protocol. The Textual app implements those ~40 protocol stubs
across ``tui/chat_view`` / ``tui/chat_timeline`` / ``tui/sidebar_view``
/ ...; task 1 implements the subset the headless pipeline actually
drives (service events, chat-ish writes, refresh signals, activity
bookkeeping) as event-hub emissions, and leaves the pure-view stubs as
inherited no-ops (the core never requires them to do anything).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections import deque
from functools import partial
from pathlib import Path
from typing import Any

from drcode.agent import is_pending_retry
from drcode.config import Config
from drcode.core.pipeline import PipelineLayer
from drcode.core.usage import SessionTokenUsage
from drcode.github import GitHubSyncState, GitHubSyncStateStore
from drcode.keepawake import KeepAwake
from drcode.models import (
    Clarification,
    PendingInput,
    Task,
    TaskEvent,
    TaskKind,
    TaskStatus,
)
from drcode.notifications import Notifier
from drcode.providers import (
    AgentRole,
    CustomModelEntry,
    ModelRef,
    load_project_model_slots,
    migrate_project_config,
    migrate_user_config,
    project_has_model_selection,
    set_project_custom_active,
    set_project_custom_model,
    set_project_model,
)
from drcode.settings import (
    ProjectSettings,
    ProjectSettingsStore,
    WorktreeRemoval,
)
from drcode.task_env import TaskEnvironment
from drcode.task_manager import TaskManager, validate_dependencies
from drcode.tools import cleanup_orphan_scratches
from drcode.usage_store import UsageStore, usage_file
from drcode.workflow import load_hooks
from PySide6.QtCore import QTimer

from .activity_view import fmt_age
from .event_hub import EventHub
from .qt_host import QtHost
from .tray_notifications import TrayEventBridge, TrayNotifier


class CoreShell(PipelineLayer):
    """One live drcode core, driven headless through the Qt host."""

    def __init__(
        self,
        project_root: Path,
        host: QtHost,
        hub: EventHub,
        tray: TrayNotifier | None = None,
    ) -> None:
        self._intervals: list[QTimer] = []
        # CoreState / layer attributes are assigned by this __init__ —
        # the same contract DrCodeApp.__init__ owns for the TUI.
        super().__init__()
        self._host = host
        self._hub = hub
        # The UI seam of the core pipeline (toasts, timers, workers,
        # chat blocks) — the pipeline layers call nothing Qt directly.
        # Typed as the core's protocol (the base declaration); the
        # concrete QtHost is kept in self._host.
        self.host = host
        # The loop thread: everything pipeline-related runs there (the
        # qasync loop rides the Qt main thread); daemon threads of the
        # notifier/worktree teardown check against it.
        self._thread_id = threading.get_ident()
        # Resolve once: a relative or symlinked root would leak into
        # storage, git and the tool sandbox.
        project_root = Path(project_root).resolve()
        self.project_root = project_root
        # One-time migrations BEFORE the config is built (the same order
        # the TUI uses; both idempotent, never fatal).
        migrate_user_config()
        migrate_project_config(project_root)
        # Layered config: env > project (.env / drcode.json) >
        # ~/.config/drcode/drcode.json > defaults.
        self.config = Config(project_root=project_root)
        self.manager = TaskManager(project_root)
        self.settings_store = ProjectSettingsStore(project_root)
        self.settings: ProjectSettings = self.settings_store.load()
        # Per-task worktree isolation: holds the LIVE settings instance.
        self.task_env = TaskEnvironment(project_root, self.settings)
        self._unmerged_tracking_registered = False
        self.task_env.event_log = self._task_env_event
        self.manager.add_removal_listener(self._remove_worktree_background)
        self.manager.add_completion_listener(self._completion_removal_background)
        # RULE 1 of the worktree contract: snapshot on every settle.
        self.manager.add_status_listener(self._snapshot_on_settled)
        # The GUI's own observer: every status transition becomes a
        # task_updated event (the debug console's primary feed).
        self.manager.add_status_listener(self._on_status_change)
        self.manager.add_creation_listener(self._on_task_created)
        # Strong references to in-flight background snapshot tasks.
        self._snapshot_jobs: set[asyncio.Task[None]] = set()
        # GitHub Issues sync bookkeeping (the poller itself is wired by
        # the window like the TUI's on_mount does).
        self._github_state_store = GitHubSyncStateStore(project_root)
        self._github_state: GitHubSyncState = self._github_state_store.load()
        self._github_next_sync = time.monotonic() + 15.0
        self._github_syncing = False
        self._github_last_error: str | None = None
        # ntfy.sh notifications (no TTS speaker in the shell yet). With a
        # tray bridge the notifier ALSO taps the pipeline's settled/busy
        # transitions for the GUI's own tray notifications (the ntfy
        # behaviour itself is untouched; per-task ntfy events stay
        # unwired in the shell — the tray listens to the manager
        # directly below).
        if tray is None:
            notifier: Notifier = Notifier(
                lambda: self.settings,
                project=project_root.name,
                project_root=project_root,
                on_error=self._notify_error,
            )
        else:
            notifier = TrayEventBridge(
                tray,
                lambda: self.settings,
                project=project_root.name,
                project_root=project_root,
                on_error=self._notify_error,
            )
            # The GUI's own tray notifications: system-origin task
            # transitions (the announcement-language source rides the
            # live settings instance).
            tray.set_settings_getter(lambda: self.settings)
            self.manager.add_status_listener(tray.task_transition)
        self.notifier = notifier
        # Keep-awake (system-sleep assertion while the pipeline has work).
        self.keepawake = KeepAwake()
        # "Queue empty" notifications fire on transitions only.
        self._notifications_primed = False
        self._purged_tasks = self.manager.purge_expired_completed(
            self.settings.completed_retention.days
        )
        cleanup_orphan_scratches(set(self.manager.tasks))
        # Custom team workflow (.drcode/workflow.py hooks); None = absent.
        self.workflow = load_hooks(project_root)
        # External-review bookkeeping (session-scoped).
        self._review_running = False
        self._review_task_id: str | None = None
        self._review_retry_at: dict[str, float] = {}
        self._review_fail_count: dict[str, int] = {}
        self._review_poll_at = 0.0
        self._dev_done_notes: dict[str, str] = {}
        self._accept_hook_lock = asyncio.Lock()
        self._hook_merge_audit_ok: dict[str, float] = {}
        # Per-task mergedness of completed tasks' branches (a stub here:
        # the git-indicator poll is task-7 parity).
        self._merge_state: dict[str, str] = {}
        self._merge_binding_visible: bool | None = None
        self._merge_poll_done = False
        self._install_rerun_ids: list[str] = []
        self._install_rerun_running = False
        self.viewed_id: str | None = None
        # Counted run state (parallel development slots).
        self._agent_runs: dict[str, int] = {}
        self.tester_running = False
        self._tester_task_id: str | None = None
        self.new_task_kind: TaskKind = TaskKind.FEATURE
        # Per task: run generation bookkeeping.
        self._agent_generations: dict[str, int] = {}
        self._pending_rendered: list[str] = []
        self._consumed_prompts: dict[str, tuple[str, list[PendingInput]]] = {}
        self._chat_has_content = False
        # Tester failure / retry bookkeeping.
        self._tester_fail_count: dict[str, int] = {}
        self._tester_retry_at: dict[str, float] = {}
        self._tester_net_count: dict[str, int] = {}
        self._tester_resume_round: set[str] = set()
        self._tester_overflow_retried: set[str] = set()
        self._tester_compaction_resumed: set[str] = set()
        self._tester_compaction_verdict: set[str] = set()
        # Merge-conflict resolver bookkeeping (single-slot worker).
        self._resolver_running: bool = False
        self._resolver_task_id: str | None = None
        self._resolver_generation: int = 0
        self._resolver_fail_count: dict[str, int] = {}
        self._resolver_retry_at: dict[str, float] = {}
        self._resolver_net_count: dict[str, int] = {}
        self._resolver_resume_round: set[str] = set()
        self._resolver_compaction_failed: set[str] = set()
        self._resolver_hard_limit_exhausted: set[str] = set()
        self._resolver_hard_limit_compactions: dict[str, int] = {}
        self._resolver_threshold_compactions: dict[str, int] = {}
        # Failed-queue bookkeeping.
        self._agent_failed: set[str] = set()
        self._agent_dead: set[str] = set()
        self._failed_queue_rounds = 0
        self._failed_queue_retry_at = 0.0
        # Dependency gate.
        self._dep_gate: dict[str, dict[str, str]] = {}
        self._dep_block_notified: set[str] = set()
        # Agent network-retry bookkeeping.
        self._agent_net_count: dict[str, int] = {}
        self._agent_net_retry_at: dict[str, float] = {}
        self._net_retries_total = 0
        # Session statistics.
        self._session_known_ids: set[str] = set(self.manager.tasks)
        self._session_completed_ids: set[str] = {
            t.id
            for t in self.manager.tasks.values()
            if t.status is TaskStatus.COMPLETED
        }
        self._session_created: dict[TaskKind, int] = dict.fromkeys(TaskKind, 0)
        self._session_completed = 0
        self._session_usage = SessionTokenUsage()
        self._usage_store = UsageStore(path=usage_file())
        self._session_durations: list[float] = []
        # Run-state activity bookkeeping (parallel slots).
        self._last_activity: dict[str, float] = {}
        self._current_tool: dict[str, str | None] = {}
        # Live tool-call blocks by run/call id (spinner stops on result).
        self._active_tools: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._tps_samples: dict[str, deque[tuple[float, int]]] = {}
        self._conn_label = "○ not probed (skeleton)"
        self._conn_role: AgentRole | None = None
        self._last_probe_ms: int | None = None
        self._probe_fail_streak = 0
        self._tester_generation = 0
        # Context fill / compaction bookkeeping.
        self._live_ctx_tokens: dict[str, int] = {}
        # Eviction credit per task (see drcode.core.state): the ≈tokens the
        # batch eviction of agent-marked tool results collapsed, subtracted
        # from the dev transcript's fill readings until a fresh usage
        # report consumes it.
        self._evicted_ctx_tokens: dict[str, int] = {}
        self._live_ctx_tester: dict[str, int] = {}
        self._live_ctx_resolver: dict[str, int] = {}
        self._compaction_failed: set[str] = set()
        self._hard_limit_exhausted: set[str] = set()
        self._hard_limit_compactions: dict[str, int] = {}
        self._threshold_compactions: dict[str, int] = {}
        self._tester_threshold_compactions: dict[str, int] = {}
        self._threshold_under_obs: dict[str, int] = {}
        # Chat-epoch / delta bookkeeping (stubs: no chat view yet).
        self._chat_epoch = 0
        self._rendered_clarification: Clarification | None = None
        # Per-role resolved model connections (cached — see core.state).
        self._resolved_configs: dict[AgentRole, Config] = {}
        self._resolved_model_keys: dict[AgentRole, str] = {}

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Bring the pipeline up (the headless subset of the TUI's
        on_mount): listeners, pollers and the startup `_advance()`."""
        self._host.start()
        self._intervals: list[QTimer] = []
        # External task pickup: task files written by other processes
        # (drcode --feature-request, one-shot GitHub sync) join the
        # queue without a restart. Also the GUI↔TUI parallel-run proof:
        # both processes watch the same .drcode/ storage.
        self.set_interval(1.0, self._poll_new_tasks)
        # Dependency-gate unblock tick (a foreign `git merge` of a dep
        # must unblock waiting dependents in an idle pipeline).
        self.set_interval(30, self._poll_dependency_unblock)
        # Keep-awake safety tick.
        self.set_interval(5, self._update_keepawake)
        # Stall watchdog (plan task 7): a 1 s tick aborts and restarts a
        # run that has emitted nothing for the configured stall limit.
        self.set_interval(1.0, self._poll_stall)
        # Pre-stamp the isolation pins of tasks that were mid-work at
        # the previous shutdown (must land BEFORE the first _advance).
        if self.task_env.available:
            try:
                pinned = self.task_env.prestamp_isolation_pins(
                    list(self.manager.tasks.values())
                )
            except Exception:
                pinned = []
            for t in pinned:
                self.manager.storage.save(t)
        # Startup resume: re-test interrupted TESTING tasks and promote
        # the queue — exactly the TUI's startup branch, minus the setup
        # dialog (the debug console announces the parked state instead).
        self.refresh_sidebar()
        self.refresh_status()
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()
        elif not project_has_model_selection(self.project_root):
            self.host.host_notify(
                "No model configured for this project — the queue stays "
                "parked until one is set (drcode.json / DRCODE_MODEL).",
                title="drcode core",
                severity="warning",
            )
        self.host.host_notify(
            f"Core started on {self.project_root} ({len(self.manager.tasks)} tasks).",
            title="drcode core",
        )

    def shutdown(self) -> None:
        """Tear the core down: stop pollers, cancel workers, release
        keep-awake."""
        self._host.stop()
        for timer in getattr(self, "_intervals", []):
            timer.stop()
        self._intervals = []
        self.keepawake.stop()

    # -- polling helpers (the TUI's App.set_interval analogue) -----------------

    def set_interval(self, seconds: float, callback: Any) -> None:
        """A repeating callback on the loop thread (QtHost handles the
        QTimer plumbing and the exception guard)."""
        timer = QTimer(self._host)
        timer.setInterval(int(seconds * 1000))
        timer.timeout.connect(lambda: QtHost._guard(callback))
        timer.start()
        self._intervals.append(timer)

    # -- manager observers (task_updated feed) -----------------------------------

    def _on_status_change(
        self, task: Task, old: TaskStatus, new: TaskStatus, origin: str
    ) -> None:
        del old, origin
        # Status listeners may fire from worker threads; the hub signal
        # is queued safely into the GUI thread by Qt.
        self._hub.emit_task_updated(task.id, new.value)
        # The pipeline's own refresh calls stay on the loop thread.
        self._hub.append_log(f"status {task.id}: {new.value}")

    def _on_task_created(self, task: Task, origin: str) -> None:
        self._hub.emit_task_updated(task.id, task.status.value)
        self._hub.append_log(f"created {task.id} ({origin})")

    # -- UI-layer protocol: service events / chat writes -------------------------

    def _log_event(self, task: Task, text: str, *, kind: str = "system") -> None:
        """Persist a service event into the task's event log (never fed
        to the model — see tui.chat_timeline for the original)."""
        task.events.append(TaskEvent(kind=kind, text=text))
        task.touch()
        self.manager.storage.save(task)

    def _show_event(self, task: Task, text: str, *, kind: str = "system") -> None:
        """Persist an event and surface it on the event hub."""
        self._log_event(task, text, kind=kind)
        self._hub.emit_service_event(task.id, kind, text)

    def _show_event_once(self, task: Task, text: str, *, kind: str = "system") -> None:
        """`_show_event` deduplicated per task and wording."""
        if not any(e.text == text for e in task.events):
            self._show_event(task, text, kind=kind)

    def _write_chat(
        self,
        content: Any,
        *,
        expand: bool = False,
        note: Any = None,
    ) -> None:
        """Headless chat write: a StreamBlock was already handed to the
        open chat view at creation (the QtHost sink); anything else is
        logged verbatim into the session log."""
        del expand, note
        text = getattr(content, "get_content", None)
        if callable(text):
            self._hub.append_log(f"chat [{getattr(content, 'kind', '?')}]")
        else:
            self._hub.append_log(f"chat {content!r}")

    def _write_history_line(self, role: str, text: str, at: Any = None) -> Any:
        """A live history write of a running pipeline (a consumed prompt
        entering the view, a testing/resolving separator): forwarded to
        the open chat view (the persisted transcript covers it at the
        next rebuild). A queued-message pre-draw ("user-pending") also
        registers the text in ``_pending_rendered`` — exactly what the
        TUI's chat timeline does on the same write — so the consuming
        run's node-boundary draw skips it and the bubble shows once."""
        if role == "user-pending" and text not in self._pending_rendered:
            self._pending_rendered.append(text)
        self._hub.append_log(f"history {role}: {text!r}")
        self._host.stream_sink_write_line(role, text, at)
        return None

    def _write_clarification(self, clarification: Clarification) -> None:
        self._hub.append_log(
            "clarification: " + "; ".join(q.question for q in clarification.questions)
        )
        self._host.stream_sink_write_clarification(clarification)

    def _isolation_pin_note(self, task: Task, *, save: bool) -> None:
        """Run-boundary bookkeeping for the isolation pin (mirrors
        tui.chat_timeline; the wording differs only in the sink)."""
        pin = task.isolation_pinned
        if pin is None:
            return
        if pin != self.task_env.active:
            text = (
                "Task pinned to worktree isolation (the setting is now off)."
                if pin
                else "Task pinned to the project directory (worktree "
                "isolation is now on)."
            )
            self._show_event_once(task, text)
            return
        if save:
            task.touch()
            self.manager.storage.save(task)

    # -- UI-layer protocol: refresh signals ---------------------------------------

    def refresh_sidebar(self) -> None:
        """The sidebar poll of task 2 will build a real model; task 1
        emits a coarse queue snapshot as task_updated events."""
        for task in self.manager.all_tasks():
            self._hub.emit_task_updated(task.id, task.status.value)

    def refresh_status(self) -> None:
        self._hub.append_log("refresh_status")

    def refresh_task_status(self) -> None:
        self._hub.append_log("refresh_task_status")

    def refresh_todo_panel(self) -> None:
        self._hub.append_log("refresh_todo_panel")

    # -- UI-layer protocol: activity / tool bookkeeping ---------------------------

    def _touch_activity(
        self, task_id: str, tool: str | None = None, *, done: bool = False
    ) -> None:
        self._last_activity[task_id] = time.monotonic()
        if done:
            self._current_tool[task_id] = None
        elif tool is not None:
            self._current_tool[task_id] = tool

    def _tool_call_started(
        self,
        run_key: tuple[str, str, int],
        task_id: str,
        call_id: str,
        call_text: str,
        block: Any,
    ) -> None:
        # The live tool-call block keeps spinning in the chat view until
        # the result event stops it (registry over all runs — the agent
        # and the tester never overlap, but the lookup covers both).
        self._active_tools.setdefault(run_key, {})[call_id] = block
        self._touch_activity(task_id, call_text)
        self._hub.emit_service_event(task_id, "tool", call_text)

    def _tool_call_finished(self, call_id: str) -> None:
        for per_run in self._active_tools.values():
            block = per_run.pop(call_id, None)
            if block is not None:
                stop = getattr(block, "stop_spinner", None)
                if callable(stop):
                    stop()
        self._hub.append_log(f"tool finished {call_id}")

    def _stop_run_tools(self, run_key: tuple[str, str, int]) -> None:
        """End-of-run cleanup: a call whose result event was lost to an
        abort/error loses its spinner."""
        for block in self._active_tools.pop(run_key, {}).values():
            stop = getattr(block, "stop_spinner", None)
            if callable(stop):
                stop()

    def _record_session_usage(self, usage: Any, role: AgentRole, model_id: str) -> None:
        """Fold one model call's usage into the session totals and the
        persistent per-day store (statistics never fatal)."""
        if usage is None:
            return
        self._session_usage.add(usage)
        # Statistics are never fatal (a failed write — disk full, a
        # read-only config dir — costs at most this call's counters).
        with contextlib.suppress(Exception):
            self._usage_store.record(role, model_id, usage)

    def _collect_session_stats(self) -> None:
        """Fold task-tree changes into the session counters (the port of
        ``tui.header_view._collect_session_stats``): tasks that appeared
        since the previous tick count as created this session (by kind),
        tasks that reached COMPLETED count as processed, with the
        activation → completion span (a task cancelled straight from the
        queue has no activation stamp and is counted without a
        duration). Runs on the 1 s activity poll."""
        tasks = self.manager.tasks
        for task_id in set(tasks) - self._session_known_ids:
            self._session_known_ids.add(task_id)
            self._session_created[tasks[task_id].kind] += 1
        for task in tasks.values():
            if (
                task.status is TaskStatus.COMPLETED
                and task.id not in self._session_completed_ids
            ):
                self._session_completed_ids.add(task.id)
                self._session_completed += 1
                if task.activated_at and task.completed_at:
                    self._session_durations.append(
                        (task.completed_at - task.activated_at).total_seconds()
                    )

    # -- stall watchdog (plan task 7, port of tui.activity_view._poll_stall) -----

    def _poll_stall(self) -> None:
        """Watchdog: when a running agent/tester/resolver has emitted
        nothing (no stream deltas, no tool events) for the configured
        stall limit, abort the hung run and restart it — ONLY that run:
        with parallel development slots every run gets its own stall
        deadline and its own nudge, and the healthy siblings keep
        running. A stalled run blocks its own task — and through the
        pipeline gate, the whole backlog — so this keeps an overnight
        run going without human intervention."""
        limit = self.settings.stall_nudge.minutes
        if limit is None:
            return
        now = time.monotonic()
        running: list[tuple[str, str]] = [
            (task_id, "agent") for task_id in list(self._agent_runs)
        ]
        if self.tester_running and self._tester_task_id:
            running.append((self._tester_task_id, "tester"))
        if self._resolver_running and self._resolver_task_id:
            running.append((self._resolver_task_id, "resolver"))
        for task_id, who in running:
            stamped = self._last_activity.get(task_id)
            if stamped is None:
                continue  # the run has not started emitting yet
            stalled_for = now - stamped
            if stalled_for < limit * 60:
                continue
            task = self.manager.tasks.get(task_id)
            if task is None:
                continue
            self._show_event(
                task,
                f"{who} stalled for {fmt_age(int(stalled_for))} without any "
                f"activity — aborting the run (stall limit: "
                f"{self.settings.stall_nudge.label})",
            )
            self._nudge_run(task)

    # -- UI-layer protocol: connection / outage -----------------------------------

    def _active_header_role(self) -> AgentRole:
        return AgentRole.CODING

    def _recover_from_outage(self) -> None:
        self._hub.append_log("network recovered")

    def _note_network_recovered(self, task: Task, counts: dict[str, int]) -> None:
        del counts
        self._show_event(task, "Connection restored — the run continues.")

    # -- UI-layer protocol: view stubs (no chat view in task 1) --------------------

    def view_task(self, task_id: str) -> None:
        task = self.manager.tasks.get(task_id)
        if task is None:
            # A stale mention link (the task was deleted): a toast beats
            # a KeyError (the TUI's open_task behaviour).
            self._hub.emit_notify(f"Task #{task_id} not found.", "drcode", "warning")
            return
        self.viewed_id = task_id
        # Live blocks are attributed to the viewed task (the host stamps
        # new blocks with it and mounts them into the open chat view).
        self._host.current_task_id = task_id
        self._hub.emit_task_updated(task_id, task.status.value)

    # -- chat-view sinks (plan task 3: the GUI chat pane) -------------------------

    def _chat_at_bottom(self) -> bool:
        """Is the open chat view tracking the tail? (the pipeline's
        follow rule — the TUI's chat-timeline method, sink-routed)."""
        return self._host.stream_sink_at_bottom()

    def _stream_follow(self) -> None:
        self._host.stream_sink_follow()

    def _suggest_conflict_screen(self, task: Task) -> None:
        self._show_event(task, "Merge conflicts parked — open the merge screen.")

    def _unmerged_completed_ids(self) -> list[str]:
        """Ids of completed tasks whose ``drcode/<task-id>`` branch still
        holds commits the base branch does not have — the count behind
        the git indicator (the GUI port of
        ``tui.app._unmerged_completed_ids``, simplified: the wholesale
        ``drcode_branch_state`` snapshot over every completed task, no
        accept-hook merge audit and no stale triage-report sweep — those
        ship with the merge screen, plan task 9; see PARITY.md).
        Blocking git calls — run off the loop thread by the caller."""
        env = self.task_env
        if not env.active:
            self._merge_state = {}
            return []
        candidates = [
            t
            for t in self.manager.tasks.values()
            if t.status is TaskStatus.COMPLETED
        ]
        existing, unmerged = env.drcode_branch_state()
        self._merge_state = {
            t.id: (
                "unmerged"
                if env.branch(t.id) in unmerged
                else "merged"
                if env.branch(t.id) in existing
                else "none"
            )
            for t in candidates
        }
        return [t.id for t in candidates if env.branch(t.id) in unmerged]

    # -- user input / run control (plan task 4) ------------------------------------
    #
    # The GUI port of the TUI's user actions over a running task:
    # tui/actions_lifecycle._submit_user_text (the chat input submit),
    # _abort_active_runs (the confirmed ESC stop) and
    # tui/activity_view._nudge_run (Ctrl+R). The semantics live here, in
    # the shell — the widgets only route to these methods — so a test
    # can drive the exact production path headless.

    def submit_user_text(self, task_id: str, text: str) -> bool:
        """Queue a user message into the task and move the pipeline.

        The port of ``tui.actions_lifecycle._submit_user_text``: the text
        goes through ``manager.send`` (pending_input accumulation, the
        duplicate re-send guard, the clarification-answer re-queue with
        ``queue_head``, the completed/testing re-entry) — then, as in the
        TUI, an ACTIVE task's in-flight run is superseded by a fresh one
        (the exclusive ``agent-<id>`` group re-queues the interrupted
        attempt's message) and every other status re-enters ``_advance()``
        (promotion → ``run_agent``; an unfinished attempt resumes as a
        continuation turn, a queued batch is consumed as ONE prompt).

        Returns True when the message was accepted (False: unknown task,
        a draft, empty text, or a duplicate of something still queued /
        already in flight). The run start is gated on a configured model
        like every other shell entry point — the message itself is always
        recorded, exactly as in the TUI's no-model state.
        """
        task = self.manager.tasks.get(task_id)
        if task is None:
            self._hub.emit_notify(f"Task #{task_id} not found.", "drcode", "warning")
            return False
        if task.status is TaskStatus.DRAFT:
            # A draft never takes chat input (belt-and-braces, same as
            # the TUI: its text is composed in the task editor).
            self._hub.emit_notify(
                "A draft is composed in the task editor, not the chat.",
                "drcode",
                "warning",
            )
            return False
        stripped = text.strip()
        if not stripped:
            return False
        # A re-send of a text that is still queued (an identical entry
        # sits in pending_input) or already in flight (the transcript
        # holds it as the prompt of the unfinished attempt) is a no-op:
        # send() ignores it, and drawing/promoting it again would show
        # the comment twice.
        if any(e.text == stripped for e in task.pending_input) or is_pending_retry(
            task.messages, stripped
        ):
            return False
        # Capture the pre-run state: _advance() promotes the task to
        # ACTIVE (promote_next) and flips its status, so the pre-draw
        # decision below must use the ORIGINAL status.
        was_active = task.status is TaskStatus.ACTIVE
        self.manager.send(task_id, stripped)
        if not was_active and self.viewed_id == task_id:
            # Draw the queued message BEFORE advancing: the promoted run
            # consumes pending_input as its first step and would draw the
            # "user" block itself (registering the text in
            # _pending_rendered makes that draw skip it — see
            # _write_history_line). A rebuild re-renders the entry from
            # the timeline once, whichever side of the consumption window
            # it lands on.
            self._write_history_line("user-pending", stripped)
        if was_active:
            # A comment to the running task interrupts it: the exclusive
            # "agent-<id>" group cancels the in-flight run (its message
            # goes back to pending_input) and the new run resumes with
            # the comment.
            self._clear_stream_label()
            self.run_agent(task_id)
        elif self.resolved_config(AgentRole.CODING).is_ready:
            # Queued/clarification/... input: advance the pipeline —
            # testing backlog first, then the queue. (The TUI advances
            # unconditionally; the shell's other entry points gate the
            # RUN start on a configured model, and a run that is
            # guaranteed to fail on model construction is not semantics
            # worth reproducing — the send() state transition above
            # already happened.)
            self._advance()
        self.refresh_sidebar()
        self.refresh_status()
        return True

    def _clear_stream_label(self) -> None:
        """The TUI's stream-label teardown, on the sink seam: finish the
        open chat view's live blocks (spinner off, tail flushed) — an
        aborted/superseded run must not leave a spinning block behind."""
        self._host.stream_sink_stop_streaming()

    def stop_viewed_run(self) -> bool:
        """The actual stop — the port of
        ``tui.actions_lifecycle._abort_active_runs`` (what a confirmed
        ESC fires). With a single development slot (the default, or
        parallelism without worktree isolation) this is the historical
        global stop: whatever run is alive. With parallel slots it stops
        ONLY the currently viewed task's run. Every aborted run is
        marked ``unfinished`` (the resume flag the promotion and the
        continuation turn read) and leaves a service event in its chat,
        so the stop is never silent.

        Returns True when something was actually stopped."""
        stopped_tasks: list[Task] = []
        if self.parallel_limit <= 1:
            if not (self.agent_running or self.tester_running):
                return False
            stopped_tasks = [
                self.manager.get(tid)
                for tid in list(self._agent_runs)
                if tid in self.manager.tasks
            ]
            for group in [f"agent-{tid}" for tid in list(self._agent_runs)]:
                self._host.cancel_worker_group(group)
            if self.tester_running:
                self._host.cancel_worker_group("tester")
                if (
                    self._tester_task_id is not None
                    and self._tester_task_id in self.manager.tasks
                ):
                    stopped_tasks.append(self.manager.get(self._tester_task_id))
            self._agent_runs.clear()
            self.tester_running = False
            self._current_tool.clear()
            self._clear_stream_label()
            self.refresh_status()
        else:
            task = self.manager.tasks.get(self.viewed_id or "")
            if task is None:
                self._hub.emit_notify(
                    "Task is not running — select an active task to stop one",
                    "Stop run",
                    "warning",
                )
                return False
            stopped = False
            if task.id in self._agent_runs:
                self._host.cancel_worker_group(f"agent-{task.id}")
                self._agent_runs.pop(task.id, None)
                self._current_tool[task.id] = None
                stopped = True
                stopped_tasks.append(task)
            if self.tester_running and self._tester_task_id == task.id:
                self._host.cancel_worker_group("tester")
                self.tester_running = False
                self._current_tool[task.id] = None
                stopped = True
                stopped_tasks.append(task)
            if not stopped:
                self._hub.emit_notify(
                    "Task is not running — select an active task to stop one",
                    "Stop run",
                    "warning",
                )
                return False
            self._clear_stream_label()
            self.refresh_status()
        # The stop is explicit and shows the way back: the unfinished
        # mark makes promotion (and Ctrl+R / any new input) resume the
        # interrupted attempt as a continuation turn.
        for stopped in stopped_tasks:
            if stopped.status is TaskStatus.ACTIVE and not stopped.unfinished:
                # Mark HERE, not only in the run's CancelledError handler:
                # the worker may be cancelled before its first step — the
                # handler then never runs. Idempotent with the handler's
                # own marking.
                stopped.unfinished = True
                stopped.touch()
                self.manager.storage.save(stopped)
            self._show_event(
                stopped,
                "Run aborted by user (ESC) — press Ctrl+R to resume.",
            )
        self._hub.emit_notify(
            "Run stopped · Ctrl+R to resume", "Stop run", "information"
        )
        return True

    #: How long a nudge waits before restarting the aborted run (the
    #: TUI's activity-view constant: the cancellation must settle first).
    _NUDGE_RESTART_DELAY = 1.0

    def _nudge_run(self, task: Task, *, source: str = "watchdog") -> bool:
        """Abort the hung/stalled run for `task` (if one is in flight)
        and schedule its restart — the port of
        ``tui.activity_view._nudge_run`` (Ctrl+R, and the same move the
        stall watchdog makes automatically). The agent resumes via a
        continuation turn (no text is re-sent — ``task.unfinished``
        marks the interrupted attempt); a TESTING task gets a fresh
        verification round via the pipeline gate. `source` is accepted
        for the core protocol (the restart event wording of the TUI);
        returns whether a restart was scheduled."""
        del source
        scheduled = False
        if task.status is TaskStatus.ACTIVE:
            if task.id in self._agent_runs:
                self._host.cancel_worker_group(f"agent-{task.id}")
                self._agent_runs.pop(task.id, None)
                self._current_tool[task.id] = None
                self._clear_stream_label()
                self.refresh_status()
            self._schedule_pipeline_timer(
                self._NUDGE_RESTART_DELAY, partial(self.run_agent, task.id)
            )
            scheduled = True
        elif task.status is TaskStatus.TESTING:
            if self.tester_running:
                self._host.cancel_worker_group("tester")
                self.tester_running = False
                self._current_tool[task.id] = None
                self.refresh_status()
            self._schedule_pipeline_timer(self._NUDGE_RESTART_DELAY, self._advance)
            scheduled = True
        elif self.tester_running and self._tester_task_id == task.id:
            # The run hung AFTER its useful work: the status already
            # moved on mid-run, but the hung tester worker keeps
            # tester_running True and blocks the whole pipeline — abort
            # it and re-advance.
            self._host.cancel_worker_group("tester")
            self.tester_running = False
            self._current_tool[task.id] = None
            self.refresh_status()
            self._schedule_pipeline_timer(self._NUDGE_RESTART_DELAY, self._advance)
        elif self._resolver_running and self._resolver_task_id == task.id:
            # A hung conflict-resolver round: abort and restart the round
            # (the transcript is kept; the round resumes as a
            # continuation turn).
            self._host.cancel_worker_group("resolver")
            self._resolver_running = False
            self._resolver_task_id = None
            self._current_tool[task.id] = None
            self.refresh_status()
            self._schedule_pipeline_timer(
                self._NUDGE_RESTART_DELAY, partial(self.run_resolver, task.id)
            )
        self.refresh_sidebar()
        return scheduled

    def resume_viewed_run(self) -> bool:
        """Ctrl+R on the viewed task: abort its hung or stalled run and
        restart it — the one-action resume for a task left "active,
        nothing running" by a stop (ESC). Returns False when nothing is
        viewed."""
        task = self.manager.tasks.get(self.viewed_id or "")
        if task is None:
            self._hub.emit_notify(
                "Open a task to resume its run.", "Resume run", "warning"
            )
            return False
        self._nudge_run(task)
        return True

    # -- task management (plan task 5) ----------------------------------------------
    #
    # The GUI port of the TUI's task-lifecycle commands
    # (tui/actions_lifecycle.py): creation, hold/resume, cancel, delete,
    # dependency editing (the `validate_dependencies` gate) and queue
    # reordering. The semantics live HERE — the widgets only route to
    # these methods — so a test drives the exact production path
    # headless, exactly like the plan-task-4 seam above.
    #
    # Shell convention (carried over from submit_user_text): the state
    # transition ALWAYS happens; the `_advance()` that may START A RUN is
    # gated on a configured model (without one the queue stays parked —
    # the task-1 startup rule), while the tester/review halves of the
    # gate stay model-independent. TUI parity per command is noted in the
    # docstrings; every refusal is a notify, never an exception.

    @staticmethod
    def can_edit_dependencies(task: Task | None) -> bool:
        """Dependencies only make sense before development starts: queued
        and held tasks — the same statuses the TUI's kind cycle and dep
        palette accept (``actions_lifecycle._can_edit_dependencies``)."""
        return task is not None and task.status in (
            TaskStatus.QUEUED,
            TaskStatus.HOLD,
        )

    def create_task(
        self,
        text: str,
        *,
        kind: TaskKind | None = None,
        hold: bool = False,
        depends_on: list[str] | None = None,
    ) -> Task | None:
        """Create a task from the new-task form (the fresh-creation half
        of ``tui.actions_forms._submit_new_form``): the text becomes the
        condition (the title derives from its first line), `hold` parks
        the task right away (the editor's "create on hold"), `kind`
        defaults to the shell's last-used kind (the TUI form's
        ``new_task_kind``).

        The dependencies are validated BEFORE anything is created (the
        TUI form's rule: an invalid list keeps the form open with the
        error shown, nothing is stored) — via the same
        ``validate_dependencies`` gate the manager itself applies on
        write; a violation surfaces as a notify and a ``None`` return.
        The created task is opened in the chat pane and the pipeline is
        advanced (it starts immediately unless testing is pending, the
        queue is busy — or the task was created on hold).

        Returns the new task, or None when nothing was created (empty
        text or a dependency violation)."""
        stripped = text.strip()
        if not stripped:
            self._hub.emit_notify("Task text is empty.", "New task", "warning")
            return None
        # Creation-time validation passes task_id=None (self-reference and
        # cycles are impossible for a task that does not exist yet) — the
        # same call the TUI form makes; the manager re-validates on write.
        try:
            deps = validate_dependencies(self.manager.tasks, None, depends_on or [])
        except ValueError as e:
            self._hub.emit_notify(str(e), "New task", "warning")
            return None
        task = self.manager.create(
            "",
            stripped,
            kind=kind if kind is not None else self.new_task_kind,
            hold=hold,
        )
        if deps:
            self.manager.set_dependencies(task.id, deps)
        self.refresh_sidebar()
        self.refresh_status()
        if not hold:
            # The TUI opens the fresh task in the chat pane (a held one
            # stays where the user was — it is parked on purpose).
            self.view_task(task.id)
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()
        return task

    def hold_task(self, task_id: str) -> bool:
        """Park a task out of the backlog (Ctrl+P in the TUI): queued
        tasks and tasks awaiting a clarifying answer (the question is
        preserved and shown again on resume). Returns False (with a
        notify) for every other status."""
        task = self.manager.tasks.get(task_id)
        if task is None:
            self._hub.emit_notify(f"Task #{task_id} not found.", "Hold task", "warning")
            return False
        if task.status not in (
            TaskStatus.QUEUED,
            TaskStatus.AWAITING_CLARIFICATION,
        ):
            self._hub.emit_notify(
                "Only a queued task (or one awaiting clarification) can be "
                "put on hold.",
                "Hold task",
                "warning",
            )
            return False
        self.manager.set_hold(task.id, on=True)
        self.refresh_sidebar()
        self.refresh_status()
        if self.viewed_id == task.id:
            self.refresh_task_status()
        return True

    def resume_task(self, task_id: str) -> bool:
        """Return a held task to where it was (queued, or awaiting its
        clarification; an answer typed while held re-queues it at the
        head — the manager's rule). The pipeline is advanced: a resumed
        task may be picked up right away."""
        task = self.manager.tasks.get(task_id)
        if task is None:
            self._hub.emit_notify(
                f"Task #{task_id} not found.", "Resume task", "warning"
            )
            return False
        if task.status is not TaskStatus.HOLD:
            self._hub.emit_notify(
                "Only a held task can be resumed.", "Resume task", "warning"
            )
            return False
        self.manager.set_hold(task.id, on=False)
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()
        self.refresh_sidebar()
        self.refresh_status()
        if self.viewed_id == task.id:
            self.refresh_task_status()
        return True

    def cancel_task(self, task_id: str) -> bool:
        """User cancels the task (``actions_lifecycle.action_cancel_task``):
        its runs are stopped, all queued input is dropped and the task
        completes immediately — the tester never runs, the worktree
        environment is torn down by the removal listeners.

        Refused for a COMPLETED task (nothing left to cancel). The
        cancelled run's in-flight prompt is flushed into the transcript
        first, exactly like in the TUI — a brief consumed by a run that
        had no node boundary yet must not vanish from the history."""
        task = self.manager.tasks.get(task_id)
        if task is None:
            self._hub.emit_notify(
                f"Task #{task_id} not found.", "Cancel task", "warning"
            )
            return False
        if task.status is TaskStatus.COMPLETED:
            self._hub.emit_notify(
                "The task is already completed — cancel is for unfinished work.",
                "Cancel task",
                "warning",
            )
            return False
        stopped_run = False
        # Stop only this task's agent run (parallel slots: the other
        # tasks' runs are independent and keep going).
        if task.id in self._agent_runs:
            self._host.cancel_worker_group(f"agent-{task.id}")
            self._agent_runs.pop(task.id, None)
            self._current_tool[task.id] = None
            stopped_run = True
        if self.tester_running and self._tester_task_id == task.id:
            self._host.cancel_worker_group("tester")
            self.tester_running = False
            self._current_tool[task.id] = None
            stopped_run = True
        had_pending = bool(task.pending_input)
        self._flush_in_flight_prompt(task)
        self.manager.cancel(task.id)
        # The task was possibly in TESTING: drop its tester pipeline
        # state — with a live tester run this is done by the cancelled
        # worker's `finally`, without one the stale backoff/retry entries
        # and the stale `_tester_task_id` would linger until restart.
        self._tester_resume_round.discard(task.id)
        self._tester_compaction_resumed.discard(task.id)
        self._tester_compaction_verdict.discard(task.id)
        self._tester_fail_count.pop(task.id, None)
        self._tester_retry_at.pop(task.id, None)
        self._tester_net_count.pop(task.id, None)
        if self._tester_task_id == task.id and not self.tester_running:
            self._tester_task_id = None
        if stopped_run:
            self._clear_stream_label()
        self._show_event(
            task,
            "Task cancelled by the user — removed from the pipeline without testing.",
        )
        self.refresh_sidebar()
        self.refresh_status()
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()
        del had_pending  # the chat pane rebuilds from the transcript
        return True

    def delete_task(self, task_id: str) -> bool:
        """Fully remove the task — from the shell and from the project
        storage (``.drcode/tasks/<id>.json``).

        GUI rule (this task's spec): only a HELD task is deletable — a
        parked task has nothing running for it, so no worker state is
        left behind. Everything else (QUEUED/ACTIVE/TESTING/…/DRAFT/
        COMPLETED) is refused with a notify."""
        task = self.manager.tasks.get(task_id)
        if task is None:
            self._hub.emit_notify(
                f"Task #{task_id} not found.", "Delete task", "warning"
            )
            return False
        if task.status is not TaskStatus.HOLD:
            self._hub.emit_notify(
                "Only a held task can be deleted — put the task on hold "
                "first (queued, active and completed tasks are kept).",
                "Delete task",
                "warning",
            )
            return False
        was_viewed = self.viewed_id == task.id
        self.manager.delete(task.id)
        if was_viewed:
            # The chat pane showed the deleted task: drop the selection
            # (and any pending render bookkeeping — its "user-pending"
            # lines must never leak into another task's transcript).
            self.viewed_id = None
            self._host.current_task_id = ""
            self._chat_has_content = False
            self._pending_rendered = []
            self._chat_epoch += 1
            self._rendered_clarification = None
        self._hub.emit_task_removed(task_id)
        self.refresh_sidebar()
        self.refresh_status()
        return True

    def add_dependency(self, task_id: str, dep_id: str) -> bool:
        """Make the task wait for `dep_id` (COMPLETED + merged) before it
        can be taken into development (``action_add_dependency``). The
        `validate_dependencies` gate runs inside the manager: an unknown
        id, a self-reference or a cycle raise, and the message is
        surfaced as a notify with the deps left untouched."""
        task = self.manager.tasks.get(task_id)
        if not self.can_edit_dependencies(task) or task is None:
            self._hub.emit_notify(
                "Dependencies are editable for queued and held tasks only.",
                "Dependencies",
                "warning",
            )
            return False
        dep = self._normalize_dep_id(dep_id)
        if not dep:
            self._hub.emit_notify("Dependency id is empty.", "Dependencies", "warning")
            return False
        try:
            self.manager.set_dependencies(task.id, [*task.depends_on, dep])
        except ValueError as e:
            self._hub.emit_notify(str(e), "Dependencies", "warning")
            return False
        self._show_event(task, f"Dependency #{dep} added — task waits for it.")
        self.refresh_sidebar()
        self.refresh_status()
        if self.viewed_id == task.id:
            self.refresh_task_status()
        return True

    def remove_dependency(self, task_id: str, dep_id: str) -> bool:
        """Drop one dependency from the task (``action_remove_dependency``).
        Doubles as the force-start: removing the blocking dependency makes
        the task eligible immediately (the gate is dynamic — nothing else
        to reset), the manual override is logged into the task's service
        events and the pipeline is advanced."""
        task = self.manager.tasks.get(task_id)
        if not self.can_edit_dependencies(task) or task is None:
            self._hub.emit_notify(
                "Dependencies are editable for queued and held tasks only.",
                "Dependencies",
                "warning",
            )
            return False
        dep = self._normalize_dep_id(dep_id)
        if dep not in task.depends_on:
            return False
        self.manager.set_dependencies(task.id, [d for d in task.depends_on if d != dep])
        self._dep_block_notified.discard(task.id)
        self._dep_gate.pop(task.id, None)
        self._show_event(
            task,
            f"Manual override: dependency #{dep} removed — "
            "task eligible for development.",
        )
        self.refresh_sidebar()
        self.refresh_status()
        if self.viewed_id == task.id:
            self.refresh_task_status()
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()
        return True

    def move_task_in_queue(self, task_id: str, *, up: bool) -> bool:
        """Move a queued task one step in the backlog (the TUI's
        Ctrl+Up/Ctrl+Down): ``up`` moves it away from execution, down —
        closer. The manager refuses moves that cross the bug block (bug
        reports always run first) or leave the queue; a refusal is a
        quiet False (the view disables the actions at the edges)."""
        task = self.manager.tasks.get(task_id)
        if task is None or task.status is not TaskStatus.QUEUED:
            return False
        if self.manager.move_in_queue(task.id, up=up) is None:
            return False
        self.refresh_sidebar()
        self.refresh_status()
        if self.viewed_id == task.id:
            self.refresh_task_status()
        return True

    @staticmethod
    def _normalize_dep_id(raw: str) -> str:
        """The dependency field's id normalisation: surrounding
        whitespace off, a leading ``#`` (the mention-link form) off."""
        return raw.strip().lstrip("#")

    # -- settings / model-selection seams (plan task 6) -----------------------------

    def apply_task_settings(self, draft: ProjectSettings) -> None:
        """Apply an accepted project-settings draft ATOMICALLY — the GUI
        twin of ``tui.actions_settings.action_apply_task_settings``: the
        draft is merged into the LIVE settings object in place
        (TaskEnvironment holds the same instance), persisted once, and the
        same side effects as the TUI's per-field actions are replayed
        (pipeline gate + keep-awake re-evaluation)."""
        from .settings_dialog import copy_task_settings

        copy_task_settings(self.settings, draft)
        self.settings_store.save(self.settings)
        self._hub.emit_notify(
            "Task settings applied", "Project settings", "information"
        )
        self._advance()
        self._update_keepawake()

    def apply_notifications_settings(self, enabled: bool, topic: str) -> None:
        """The Save of the notifications dialog (the GUI port of
        ``tui.actions_settings._notifications_settings_done``): persist
        the enable/topic pair into the project settings (the same file
        the TUI writes) and fire the test push so the user can check the
        subscription right away."""
        self.settings.notifications_enabled = enabled
        self.settings.ntfy_topic = topic
        self.settings_store.save(self.settings)
        self._hub.emit_notify(
            f"Notifications: {'on' if enabled else 'off'}"
            + (f" (ntfy.sh/{topic})" if enabled and topic else ""),
            "Project settings",
            "information",
        )
        if enabled and topic:
            self.notifier.notify(
                "DrCode: notifications enabled",
                f'Project "{self.project_root.name}": test '
                "notification — the channel works.",
                tags=["bell"],
            )

    def apply_model_settings(
        self,
        slots: dict[AgentRole, ModelRef | str | None],
        customs: dict[AgentRole, CustomModelEntry],
        role_max_tokens: dict[AgentRole, int],
    ) -> None:
        """Persist the per-role model selection and max_tokens overrides —
        the GUI twin of ``tui.actions_models._project_model_settings_done``:
        the same three config layers, in the same order and with the same
        merge semantics as the TUI (so both front-ends stay interchangeable
        on one project):

        - new/changed custom models first (``set_project_custom_model``
          stores the entry AND activates it — ``models[role] = "custom"``);
        - the remaining slots (a catalog reference, a re-activation of a
          saved custom model, or None = "User default"), skipping the ones
          already written by the custom-model pass or left untouched;
        - the ``role_max_tokens`` overrides merged against the STORED
          settings file (the live object may predate an external edit).

        Then the per-role resolution cache is dropped and the pipeline
        gate re-run — a newly configured model starts the parked queue.
        """
        for role, entry in customs.items():
            set_project_custom_model(self.project_root, role, entry)
        current_slots = load_project_model_slots(self.project_root)
        for role, slot in slots.items():
            if role in customs or slot == current_slots.get(role):
                continue  # already activated / untouched
            if isinstance(slot, ModelRef):
                set_project_model(self.project_root, role, slot)
            elif slot == "custom":
                set_project_custom_active(self.project_root, role)
            else:
                set_project_model(self.project_root, role, None)
        stored = dict(self.settings_store.load().role_max_tokens)
        merged = dict(stored)
        for role in AgentRole:
            merged.pop(role.value, None)
        for role, value in role_max_tokens.items():
            merged[role.value] = value
        self.settings.role_max_tokens = merged
        if merged != stored:
            self.settings_store.save(self.settings)
        self.invalidate_resolved_configs()
        self.refresh_status()
        self._advance()
        overrides = (
            ", ".join(
                f"{role.value}={value}"
                for role, value in sorted(
                    role_max_tokens.items(), key=lambda kv: kv[0].value
                )
            )
            or "no max_tokens overrides"
        )
        self._hub.emit_notify(
            f"Project model settings applied ({overrides})",
            "Project model settings",
            "information",
        )

    # -- worktree listeners (adapted from tui/app.py) -----------------------------

    def _remove_worktree_background(self, task_id: str) -> None:
        self.task_env.schedule_removal(task_id, self.manager.tasks.get(task_id))

    def _completion_removal_background(
        self, task: Task, old: TaskStatus, origin: str
    ) -> None:
        del old, origin
        if self.settings.worktree_removal is not WorktreeRemoval.ON_COMPLETION:
            return
        self.task_env.schedule_removal(task.id, task)

    def _snapshot_on_settled(
        self, task: Task, old: TaskStatus, new: TaskStatus, origin: str
    ) -> None:
        if new is not TaskStatus.COMPLETED or old not in (
            TaskStatus.ACTIVE,
            TaskStatus.TESTING,
        ):
            return
        if not self.task_env.active:
            return
        if self.settings.worktree_removal is WorktreeRemoval.ON_COMPLETION:
            # The completion removal snapshots the tree itself first.
            return
        if origin == "user":
            note = "cancelled"
        elif task.verification is not None:
            note = f"tester verdict: {task.verification.value}"
        else:
            note = "completed without verification"
        self._snapshot_in_background(task.id, note)

    def _snapshot_in_background(self, task_id: str, note: str) -> None:
        """Run task_env.snapshot off the loop thread and log the result."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            threading.Thread(
                target=self._snapshot_thread_job,
                args=(task_id, note),
                name="drcode-snapshot",
                daemon=True,
            ).start()
        else:
            job = loop.create_task(self._snapshot_async_job(task_id, note))
            self._snapshot_jobs.add(job)
            job.add_done_callback(self._snapshot_jobs.discard)

    async def _snapshot_async_job(self, task_id: str, note: str) -> None:
        msg = await asyncio.to_thread(self.task_env.snapshot, task_id, note)
        if msg is not None:
            self._task_env_event(task_id, msg)

    def _snapshot_thread_job(self, task_id: str, note: str) -> None:
        msg = self.task_env.snapshot(task_id, note)
        if msg is not None:
            self._task_env_event(task_id, msg)

    def _task_env_event(self, task_id: str, text: str) -> None:
        """Service-event sink for the worktree environment."""
        task = self.manager.tasks.get(task_id)
        if task is not None:
            self._hub.emit_service_event(task_id, "worktree", text)
            # Persist even when the caller sits on a daemon thread (a
            # snapshot job): the storage write is contract-critical,
            # the live-chat half of _show_event is not.
            self._log_event(task, text, kind="worktree")
            return
        self._hub.emit_notify(text, "Worktree cleanup", "information")

    def _notify_error(self, text: str) -> None:
        """Delivery failures of the notifications module (a daemon
        thread) — surface as a notify event, never raise."""
        self._hub.emit_notify(
            f"Failed to send notification: {text}", "Notifications", "warning"
        )

    # -- external task pickup (the sidebar poll's logic, event-sinked) -------------

    def _poll_new_tasks(self) -> None:
        """Watch `.drcode/tasks/` for tasks created by other processes
        while the shell is running (parallel TUI/GUI on one project)."""
        try:
            new = self.manager.reload_new_tasks()
        except Exception:
            return
        if not new:
            return
        for task in new:
            self._show_event(
                task,
                "Task created outside this session "
                "(e.g. `drcode --feature-request`) — loaded from the "
                "project directory.",
            )
        self.refresh_sidebar()
        self.refresh_status()
        if self.resolved_config(AgentRole.CODING).is_ready:
            self._advance()

    # -- voice (not wired in the shell) ---------------------------------------------

    @property
    def voice_dictation_configured(self) -> bool:
        return False
