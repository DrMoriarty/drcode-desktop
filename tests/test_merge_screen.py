"""Plan task 9 acceptance tests: the human conflict-resolution flow in
the GUI.

Everything is driven through the production seam the widgets route to —
the headless :class:`~drcodedesktop.merge_model.MergeModel` (the port of
the TUI ConflictScreen's brain) and the unmerged-branches catalog — over
real throwaway git repositories, exactly like the engine's own tests
(``drcode/tests/test_merge_engine.py``):

- zdiff3 hunk parsing (ours / ancestor / theirs sections, labels,
  unterminated garbage);
- hunk decisions (ours/theirs/both) with the marker-gated staging rule:
  a file is staged ONLY when no conflict markers remain in it;
- finish/abort through the GUI model: the commit lands on the task's own
  branch (the engine's ``finish``), the abort rolls the worktree back —
  the user's checkout is touched only by the explicit ``--ff-only``
  ff-merge (the contract);
- the unmerged-branches catalog (the UnmergedDialog's rows), including
  the real ``TaskEnvironment`` integration (the shell's env satisfies
  the model's protocol — that is what the dialogs hand it).
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drcode import merge_engine, merge_resolver
from drcode.conflict import BOTH, OURS, THEIRS, parse_hunks
from drcode.settings import TaskIsolation

from drcodedesktop.merge_model import (
    MODE_CONFLICT,
    MODE_FF,
    MODE_IDLE,
    MODE_STAGED,
    MODE_UNMERGED,
    MergeModel,
    collect_unmerged_entries,
)
from tests.test_task_management import park_queue
from tests.test_user_input import run_shell_scenario

# -- git helpers (the drcode/tests/test_merge_engine pattern) --------------------


def _run(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if check:
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc


def _commit(root: Path, file: str, content: str, message: str) -> None:
    (root / file).write_text(content, encoding="utf-8")
    _run(root, "add", file)
    _run(root, "commit", "-qm", message)


def _read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


@dataclass
class FakeEnv:
    """The model's env slice over a throwaway repo (the same five calls
    the real ``TaskEnvironment`` answers — the dialogs pass the shell's
    env; the model tests need nothing more)."""

    root: Path

    @property
    def active(self) -> bool:
        return True

    def worktree_path(self, task_id: str) -> Path:
        return self.root / ".drcode" / "worktrees" / task_id

    def branch(self, task_id: str) -> str:
        return f"drcode/{task_id}"

    def base_branch(self) -> str:
        return "main"

    def ff_merge_branch(self, task_id: str) -> tuple[bool, str]:
        branch = self.branch(task_id)
        proc = subprocess.run(
            ["git", "merge", "--ff-only", "--no-edit", branch],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()

    def clear_identical_untracked_for_ff(
        self, task_id: str
    ) -> tuple[list[str], list[str]]:
        del task_id
        return [], []

    def drcode_branch_state(self) -> tuple[set[str], set[str]]:
        return set(), set()


def _make_repo(root: Path) -> Path:
    """A git repo on `main` with one base commit; returns the root. The
    content is valid Python so the verification funnel's ruff/pyright
    pass on the resolved tree."""
    root.mkdir(parents=True, exist_ok=True)
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "test@example.com")
    _run(root, "config", "user.name", "Test")
    _commit(root, "app.py", "a = 1\nb = 2\nc = 3\n", "base")
    return root


def _task_worktree(root: Path, task_id: str) -> Path:
    """The task's worktree the way `drcode.task_env` deploys it."""
    wt = root / ".drcode" / "worktrees" / task_id
    _run(root, "worktree", "add", str(wt), "-b", f"drcode/{task_id}", "HEAD")
    return wt


