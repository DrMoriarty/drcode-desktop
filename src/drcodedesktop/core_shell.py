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
from pathlib import Path
from typing import Any

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
    migrate_project_config,
    migrate_user_config,
    project_has_model_selection,
)
from drcode.settings import (
    ProjectSettings,
    ProjectSettingsStore,
    WorktreeRemoval,
)
from drcode.task_env import TaskEnvironment
from drcode.task_manager import TaskManager
from drcode.tools import cleanup_orphan_scratches
from drcode.usage_store import UsageStore, usage_file
from drcode.workflow import load_hooks
from PySide6.QtCore import QTimer

from .event_hub import EventHub
from .qt_host import QtHost


class CoreShell(PipelineLayer):
    """One live drcode core, driven headless through the Qt host."""

    def __init__(self, project_root: Path, host: QtHost, hub: EventHub) -> None:
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
        # ntfy.sh notifications (no TTS speaker in the shell yet).
        self.notifier = Notifier(
            lambda: self.settings,
            project=project_root.name,
            project_root=project_root,
            on_error=self._notify_error,
        )
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
        self._tps_samples: dict[str, deque[tuple[float, int]]] = {}
        self._conn_label = "○ not probed (skeleton)"
        self._conn_role: AgentRole | None = None
        self._last_probe_ms: int | None = None
        self._probe_fail_streak = 0
        self._tester_generation = 0
        # Context fill / compaction bookkeeping.
        self._live_ctx_tokens: dict[str, int] = {}
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
        """Headless chat write: a StreamBlock accumulates silently; any
        other content is logged verbatim into the session log."""
        del expand, note
        text = getattr(content, "get_content", None)
        if callable(text):
            self._hub.append_log(f"chat [{getattr(content, 'kind', '?')}]")
        else:
            self._hub.append_log(f"chat {content!r}")

    def _write_history_line(self, role: str, text: str, at: Any = None) -> Any:
        del at
        self._hub.emit_service_event(self.viewed_id or "", role, text)
        return None

    def _write_clarification(self, clarification: Clarification) -> None:
        self._hub.emit_service_event(
            self.viewed_id or "",
            "clarification",
            "; ".join(q.question for q in clarification.questions),
        )

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
        del run_key, block
        self._touch_activity(task_id, call_text)
        self._hub.emit_service_event(task_id, "tool", call_text)

    def _tool_call_finished(self, call_id: str) -> None:
        self._hub.append_log(f"tool finished {call_id}")

    def _stop_run_tools(self, run_key: tuple[str, str, int]) -> None:
        del run_key

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
        self.viewed_id = task_id
        self._hub.emit_task_updated(task_id, self.manager.tasks[task_id].status.value)

    def _suggest_conflict_screen(self, task: Task) -> None:
        self._show_event(task, "Merge conflicts parked — open the merge screen.")

    def _unmerged_completed_ids(self) -> list[str]:
        # Full git-based parity is task-7 work; the skeleton reports
        # nothing unmerged (this only gates a reminder message).
        return []

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
