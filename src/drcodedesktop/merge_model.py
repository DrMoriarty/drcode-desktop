"""The headless model of the human conflict-resolution flow (plan task 9).

The GUI port of the TUI's ``tui.merge_screen`` / ``tui.unmerged_screen``
brains, split off the widgets so the acceptance tests can drive the whole
flow without opening a window. Deliberately UI-free and synchronous (the
TUI screens run these exact calls in worker threads; the Qt dialogs do
the same — see :mod:`drcodedesktop.merge_screen`).

Composition of the three layers underneath (all reused as-is, nothing
re-implemented):

- ``drcode.merge_engine`` — the reversed merge, the triage report, the
  ``finish``/``abort`` conclusions;
- ``drcode.conflict.ConflictWorktree`` — the parked conflict as a live
  object: zdiff3 hunk parsing, the ours/theirs/both decisions, the
  stage-only-when-marker-free rule, whole-file side decisions for the
  markerless kinds;
- ``drcode.merge_resolver`` — the LLM-free *halves* only: the prepare
  step (``prepare`` = the engine's reversed merge + triage +
  auto-resolvers) and the verification funnel (``mechanical_checks``,
  ``run_test_suite``, ``review_diffs``). The LLM resolver itself stays
  in the core and is NOT ported — the GUI only shows its verdicts, same
  as the TUI does.

The contract holds unchanged: every decision lands in the task's ISOLATED
worktree and is committed on the task's own branch
(:meth:`MergeModel.commit` → ``merge_engine.finish``); the only merge
that touches the user's checkout is :meth:`MergeModel.ff_merge` — the
explicit, ``--ff-only`` fast-forward behind the base-is-an-ancestor
re-verification, exactly the TUI's ``f`` action.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from drcode import merge_engine, merge_resolver
from drcode.conflict import (
    BOTH,
    CHOICES,
    OURS,
    THEIRS,
    ConflictWorktree,
    DiffStat,
    FileState,
    Hunk,
    parse_hunks,
)
from drcode.merge_engine import MergeReport
from drcode.merge_resolver import CheckResult
from drcode.models import Task

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "BOTH",
    "CHOICES",
    "MODE_CONFLICT",
    "MODE_FF",
    "MODE_IDLE",
    "MODE_STAGED",
    "MODE_UNMERGED",
    "OURS",
    "THEIRS",
    "MergeEnv",
    "MergeModel",
    "UnmergedEntry",
    "collect_unmerged_entries",
    "parse_hunks",
]

# The screen's modes (one per phase of the parked merge's life).
MODE_IDLE = "idle"  # nothing parked, nothing unmerged
MODE_UNMERGED = "unmerged"  # unmerged branch, no merge deployed yet — prepare
MODE_CONFLICT = "conflict"  # the reversed merge is parked with conflicts
MODE_STAGED = "staged"  # resolved and staged, not committed yet
MODE_FF = "ff-ready"  # nothing left to resolve — the ff into the base is real

MODE_LABELS = {
    MODE_IDLE: "nothing to merge",
    MODE_UNMERGED: "unmerged — prepare the merge first",
    MODE_CONFLICT: "conflict — resolve the hunks below",
    MODE_STAGED: "resolved and staged — commit to conclude",
    MODE_FF: "ready — merge into the base (ff-only)",
}


class MergeEnv(Protocol):
    """The slice of ``drcode.task_env.TaskEnvironment`` the model needs
    (CoreShell satisfies it; tests can hand a stand-in)."""

    @property
    def active(self) -> bool: ...

    def worktree_path(self, task_id: str) -> Path: ...

    def branch(self, task_id: str) -> str: ...

    def base_branch(self) -> str: ...

    def ff_merge_branch(self, task_id: str) -> tuple[bool, str]: ...

    def clear_identical_untracked_for_ff(
        self, task_id: str
    ) -> tuple[list[str], list[str]]: ...

    def drcode_branch_state(self) -> tuple[set[str], set[str]]: ...


class MergeModel:
    """One task's parked merge, as the merge screen sees it.

    Every method is sync and blocking (git + file IO); the dialogs run
    them off the GUI thread, the tests call them directly. State is
    re-read on :meth:`reload` / :meth:`refresh` — the triage report is
    the static picture at merge time, the worktree is the live one.
    """

    def __init__(
        self,
        project_root: Path,
        task_id: str,
        env: MergeEnv,
        tasks: Mapping[str, Task] | None = None,
        save_task: Callable[[Task], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.task_id = task_id
        self.env = env
        self.tasks: Mapping[str, Task] = tasks if tasks is not None else {}
        self.branch = env.branch(task_id) if env.active else f"drcode/{task_id}"
        self.base_branch = env.base_branch() if env.active else ""
        self.worktree_path = env.worktree_path(task_id)
        self.task: Task | None = self.tasks.get(task_id)
        self._save_task = save_task
        self._cw: ConflictWorktree | None = None
        self.report: MergeReport | None = None
        self.reload()

    @classmethod
    def for_task(cls, shell: Any, task_id: str) -> MergeModel:
        """The production constructor: a model over a live CoreShell
        (duck-typed — only the env/task slices are touched)."""
        return cls(
            shell.project_root,
            task_id,
            shell.task_env,
            shell.manager.tasks,
            shell.manager.storage.save,
        )

    # -- state -------------------------------------------------------------------

    def reload(self) -> None:
        """A fresh :class:`ConflictWorktree` (and with it the persisted
        triage report) — the entry point after prepare/commit/abort."""
        self._cw = None
        self.report = None
        if not self.env.active or not self.worktree_path.is_dir():
            return
        self._cw = ConflictWorktree(self.project_root, self.task_id, self.worktree_path)
        self.report = self._cw.report

    def refresh(self) -> None:
        """Re-triage the live merge in place (cheap — an external edit or
        a resolver round landing while the screen is open)."""
        if self._cw is not None:
            self._cw.refresh()
            self.report = self._cw.report

    @property
    def cw(self) -> ConflictWorktree | None:
        return self._cw

    def in_progress(self) -> bool:
        return self._cw is not None and self._cw.in_progress()

    def mode(self) -> str:
        """The screen mode for the CURRENT git state (never cached)."""
        if not self.env.active or not self.worktree_path.is_dir():
            return MODE_IDLE
        if self.in_progress():
            if merge_engine.unmerged_paths(self.worktree_path):
                return MODE_CONFLICT
            return MODE_STAGED
        report = self.report
        if report is not None and report.ok and not report.conflicted:
            if report.fast_forward is True:
                return MODE_FF
            if report.fast_forward is None:
                # Resolved and staged but never concluded (the engine's
                # resumed-merge semantics) — the commit is the next step.
                return MODE_STAGED
            return MODE_IDLE  # up to date / nothing beyond the base
        if merge_engine.is_fast_forward(
            self.project_root, self.branch, self.base_branch
        ):
            # A plain unmerged branch (no conflicts parked): the ff is
            # real — but only via the explicit user action, as everywhere.
            return MODE_FF
        return MODE_UNMERGED

    def file_states(self) -> list[FileState]:
        if self._cw is None:
            return []
        return self._cw.file_states()

    # -- content / hunks -----------------------------------------------------------

    def read_file(self, path: str) -> str | None:
        if self._cw is None:
            return None
        return self._cw.read_file(path)

    def hunks(self, path: str) -> list[Hunk]:
        """The zdiff3 hunks of the path's current working-tree content."""
        text = self.read_file(path)
        if text is None:
            return []
        return parse_hunks(text)

    def apply_hunk(self, path: str, hunk: Hunk, choice: str) -> tuple[bool, str]:
        """One hunk decision (ours/theirs/both); the file is staged only
        when no markers remain (the ConflictWorktree rule)."""
        if self._cw is None:
            return False, "no merge is parked for this task"
        return self._cw.apply_hunk(path, hunk, choice)

    def take_side(self, path: str, choice: str) -> tuple[bool, str]:
        """A whole-file ours/theirs for the markerless kinds
        (modify-delete, rename family) — materialized from the triage
        report's stage blobs."""
        if self._cw is None:
            return False, "no merge is parked for this task"
        return self._cw.take_side(path, choice)

    def stage_clean(self, path: str) -> tuple[bool, str]:
        """Stage a path the user resolved outside; refuses while markers
        remain."""
        if self._cw is None:
            return False, "no merge is parked for this task"
        return self._cw.stage_clean(path)

    def diff_stat(self, *args: str, cwd: Path | None = None) -> list[DiffStat]:
        """`git diff --numstat <args>` rows (the ff-ready / staged diff
        lists and the review summary)."""
        if self._cw is None:
            return []
        return self._cw.numstat(*args, cwd=cwd)

    # -- triage report ----------------------------------------------------------------

    def triage_text(self) -> str:
        """The human-readable triage picture for the report panel: the
        report's verdict line, the conflict kinds, the auto-resolvers
        that already cleared paths and the rerere note."""
        report = self.report
        if report is None:
            return "no triage report"
        lines = [report.summary()]
        if report.rerere:
            lines.append(f"rerere: {report.rerere}")
        if report.files:
            lines.append("")
            resolved = {f.path for f in report.resolved}
            for f in report.files:
                marks = []
                if f.path in resolved:
                    marks.append(f"auto-resolved ({f.resolution})")
                marks.append(f.kind)
                detail = f"hunks: {f.hunks}" if f.hunks else ""
                lines.append(
                    f"  {f.path} — {'/'.join(marks)}"
                    + (f" ({detail})" if detail else "")
                )
        return "\n".join(lines)

    # -- verification funnel (the LLM-free levels of the resolver's stack) -----------

    def conflicted_paths(self) -> list[str]:
        """The paths the marker scan should cover: the report's remaining
        set, else the live unmerged index entries."""
        report = self.report
        remaining = [f.path for f in report.remaining] if report else []
        return remaining or merge_engine.unmerged_paths(self.worktree_path)

    def run_verification(self) -> tuple[list[CheckResult], CheckResult]:
        """The funnel: conflict markers → compile → ruff → pyright →
        pytest, all inside the task's worktree with the project's own
        environment. The report is persisted next to the triage report
        (``<task-id>-verify.txt``) so the details survive the screen."""
        checks = merge_resolver.mechanical_checks(
            self.worktree_path, self.conflicted_paths()
        )
        tests = merge_resolver.run_test_suite(self.worktree_path)
        merge_resolver.save_verify_report(
            self.project_root, self.task_id, checks, tests
        )
        return checks, tests

    def review_diffs(self) -> tuple[str, str]:
        """The level-3 payloads: diff(resolved, ours) and diff(resolved,
        theirs) over the conflicted paths — the "what did the decision
        change against either side" picture."""
        return merge_resolver.review_diffs(self.worktree_path, self.conflicted_paths())

    # -- prepare / conclusion ------------------------------------------------------

    def prepare(self) -> MergeReport:
        """Deploy (or re-detect) the reversed merge: the engine's merge +
        triage + safe auto-resolvers. The entry for the MODE_UNMERGED
        state (and for re-running after a refused ff)."""
        if not self.env.active:
            return MergeReport(
                task_id=self.task_id,
                branch=self.branch,
                base_branch=self.base_branch,
                worktree=str(self.worktree_path),
                ok=False,
                message="worktree isolation is off",
            )
        report = merge_resolver.prepare(
            self.project_root,
            self.task_id,
            self.worktree_path,
            self.base_branch,
        )
        self.reload()
        return report

    def commit(self, message: str) -> tuple[bool, str]:
        """Commit the resolved merge on the task's branch
        (``merge_engine.finish``) — refuses while unmerged paths remain."""
        if self._cw is None:
            return False, "no merge is parked for this task"
        ok, out = self._cw.commit(message)
        self.reload()
        return ok, out

    def abort(self) -> tuple[bool, str]:
        """Roll the worktree back to the pre-merge state."""
        if self._cw is None:
            return False, "no merge is parked for this task"
        ok, out = self._cw.abort()
        self.reload()
        return ok, out

    def ff_merge(self) -> tuple[bool, str]:
        """The contract's user-triggered half: fast-forward the MAIN
        checkout's base branch to ``drcode/<id>`` — ``--ff-only``, behind
        the base-is-an-ancestor re-verification (the base may have moved
        since the screen opened), with the identical-untracked pre-clean.
        A refusal never conflicts: the caller re-runs the reversed merge
        (prepare) instead."""
        if not self.env.active:
            return False, "worktree isolation is off"
        if not merge_engine.is_fast_forward(
            self.project_root, self.branch, self.base_branch
        ):
            return False, (
                f"{self.base_branch} is no longer an ancestor of "
                f"{self.branch} (the base moved ahead) — prepare the "
                "merge again"
            )
        _removed, differing = self.env.clear_identical_untracked_for_ff(self.task_id)
        if differing:
            return False, (
                "untracked file(s) differ from the branch version — move "
                f"or remove them manually: {', '.join(differing)}"
            )
        ok, out = self.env.ff_merge_branch(self.task_id)
        if ok:
            # The parked conflict is gone with the merge — clear the ✋
            # flag the same way the TUI's f action does.
            self.clear_needs_human()
        return ok, out

    def clear_needs_human(self) -> None:
        """Drop the needs_human flag (after a successful commit or ff)
        and persist the task when a save hook is wired."""
        task = self.task
        if task is not None and task.needs_human:
            task.needs_human = False
            task.touch()
            if self._save_task is not None:
                self._save_task(task)