def _parked_conflict(root: Path, task_id: str = "t1") -> tuple[Path, MergeModel]:
    """`main` and the task branch edit the same line; the reversed merge
    is deployed into the task's worktree (the engine's own prepare)."""
    wt = _task_worktree(root, task_id)
    _commit(wt, "app.py", "task = 1\nb = 2\nc = 3\n", "task edit")
    _commit(root, "app.py", "main = 1\nb = 2\nc = 3\n", "main edit")
    model = MergeModel(root, task_id, FakeEnv(root))
    report = model.prepare()
    assert report.ok and report.conflicted, report.message
    return wt, model


# -- hunk parsing -------------------------------------------------------------------


def test_parse_hunks_zdiff3_sections() -> None:
    text = (
        "head\n"
        "<<<<<<< HEAD: app.py\n"
        "task line\n"
        "||||||| 57cb0d9\n"
        "ancestor line\n"
        "=======\n"
        "base line\n"
        ">>>>>>> main\n"
        "tail\n"
    )
    hunks = parse_hunks(text)
    assert len(hunks) == 1
    hunk = hunks[0]
    assert hunk.ours == ["task line"]
    assert hunk.base == ["ancestor line"]
    assert hunk.theirs == ["base line"]
    assert hunk.head_label == "HEAD: app.py"
    assert hunk.tail_label == "main"
    assert text.split("\n")[hunk.start].startswith("<<<<<<<")
    assert text.split("\n")[hunk.end - 1].startswith(">>>>>>>")


def test_parse_hunks_plain_style_and_garbage_tail() -> None:
    text = (
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n<<<<<<< HEAD\nunterminated"
    )
    hunks = parse_hunks(text)
    assert len(hunks) == 1  # the unterminated tail is garbage, not a hunk
    assert hunks[0].ours == ["ours"]
    assert hunks[0].base == []  # no ||||||| section in the plain style
    assert hunks[0].theirs == ["theirs"]
    assert parse_hunks("no conflicts here\nat all") == []


# -- hunk decisions + marker-gated staging --------------------------------------------


def test_hunk_decisions_stage_only_when_marker_free(tmp_path: Path) -> None:
    """ours/theirs/both on the hunks; the file is staged exactly when no
    markers remain in it — a partially resolved file cannot sneak into
    the merge commit."""
    root = _make_repo(tmp_path / "proj")
    wt, model = _parked_conflict(root)

    assert model.mode() == MODE_CONFLICT
    states = model.file_states()
    assert [s.path for s in states] == ["app.py"]
    assert states[0].unmerged and states[0].hunks == 1

    # The zdiff3 content the engine parked: ours / ancestor / theirs.
    text = model.read_file("app.py")
    assert text is not None
    hunks = parse_hunks(text)
    assert len(hunks) == 1
    assert hunks[0].ours == ["task = 1"]
    assert hunks[0].base == ["a = 1"]
    assert hunks[0].theirs == ["main = 1"]

    # "both" clears the hunk → the file is marker-free → staged.
    ok, msg = model.apply_hunk("app.py", hunks[0], BOTH)
    assert ok, msg
    assert "staged" in msg
    assert parse_hunks(_read(wt, "app.py")) == []
    assert _read(wt, "app.py") == "task = 1\nmain = 1\nb = 2\nc = 3\n"
    staged = _run(wt, "diff", "--cached", "--name-only").stdout.split()
    assert staged == ["app.py"]

    # The gate is on MARKERS, not on the choice: a second conflicted
    # file stays unstaged until its last hunk is decided (the resolved
    # app.py has left the live conflict set).
    _commit(wt, "extra.py", "task2\n", "task adds extra.py")
    _commit(root, "extra.py", "main2\n", "main adds extra.py")
    model.prepare()
    assert [s.path for s in model.file_states()] == ["extra.py"]
    assert model.mode() == MODE_CONFLICT
    ok, msg = model.stage_clean("extra.py")
    assert not ok
    assert "markers remain" in msg
    unmerged = _run(wt, "status", "--porcelain").stdout
    assert "UU extra.py" in unmerged or "AA extra.py" in unmerged

    # The last decision stages it too: the merge becomes fully resolved.
    extra = model.hunks("extra.py")[0]
    ok, msg = model.apply_hunk("extra.py", extra, THEIRS)
    assert ok and "staged" in msg
    assert model.mode() == MODE_STAGED


