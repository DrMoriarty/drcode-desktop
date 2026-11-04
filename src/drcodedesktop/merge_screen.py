"""The merge-conflict screen (plan task 9) — the GUI port of the TUI's
``tui.merge_screen.ConflictScreen`` (~2200 lines), reduced to the same
workflow over Qt widgets:

- left: the conflicted files of the triage report with their LIVE state
  (resolved / N hunk(s) / untouched — refreshed from the worktree, the
  report alone goes stale as the user works) and the triage-report panel
  below it (the report's verdict, the conflict kinds, the auto-resolvers
  that already cleared paths);
- right: the selected file in its zdiff3 form — ours (the task branch),
  the common ancestor (the ``|||||||`` section) and theirs (the base
  branch) — with the hunks highlighted and a prev/next hunk navigation;
- decisions: ours / theirs / both on the current hunk (the
  ``drcode.conflict`` model applies the replacement and stages the file
  ONLY when no markers remain in it); whole-file ours/theirs for the
  markerless kinds (modify-delete, rename family); a manual "stage" for
  a file resolved outside;
- the verification funnel of the resolver stack (``mechanical_checks``
  and the test suite — the LLM layer stays in the core) with the
  persisted report and the review diffs (resolved vs ours / theirs);
- conclusions: commit (``merge_engine.finish`` — refuses while unmerged
  paths remain), abort (the rollback) and the ``--ff-only`` merge into
  the base — the last one only from the ff-ready state and behind the
  base-is-an-ancestor re-verification (the contract: the user's checkout
  is touched only by the explicit ff, never by a real merge).

Every blocking call (git, file IO, the funnel) runs in a worker thread;
the write-backs land on the GUI thread through a queued signal. The
commit/abort confirmations are explicit (a message-box second press —
the armed-key convention of the TUI translated to widgets).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, cast

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QKeySequence,
    QShortcut,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .merge_model import (
    BOTH,
    MODE_CONFLICT,
    MODE_FF,
    MODE_IDLE,
    MODE_LABELS,
    MODE_STAGED,
    MODE_UNMERGED,
    OURS,
    THEIRS,
    MergeModel,
    parse_hunks,
)

if TYPE_CHECKING:
    from drcode.conflict import Hunk
    from drcode.merge_engine import MergeReport

# The hunk highlight colors (current hunk vivid, the rest muted).
_CURRENT_HUNK = QColor(229, 192, 123, 70)
_OTHER_HUNK = QColor(100, 100, 100, 36)
_ERROR_COLOR = QColor(224, 108, 117)
_OK_COLOR = QColor(152, 195, 121)


class _Worker(QObject):
    """One-shot blocking job runner: the job body runs in a daemon
    thread, the result is delivered as a queued signal into the dialog's
    thread (the Qt way to keep every widget touch on the GUI thread)."""

    done = Signal(object)

    def run(self, job: Any, on_done: Any) -> None:
        result_holder: list[Any] = []

        def body() -> None:
            try:
                result_holder.append(job())
            except Exception as e:
                result_holder.append(e)
            self.done.emit(result_holder[0])

        self.done.connect(on_done)
        threading.Thread(target=body, daemon=True).start()


class MergeScreenDialog(QDialog):
    """The parked-conflict resolution screen for one task."""

    def __init__(self, shell: Any, task_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        self._task_id = task_id
        self._model = MergeModel.for_task(shell, task_id)
        self._worker = _Worker(self)
        self._busy = False
        self._files: list[tuple[str, str]] = []  # (path, badge) rows
        self._hunks: list[Hunk] = []
        self._hunk_index = -1
        self._closed_by_success = False

        self.setWindowTitle(f"Merge — #{task_id}")
        self.resize(1100, 720)

        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        fixed.setPointSize(11)

        # -- left column: file list + triage report --------------------------
        self._file_list = QListWidget()
        self._file_list.currentRowChanged.connect(self._on_file_selected)
        self._triage = QPlainTextEdit(readOnly=True)
        self._triage.setFont(fixed)
        self._triage.setMaximumHeight(150)
        self._triage.setPlaceholderText("Triage report")

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._file_list, 3)
        left_layout.addWidget(self._triage, 1)

        # -- right column: the zdiff3 content with hunk navigation -----------
        self._content = QPlainTextEdit(readOnly=True)
        self._content.setFont(fixed)
        self._content.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._cursor = QPlainTextEdit(readOnly=True)
        self._cursor.setFont(fixed)
        self._cursor.setMaximumHeight(64)
        self._cursor.setPlaceholderText("Review diff")

        hunk_bar = QHBoxLayout()
        self._hunk_label = QLabel("no hunks")
        self._hunk_label.setStyleSheet("color: #6b7280;")
        prev = QPushButton("◀ Prev hunk")
        prev.clicked.connect(self._prev_hunk)
        nxt = QPushButton("Next hunk ▶")
        nxt.clicked.connect(self._next_hunk)
        hunk_bar.addWidget(self._hunk_label)
        hunk_bar.addStretch(1)
        hunk_bar.addWidget(prev)
        hunk_bar.addWidget(nxt)

        decision_bar = QHBoxLayout()
        self._ours_btn = QPushButton("Take ours (task branch)")
        self._ours_btn.clicked.connect(lambda: self._decide(OURS))
        self._theirs_btn = QPushButton("Take theirs (base branch)")
        self._theirs_btn.clicked.connect(lambda: self._decide(THEIRS))
        self._both_btn = QPushButton("Keep both")
        self._both_btn.clicked.connect(lambda: self._decide(BOTH))
        self._stage_btn = QPushButton("Stage file")
        self._stage_btn.clicked.connect(self._stage_file)
        for b in (self._ours_btn, self._theirs_btn, self._both_btn, self._stage_btn):
            decision_bar.addWidget(b)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(hunk_bar)
        right_layout.addWidget(self._content, 1)
        right_layout.addWidget(self._cursor, 0)
        right_layout.addLayout(decision_bar)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # -- verification funnel ----------------------------------------------
        self._verify_log = QPlainTextEdit(readOnly=True)
        self._verify_log.setFont(fixed)
        self._verify_log.setMaximumHeight(140)
        self._verify_log.setPlaceholderText("Verification funnel")
        verify_bar = QHBoxLayout()
        self._verify_btn = QPushButton(
            "Verify (markers → compile → ruff → pyright → tests)"
        )
        self._verify_btn.clicked.connect(self._run_verification)
        self._review_btn = QPushButton("Review diffs")
        self._review_btn.clicked.connect(self._show_review_diffs)
        verify_bar.addWidget(self._verify_btn)
        verify_bar.addWidget(self._review_btn)
        verify_bar.addStretch(1)

        # -- conclusion bar -------------------------------------------------------
        bottom_bar = QHBoxLayout()
        self._prepare_btn = QPushButton("Prepare merge")
        self._prepare_btn.clicked.connect(self._prepare)
        self._commit_btn = QPushButton("Commit resolution…")
        self._commit_btn.clicked.connect(self._commit)
        self._abort_btn = QPushButton("Abort merge…")
        self._abort_btn.clicked.connect(self._abort)
        self._ff_btn = QPushButton("Merge into base (ff-only)")
        self._ff_btn.clicked.connect(self._ff_merge)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        bottom_bar.addWidget(self._prepare_btn)
        bottom_bar.addWidget(self._commit_btn)
        bottom_bar.addWidget(self._abort_btn)
        bottom_bar.addWidget(self._ff_btn)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(refresh)
        bottom_bar.addWidget(close)

        self._mode_label = QLabel("")
        self._mode_label.setStyleSheet("color: #6b7280;")

        layout = QVBoxLayout(self)
        layout.addWidget(self._mode_label)
        layout.addWidget(splitter, 1)
        layout.addLayout(verify_bar)
        layout.addWidget(self._verify_log)
        layout.addLayout(bottom_bar)

        QShortcut(QKeySequence("Ctrl+Enter"), self, self._commit)
        QShortcut(QKeySequence("F5"), self, self._refresh)

    # -- lifecycle ------------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Every open is a fresh look; an undeployed unmerged branch is
        prepared right away (the TUI screen's opening prepare phase)."""
        super().showEvent(event)  # type: ignore[arg-type]
        if self._model.mode() == MODE_UNMERGED:
            self._prepare()
        else:
            self._refresh()

    # -- state helpers ----------------------------------------------------------

    def _mode(self) -> str:
        return self._model.mode()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (
            self._ours_btn,
            self._theirs_btn,
            self._both_btn,
            self._stage_btn,
            self._verify_btn,
            self._review_btn,
            self._prepare_btn,
            self._commit_btn,
            self._abort_btn,
            self._ff_btn,
        ):
            w.setEnabled(not busy)

    def _log(self, text: str, color: QColor | None = None) -> None:
        if color is not None:
            self._verify_log.appendHtml(
                f'<span style="color: {color.name()};">{_esc(text)}</span>'
            )
        else:
            self._verify_log.appendPlainText(text)

    def _bg(self, job: Any, on_done: Any) -> None:
        self._set_busy(True)
        self._worker.run(job, self._unwrap(on_done))

    def _unwrap(self, on_done: Any) -> Any:
        def deliver(result: object) -> None:
            self._set_busy(False)
            on_done(result)

        return deliver

    @staticmethod
    def _pair(result: object) -> tuple[bool, str]:
        """The (ok, message) of a model call — exceptions become
        (False, "ExceptionType: …"), so a git failure is a red log line,
        never a crash."""
        if isinstance(result, Exception):
            return False, f"{type(result).__name__}: {result}"
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1])
        return False, str(result)

    # -- rendering -----------------------------------------------------------------

    def _refresh(self) -> None:
        """Sync the whole view from the worktree (sync: local file reads
        plus one cheap git status — the TUI runs it in a worker too, but
        the dialog's file list needs it on every interaction)."""
        self._model.refresh()
        mode = self._mode()
        self._mode_label.setText(
            f"#{self._task_id} — {self._model.branch} ← {self._model.base_branch}"
            f" — {MODE_LABELS.get(mode, mode)}"
        )
        for w, modes in (
            (self._prepare_btn, (MODE_UNMERGED,)),
            (self._commit_btn, (MODE_STAGED, MODE_CONFLICT)),
            (self._abort_btn, (MODE_CONFLICT, MODE_STAGED)),
            (self._ff_btn, (MODE_FF,)),
        ):
            w.setVisible(mode in modes)
        if mode in (MODE_IDLE, MODE_FF):
            self._diff_view()
        else:
            self._file_view()
        self._triage.setPlainText(self._model.triage_text())

    def _file_view(self) -> None:
        """The conflicted-files list with their live badges."""
        states = self._model.file_states()
        selected = self._selected_path()
        self._file_list.clear()
        self._files = []
        for st in states:
            badge = _badge(st.unmerged, st.has_markers, st.hunks)
            item = QListWidgetItem(f"{st.path}   {badge}")
            if st.resolved:
                item.setForeground(_OK_COLOR)
            elif st.unmerged and not st.hunks:
                item.setForeground(_ERROR_COLOR)
            self._file_list.addItem(item)
            self._files.append((st.path, badge))
        if selected:
            for i, (path, _row_badge) in enumerate(self._files):
                if path == selected:
                    self._file_list.setCurrentRow(i)
                    break
            else:
                self._content.clear()
                self._hunks = []
        elif self._files:
            self._file_list.setCurrentRow(0)
        else:
            self._content.clear()
            self._hunks = []

    def _diff_view(self) -> None:
        """The ff-ready / idle face: the change size the conclusion is
        about (the same `git diff --numstat` picture the TUI shows)."""
        self._file_list.clear()
        self._files = []
        self._hunks = []
        base = self._model.base_branch or "HEAD"
        stats = self._model.diff_stat(
            f"{base}..{self._model.branch}",
            cwd=self._model.project_root,
        )
        added = sum(s.added or 0 for s in stats)
        deleted = sum(s.deleted or 0 for s in stats)
        files = len(stats)
        summary = (
            f"{files} file(s) changed, {added} insertion(s), {deleted} deletion(s)"
        )
        self._file_list.addItem(QListWidgetItem(summary))
        for s in stats:
            counts = "bin, bin" if s.binary else f"+{s.added}, -{s.deleted}"
            self._file_list.addItem(QListWidgetItem(f"  {s.path}   {counts}"))
        self._content.setPlainText(
            "Nothing left to resolve in the worktree.\n"
            "Commit the resolution (staged mode) or fast-forward the base "
            "(ff-ready) — the actions below."
        )

    def _selected_path(self) -> str | None:
        row = self._file_list.currentRow()
        if 0 <= row < len(self._files):
            return self._files[row][0]
        return None

    def _on_file_selected(self, row: int) -> None:
        if not 0 <= row < len(self._files):
            return
        self._render_file(self._files[row][0])

    def _render_file(self, path: str) -> None:
        text = self._model.read_file(path)
        if text is None:
            self._content.setPlainText(f"(unreadable: {path})")
            self._hunks = []
            return
        self._content.setPlainText(text)
        self._hunks = parse_hunks(text)
        self._hunk_index = 0 if self._hunks else -1
        self._highlight()

    def _highlight(self) -> None:
        """Rebuild the hunk extra-selections and jump the cursor to the
        current hunk (the TUI's cursor-over-hunks equivalent)."""
        doc = self._content.document()
        selections: list[QTextEdit.ExtraSelection] = []
        fmt_other = QTextCharFormat()
        fmt_other.setBackground(_OTHER_HUNK)
        fmt_current = QTextCharFormat()
        fmt_current.setBackground(_CURRENT_HUNK)
        for i, hunk in enumerate(self._hunks):
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down, n=hunk.start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                n=max(1, hunk.size()),
            )
            sel = QTextEdit.ExtraSelection()
            # PySide6 stubs omit the ExtraSelection fields; the runtime
            # assignment is the documented way to fill them.
            sel.cursor = cursor  # type: ignore[reportAttributeAccessIssue]
            sel.format = (  # type: ignore[reportAttributeAccessIssue]
                fmt_current if i == self._hunk_index else fmt_other
            )
            selections.append(sel)
        self._content.setExtraSelections(selections)
        total = len(self._hunks)
        if total and 0 <= self._hunk_index < total:
            self._hunk_label.setText(f"hunk {self._hunk_index + 1} of {total}")
            hunk = self._hunks[self._hunk_index]
            self._cursor.setPlainText(
                f"ours (task): {len(hunk.ours)} lines · "
                f"ancestor: {len(hunk.base)} · theirs (base): {len(hunk.theirs)}"
                + (f" — {hunk.head_label}" if hunk.head_label else "")
            )
        else:
            self._hunk_label.setText("no hunks")
            self._cursor.setPlainText("")

    def _prev_hunk(self) -> None:
        if self._hunks:
            self._hunk_index = max(0, self._hunk_index - 1)
            self._highlight()

    def _next_hunk(self) -> None:
        if self._hunks:
            self._hunk_index = min(len(self._hunks) - 1, self._hunk_index + 1)
            self._highlight()

    # -- decisions -------------------------------------------------------------------

    def _current_hunk(self) -> Hunk | None:
        if not self._hunks or not 0 <= self._hunk_index < len(self._hunks):
            return None
        return self._hunks[self._hunk_index]

    def _decide(self, choice: str) -> None:
        """The ours/theirs/both decision: on the current hunk for a
        content conflict, or as the whole-file side for a markerless
        kind. The model stages the file only when no markers remain."""
        path = self._selected_path()
        if path is None or self._busy:
            return
        hunk = self._current_hunk()
        if hunk is not None:
            ok, msg = self._model.apply_hunk(path, hunk, choice)
        else:
            if choice == BOTH:
                self._log(
                    "whole-file 'both' is not defined for this kind", _ERROR_COLOR
                )
                return
            ok, msg = self._model.take_side(path, choice)
        self._log(f"{path}: {msg}", None if ok else _ERROR_COLOR)
        self._refresh()
        self._render_file(path)

    def _stage_file(self) -> None:
        path = self._selected_path()
        if path is None or self._busy:
            return
        ok, msg = self._model.stage_clean(path)
        self._log(f"{path}: {msg}", None if ok else _ERROR_COLOR)
        self._refresh()

    # -- verification funnel -------------------------------------------------

    def _run_verification(self) -> None:
        if self._busy:
            return
        self._log("verification: running…")
        self._bg(
            self._model.run_verification,
            lambda result: self._verification_done(result),
        )

    def _verification_done(self, result: object) -> None:
        if isinstance(result, Exception):
            self._log(f"verification failed to run: {result}", _ERROR_COLOR)
            return
        if not isinstance(result, tuple) or len(result) != 2:
            self._log("verification: unexpected result", _ERROR_COLOR)
            return
        checks, tests = cast("tuple[list[Any], Any]", result)
        failed = [c.name for c in [*checks, tests] if not c.ok]
        verdict = (
            f"verification PASSED ({len(checks) + 1} checks)"
            if not failed
            else f"verification FAILED: {', '.join(failed)}"
        )
        self._log(verdict, None if not failed else _ERROR_COLOR)
        for c in [*checks, tests]:
            self._log(f"  {c.line}", None if c.ok else _ERROR_COLOR)
            if c.output and not c.ok:
                self._log("    " + c.output.replace("\n", "\n    ")[:2000])

    def _show_review_diffs(self) -> None:
        if self._busy:
            return
        self._bg(self._model.review_diffs, self._review_diffs_done)

    def _review_diffs_done(self, result: object) -> None:
        if isinstance(result, Exception):
            self._log(f"review diffs failed: {result}", _ERROR_COLOR)
            return
        if not isinstance(result, tuple) or len(result) != 2:
            self._log("review diffs: unexpected result", _ERROR_COLOR)
            return
        ours, theirs = cast("tuple[str, str]", result)
        self._cursor.setPlainText(
            f"diff vs ours (task branch):\n{ours or '(empty)'}\n\n"
            f"diff vs theirs (base branch):\n{theirs or '(empty)'}"
        )

    # -- prepare / conclusions -----------------------------------------------

    def _prepare(self) -> None:
        if self._busy:
            return
        self._log("prepare: running the reversed merge…")
        self._bg(self._model.prepare, self._prepare_done)

    def _prepare_done(self, result: object) -> None:
        if isinstance(result, Exception):
            self._log(f"prepare failed: {result}", _ERROR_COLOR)
            return
        report = cast("MergeReport", result)
        self._log(f"prepare: {report.summary()}", None if report.ok else _ERROR_COLOR)
        self._refresh()

    def _commit(self) -> None:
        if self._busy or self._mode() not in (MODE_STAGED, MODE_CONFLICT):
            return
        remaining = self._model.file_states()
        unresolved = [s.path for s in remaining if s.unmerged]
        if unresolved:
            QMessageBox.warning(
                self,
                "Merge",
                "Unmerged paths remain:\n" + "\n".join(unresolved[:20]),
            )
            return
        answer = QMessageBox.question(
            self,
            "Commit resolution",
            f"Commit the resolved merge on {self._model.branch}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._bg(
            lambda: self._model.commit(
                f"Merge branch '{self._model.base_branch}' into "
                f"{self._model.branch} (resolved in GUI)"
            ),
            self._commit_done,
        )

    def _commit_done(self, result: object) -> None:
        ok, msg = self._pair(result)
        self._log(f"commit: {msg}", None if ok else _ERROR_COLOR)
        if ok:
            self._model.clear_needs_human()
            self._close_success()

    def _abort(self) -> None:
        if self._busy or self._mode() not in (MODE_CONFLICT, MODE_STAGED):
            return
        answer = QMessageBox.question(
            self,
            "Abort merge",
            "Abort the merge and roll the task worktree back to the "
            "pre-merge state? All resolution decisions are discarded.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._bg(self._model.abort, self._abort_done)

    def _abort_done(self, result: object) -> None:
        ok, msg = self._pair(result)
        self._log(f"abort: {msg}", None if ok else _ERROR_COLOR)
        if ok:
            self._close_success()

    def _ff_merge(self) -> None:
        if self._busy or self._mode() != MODE_FF:
            return
        self._bg(self._model.ff_merge, self._ff_done)

    def _ff_done(self, result: object) -> None:
        ok, msg = self._pair(result)
        self._log(f"ff-merge: {msg}", None if ok else _ERROR_COLOR)
        if ok:
            self._model.clear_needs_human()
            self._close_success()

    def _close_success(self) -> None:
        self._closed_by_success = True
        self.accept()

    # -- tests ------------------------------------------------------------

    def state_for_tests(self) -> dict[str, Any]:
        """The headless inspection seam: the mode, the file badges and
        the current hunk index (the widgets' state the acceptance tests
        assert on without opening a real window)."""
        return {
            "mode": self._mode(),
            "files": list(self._files),
            "hunk_index": self._hunk_index,
            "hunks": len(self._hunks),
        }


def _badge(unmerged: bool, has_markers: bool, hunks: int | None) -> str:
    """The live status badge of a file row (the TUI FileState.badge in
    plain text — no rich markup in a Qt list)."""
    if not unmerged and not has_markers:
        return "✓ resolved"
    if unmerged and hunks:
        return f"{hunks} hunk(s)"
    if unmerged:
        return "untouched"
    return "edited, unstaged"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