# -- the unmerged-branches catalog (tui.unmerged_screen's model half) -------------


@dataclass(frozen=True)
class UnmergedEntry:
    """One row of the unmerged-branches list: the task, its branch and
    the merge state the actions dispatch on (``kind``: conflict / ff /
    staged / unmerged — the TUI screen's exact vocabulary)."""

    task_id: str
    title: str
    branch: str
    base: str
    kind: str  # "conflict" | "ff" | "staged" | "unmerged"

    @property
    def state_label(self) -> str:
        if self.kind == "conflict":
            return "conflict — needs resolution"
        if self.kind == "ff":
            return f"ready — merge into {self.base} (ff-only)"
        if self.kind == "staged":
            return "resolved, not committed — commit it"
        return f"unmerged — merge into {self.base} not done"


def _collect_entry(
    project_root: Path,
    env: MergeEnv,
    tasks: Mapping[str, Task],
    task_id: str,
) -> UnmergedEntry | None:
    """One task's merge state, fresh from git (sync, blocking). None =
    nothing to merge anymore (no branch, already merged, branch holds
    nothing beyond the base) — the row is dropped, like the TUI's."""
    if not env.active:
        return None
    task = tasks.get(task_id)
    if task is None:
        return None
    branch = env.branch(task_id)
    base = env.base_branch()
    existing, unmerged = env.drcode_branch_state()
    if branch not in existing or branch not in unmerged:
        return None
    worktree = env.worktree_path(task_id)
    report = merge_engine.load_report(project_root, task_id)
    if task.needs_human or (worktree.is_dir() and merge_engine.in_progress(worktree)):
        kind = "conflict"
    elif report is not None and report.ok and not report.conflicted:
        if report.fast_forward is True:
            kind = "ff"
        elif report.fast_forward is None:
            kind = "staged"
        else:
            return None  # up to date — nothing beyond the base
    elif merge_engine.is_fast_forward(project_root, branch, base):
        kind = "ff"
    else:
        kind = "unmerged"
    return UnmergedEntry(
        task_id=task_id,
        title=task.title_display,
        branch=branch,
        base=base,
        kind=kind,
    )


def collect_unmerged_entries(
    project_root: Path,
    env: MergeEnv,
    tasks: Mapping[str, Task],
    task_ids: Sequence[str] | None = None,
) -> list[UnmergedEntry]:
    """Every candidate row for the unmerged-branches dialog: by default
    all tasks with the ✋ flag first, then every task that still has
    unmerged branch work (the TUI's candidate order)."""
    if task_ids is None:
        ordered = [t.id for t in tasks.values() if t.needs_human]
        ordered.extend(t.id for t in tasks.values() if not t.needs_human)
        task_ids = ordered
    entries: list[UnmergedEntry] = []
    for task_id in dict.fromkeys(task_ids):
        entry = _collect_entry(project_root, env, tasks, task_id)
        if entry is not None:
            entries.append(entry)
    return entries