def test_take_side_materializes_report_blobs(tmp_path: Path) -> None:
    """Markerless kinds (modify-delete) have no hunks: the whole-file
    decision materializes a side straight from the triage report's
    recorded stage blobs — or records the deletion with git rm."""
    root = _make_repo(tmp_path / "proj")
    _commit(root, "del.txt", "base version\n", "base adds del.txt")
    wt = _task_worktree(root, "t1")
    # The task branch MODIFIES del.txt, the base DELETES it.
    _commit(wt, "del.txt", "task kept this\n", "task edits del.txt")
    _run(root, "rm", "-f", "del.txt")
    _run(root, "commit", "-qm", "main deletes del.txt")

    model = MergeModel(root, "t1", FakeEnv(root))
    report = model.prepare()
    assert report.conflicted
    entry = next(
        f for f in (model.report.files if model.report else []) if f.path == "del.txt"
    )
    assert entry.kind == merge_engine.MODIFY_DELETE
    states = {s.path: s for s in model.file_states()}
    assert states["del.txt"].unmerged and states["del.txt"].hunks is None

    # theirs = the base deleted it → the deletion is recorded (git rm).
    ok, msg = model.take_side("del.txt", THEIRS)
    assert ok, msg
    assert "removed" in msg or "staged" in msg
    assert not (wt / "del.txt").exists()

    # Re-park and take OURS: the task's blob comes back byte-for-byte.
    model.abort()
    model.prepare()
    ok, msg = model.take_side("del.txt", OURS)
    assert ok, msg
    assert _read(wt, "del.txt") == "task kept this\n"
    # 'both' is not defined for the markerless kinds.
    try:
        model.take_side("del.txt", BOTH)
        raise AssertionError("both must raise for markerless kinds")
    except ValueError:
        pass
    assert model.mode() == MODE_STAGED


# -- finish / abort through the GUI model ----------------------------------------------


def test_finish_commit_and_ff_through_model(tmp_path: Path) -> None:
    """The full happy path: prepare → decide → finish (the commit lands
    on the task's OWN branch) → the ff-ready mode → the explicit
    --ff-only merge brings the work into the base checkout."""
    root = _make_repo(tmp_path / "proj")
    wt, model = _parked_conflict(root)

    hunk = model.hunks("app.py")[0]
    ok, msg = model.apply_hunk("app.py", hunk, OURS)
    assert ok, msg
    assert model.mode() == MODE_STAGED

    ok, msg = model.commit("resolved in the GUI")
    assert ok, msg
    # The merge commit is on the TASK branch...
    log = _run(wt, "log", "--oneline", "-1").stdout
    assert "resolved in the GUI" in log
    # ...the user's checkout is still untouched...
    assert _read(root, "app.py") == "main = 1\nb = 2\nc = 3\n"
    assert model.mode() == MODE_FF

    ok, msg = model.ff_merge()
    assert ok, msg
    assert _read(root, "app.py") == "task = 1\nb = 2\nc = 3\n"

    # The contract's other half: a moved base is a refusal, never a
    # conflicted merge in the user's tree.
    _commit(root, "app.py", "moved = 1\nb = 2\nc = 3\n", "main moves")
    ok, msg = model.ff_merge()
    assert not ok
    assert "no longer an ancestor" in msg
    assert _read(root, "app.py") == "moved = 1\nb = 2\nc = 3\n"


