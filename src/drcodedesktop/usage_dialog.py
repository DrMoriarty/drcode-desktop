"""User-level token-usage dialog (plan task 7) — the GUI port of
``drcode.tui.usage_dialog``.

Shows the persistent per-day, per-model token statistics collected by
:mod:`drcode.usage_store` (``~/.config/drcode/usage.json``): one table
row per (day, model key) with the four per-response counters pydantic-ai
reports plus the request count, and a period-total row at the bottom.
The keys are ``"<role>/<model>"`` — model id in the catalog's
``"<provider>/<model-id>"`` format where the provider is known — so the
same model serving different roles shows as separate rows.

The file is read off the loop thread (a large usage history must never
stall the UI). Read-only: the dialog never writes.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from drcode.usage_store import FIELDS, UsageStore, usage_file
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

#: Human headers of the counter columns, in FIELDS order.
_FIELD_HEADERS = ("Requests", "Input", "Output", "Cache read", "Cache write")
_COLUMNS = ("Day", "Model", *_FIELD_HEADERS)


def _fmt(n: int) -> str:
    """Thousands-separated counter for the table cells."""
    return f"{n:,}"


class UsageDialog(QDialog):
    """Modal table: days x models with input/output/cache breakdown."""

    def __init__(
        self,
        store: UsageStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Token usage — per day and model")
        self.resize(720, 420)
        # Explicit resolved path by default (incident ebe6ba15
        # discipline): read-only today, but a bare UsageStore() would be
        # guard-locked forever under an inherited DRCODE_TASK_ID.
        self._store = store or UsageStore(path=usage_file())

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._status = QLabel("Loading…")
        close = QPushButton("Close")
        close.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addWidget(self._status)
        layout.addLayout(buttons)

    # -- loading -----------------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Start the (threaded) read every time the dialog opens — the
        file may have grown since the last look."""
        super().showEvent(event)  # type: ignore[arg-type]
        self.reload()

    def reload(self) -> None:
        """Read the store off the loop thread and fill the table (the
        qasync loop rides the Qt one — the dialog stays responsive)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (a bare constructor in a test): a
            # synchronous read is fine there.
            self._apply(self._store.load())
            return
        self._job = asyncio.ensure_future(self._read_async())

    async def _read_async(self) -> None:
        days = await asyncio.to_thread(self._store.load)
        self._apply(days)

    def _apply(self, days: dict) -> None:
        self._fill_table(days)
        self._status.setText(self._summary(days))

    # -- rendering (the TUI dialog's statics, verbatim data rules) -----------------

    def _fill_table(self, days: dict) -> None:
        """One row per (day, model), newest day first, then a period
        total. Clearing first makes a reload idempotent."""
        self._table.setRowCount(0)
        totals = dict.fromkeys(FIELDS, 0)
        rows = 0
        for day in sorted(days, reverse=True):
            for key in sorted(days[day]):
                counters = days[day][key]
                for field in FIELDS:
                    totals[field] += counters.get(field, 0)
                self._add_row(day, key, *(counters.get(f, 0) for f in FIELDS))
                rows += 1
        if rows:
            self._add_row("Σ period", "", *(totals[f] for f in FIELDS))

    def _add_row(self, day: str, key: str, *counters: int) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        cells = [day, key, *(_fmt(c) for c in counters)]
        bold = day == "Σ period"
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if bold:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            if column >= 2:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(row, column, item)

    @staticmethod
    def _summary(days: dict) -> str:
        """One-line status under the table: the period covered and the
        row count — or the empty-archive hint."""
        if not days:
            return "No usage recorded yet."
        span = f"{min(days)} … {max(days)}"
        models = {key for entry in days.values() for key in entry}
        return f"{len(days)} day(s), {len(models)} model key(s): {span}"