def test_finish_refuses_while_unmerged_paths_remain(tmp_path: Path) -> None:
    """The engine's finish is the last line of defense behind the
    screen's own checks: an unresolved path refuses the commit."""
    root = _make_repo(tmp_path / "proj")
    wt = _task_worktree(root, "t1")
    _commit(wt, "app.py", "task\nb\nc\n", "task edit")
    _commit(root, "app.py", "main\nb\nc\n", "main edit")
    model = MergeModel(root, "t1", FakeEnv(root))
    model.prepare()

    ok, msg = model.commit("premature")
    assert not ok
    assert "remain" in msg
    assert merge_engine.in_progress(wt)
    assert model.mode() == MODE_CONFLICT


def test_abort_rolls_back_to_pre_merge_state(tmp_path: Path) -> None:
    """Abort: the parked merge (decisions included) is discarded, the
    worktree is back to the pre-merge state and the flow can simply
    start over."""
    root = _make_repo(tmp_path / "proj")
    wt, model = _parked_conflict(root)
    hunk = model.hunks("app.py")[0]
    model.apply_hunk("app.py", hunk, THEIRS)
    head_before = _run(wt, "rev-parse", "HEAD").stdout.strip()

    ok, msg = model.abort()
    assert ok, msg
    assert model.in_progress() is False
    assert _run(wt, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _read(wt, "app.py") == "task = 1\nb = 2\nc = 3\n"
    assert model.mode() == MODE_UNMERGED

    report = model.prepare()
    assert report.ok and report.conflicted
    assert model.mode() == MODE_CONFLICT


# -- the verification funnel (the LLM-free levels) ---------------------------


def test_verification_funnel_runs_in_worktree(tmp_path: Path) -> None:
    """The funnel (markers → compile → ruff → pyright → tests) runs
    inside the task's worktree and is persisted next to the triage
    report; the review diffs name both sides."""
    root = _make_repo(tmp_path / "proj")
    _parked_conflict(root)
    model = MergeModel(root, "t1", FakeEnv(root))

    checks, tests = model.run_verification()
    names = [c.name for c in checks] + [tests.name]
    assert names[0] == "conflict markers"
    assert any("compile" in n for n in names)
    assert any(n.startswith("test suite") for n in names)
    # The unresolved tree must fail the marker check...
    markers = checks[0]
    assert not markers.ok
    assert "app.py" in markers.output
    # ...and the report survives the screen being closed.
    assert merge_resolver.verify_report_path(root, "t1").exists()

    ours, theirs = model.review_diffs()
    assert "app.py" in ours and "app.py" in theirs

    # After the resolution the funnel passes (the repo has no tests/
    # directory: the suite reports itself skipped).
    hunk = model.hunks("app.py")[0]
    model.apply_hunk("app.py", hunk, OURS)
    checks, tests = model.run_verification()
    assert all(c.ok for c in [*checks, tests])


# -- the dialogs (headless wiring smoke) ------------------------------------------


def test_dialogs_construct_and_route(tmp_path: Path, monkeypatch: Any) -> None:
    """The Qt dialogs route to the same model headlessly: the merge
    screen shows the conflict and its decision button lands the hunk
    (staging the file), the unmerged list carries the row."""

    async def scenario(shell: Any, hub: Any) -> None:
        from drcodedesktop.merge_screen import MergeScreenDialog
        from drcodedesktop.unmerged_screen import UnmergedDialog

        task, _wt = _scenario_repo_with_shell(shell)
        model = MergeModel(shell.project_root, task.id, shell.task_env)
        model.prepare()

        dialog = MergeScreenDialog(shell, task.id)
        dialog._refresh()
        state = dialog.state_for_tests()
        assert state["mode"] == MODE_CONFLICT
        assert [p for p, _b in state["files"]] == ["app.py"]

        # select the file (the render follows the selection) and decide
        dialog._file_list.setCurrentRow(0)
        assert dialog.state_for_tests()["hunks"] == 1
        dialog._hunk_index = 0
        dialog._decide(OURS)
        assert model.mode() == MODE_STAGED
        assert dialog.state_for_tests()["mode"] == MODE_STAGED
        dialog.deleteLater()

        unmerged = UnmergedDialog(shell)
        unmerged._refresh()
        assert (task.id, "conflict") in unmerged.state_for_tests()
        unmerged.deleteLater()

        # Give the loop the ticks to run the deferred deletions.
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def _scenario_repo_with_shell(shell: Any) -> tuple[Any, Path]:
    """The shell-scenario variant of `_parked_conflict`'s repo setup:
    git repo + isolation on + one task whose branch conflicts with the
    base (the merge itself is deployed by the caller)."""
    root = shell.project_root
    _make_repo(root)
    (root / ".gitignore").write_text(".drcode/\n")
    shell.settings.task_isolation = TaskIsolation.WORKTREE
    task = shell.create_task("Merge me")
    assert task is not None
    wt = _task_worktree(root, task.id)
    _commit(wt, "app.py", "task = 1\nb = 2\nc = 3\n", "task edit")
    _commit(root, "app.py", "main = 1\nb = 2\nc = 3\n", "main edit")
    return task, wt


# -- the unmerged-branches catalog ----------------------------------------------


def test_unmerged_catalog_kinds(tmp_path: Path, monkeypatch: Any) -> None:
    """The UnmergedDialog rows: conflict (parked or needs_human), staged
    (resolved, not committed), ff (clean), unmerged (plain branch) — and
    a merged branch is dropped. Driven through the REAL TaskEnvironment
    (the shell's env — what the dialogs actually pass)."""

    async def scenario(shell: Any, hub: Any) -> None:
        del hub
        root = shell.project_root
        _make_repo(root)
        (root / ".gitignore").write_text(".drcode/\n")
        shell.settings.task_isolation = TaskIsolation.WORKTREE

        conflict_task = shell.create_task("Conflicted")
        staged_task = shell.create_task("Staged")
        ff_task = shell.create_task("Fast forward")
        plain_task = shell.create_task("Plain unmerged")
        merged_task = shell.create_task("Already merged")
        assert conflict_task and staged_task and ff_task
        assert plain_task and merged_task

        # conflict: needs_human with a parked merge
        wt = _task_worktree(root, conflict_task.id)
        _commit(wt, "app.py", "task\nb\nc\n", "task edit")
        _commit(root, "app.py", "main\nb\nc\n", "main edit")
        report = merge_resolver.prepare(root, conflict_task.id, wt, "main")
        assert report.conflicted
        conflict_task.needs_human = True
        conflict_task.touch()
        shell.manager.storage.save(conflict_task)

        # staged: resolved + staged, merge state concluded by hand
        # (`git merge --quit` leaves the staged tree without MERGE_HEAD;
        # the engine itself persists only CONFLICTED reports, so the
        # resumed-merge report (ok, not conflicted, ff=None) the staged
        # row is classified from is saved explicitly here)
        wt2 = _task_worktree(root, staged_task.id)
        _commit(wt2, "app.py", "task\nb\nc\n", "task edit")
        merge_resolver.prepare(root, staged_task.id, wt2, "main")
        (wt2 / "app.py").write_text("resolved\nb\nc\n")
        _run(wt2, "add", "app.py")
        _run(wt2, "merge", "--quit")
        merge_engine.save_report(
            root,
            staged_task.id,
            merge_engine.MergeReport(
                task_id=staged_task.id,
                branch=f"drcode/{staged_task.id}",
                base_branch="main",
                worktree=str(wt2),
                ok=True,
                conflicted=False,
                fast_forward=None,
            ),
        )

        # ff: a clean reversed merge (no conflict at all)
        wt3 = _task_worktree(root, ff_task.id)
        _commit(wt3, "feature.txt", "feature\n", "feature work")
        merge_engine.merge_base_into_worktree(root, ff_task.id, wt3, "main", save=True)

        # plain unmerged: a DIVERGED branch — forked from an older base
        # (the wrong-start-point case the catalog exists for): its
        # commits are not ff-able into the moved base, no merge parked
        wt4 = root / ".drcode" / "worktrees" / plain_task.id
        _run(
            root,
            "worktree",
            "add",
            str(wt4),
            "-b",
            f"drcode/{plain_task.id}",
            "HEAD~1",
        )
        _commit(wt4, "other.txt", "other\n", "plain work")

        # not-yet-merged: its branch still holds the base's content
        wt5 = _task_worktree(root, merged_task.id)
        _commit(wt5, "same.txt", "same\n", "to be merged by hand")

        entries = collect_unmerged_entries(root, shell.task_env, shell.manager.tasks)
        kinds = {e.task_id: e.kind for e in entries}
        assert kinds[conflict_task.id] == "conflict"
        assert kinds[staged_task.id] == "staged"
        assert kinds[ff_task.id] == "ff"
        assert kinds[plain_task.id] == "unmerged"
        assert kinds[merged_task.id] == "ff"

        # needs_human sorts first (the dialog's default order).
        assert entries[0].task_id == conflict_task.id
        labels = {e.task_id: e.state_label for e in entries}
        assert "needs resolution" in labels[conflict_task.id]
        assert "commit it" in labels[staged_task.id]
        assert "ff-only" in labels[ff_task.id]

        # After the conflict is resolved and committed, the row flips
        # to ff (the branch can now fast-forward the base).
        model = MergeModel(root, conflict_task.id, shell.task_env, shell.manager.tasks)
        hunk = model.hunks("app.py")[0]
        model.apply_hunk("app.py", hunk, OURS)
        ok, msg = model.commit("done")
        assert ok, msg
        model.clear_needs_human()  # the dialog's _commit_done does this
        rep = merge_engine.load_report(root, conflict_task.id)
        assert not merge_engine.in_progress(wt), "merge must be concluded"
        assert not conflict_task.needs_human
        assert merge_engine.is_fast_forward(
            root, f"drcode/{conflict_task.id}", "main"
        ), f"branch must be ff-able; report={(rep.ok, rep.conflicted) if rep else None}"
        entries3 = collect_unmerged_entries(root, shell.task_env, shell.manager.tasks)
        kinds3 = {e.task_id: e.kind for e in entries3}
        assert kinds3[conflict_task.id] == "ff"

        # already merged: branch content reachable from the base (the
        # ff moves main — the row drops from the catalog afterwards)
        _run(root, "merge", "--ff-only", f"drcode/{merged_task.id}")
        entries_m = collect_unmerged_entries(root, shell.task_env, shell.manager.tasks)
        assert merged_task.id not in {e.task_id for e in entries_m}

    run_shell_scenario(tmp_path, park_queue(monkeypatch), scenario)


def test_model_is_inert_without_isolation(tmp_path: Path) -> None:
    """No worktree isolation (or no git repo) → the model is idle and
    every decision is a refusal — the screen cannot exist without the
    isolated worktree."""
    from drcode.settings import ProjectSettings
    from drcode.task_env import TaskEnvironment

    root = tmp_path / "bare"
    root.mkdir()
    env = TaskEnvironment(root, ProjectSettings())  # isolation OFF
    model = MergeModel(root, "t0", env)
    assert model.mode() == MODE_IDLE
    assert model.file_states() == []
    assert model.hunks("x.py") == []
    ok, _msg = model.commit("nope")
    assert ok is False
    ok, _msg = model.abort()
    assert ok is False
    ok, _msg = model.take_side("x.py", OURS)
    assert ok is False
    ok, _msg = model.stage_clean("x.py")
    assert ok is False
    ok, _msg = model.ff_merge()
    assert ok is False


def test_mode_labels_cover_all_modes() -> None:
    from drcodedesktop.merge_model import MODE_LABELS

    for mode in (MODE_CONFLICT, MODE_FF, MODE_IDLE, MODE_STAGED, MODE_UNMERGED):
        assert MODE_LABELS[mode]
