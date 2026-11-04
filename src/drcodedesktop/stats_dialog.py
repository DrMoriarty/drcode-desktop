"""Project statistics dialog (plan task 7) — the GUI port of
``drcode.tui.stats_dialog`` (Ctrl+G): day buckets of the task funnel.

A read-only modal: a table with the last 14/30 UTC days (created /
completed / cancelled / failed + the average dev / testing / resolver
time per finished stay) and two sparklines over the same window
(created and completed per day).

The data never touches the loop's hot path: the whole read (a fresh
:class:`drcode.stats.StatsStore` load of ``.drcode/stats.json`` + the
window snapshot) runs once per dialog session in a thread — the dialog
is a pure reader (a throwaway store instance, no writes).

"P" toggles the window between 14 and 30 days; Esc closes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from drcode.stats import DayStats, StatsStore, format_seconds
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

# The two window sizes the "P" toggle flips between.
_PERIODS: tuple[int, ...] = (14, 30)

_COLUMNS = (
    "Дата",
    "Создано",
    "Выполнено",
    "Отменено",
    "Провалено",
    "Разработка",
    "Тесты",
    "Резолв",
)

_SPARK_CREATED = QColor(152, 195, 121)
_SPARK_COMPLETED = QColor(61, 156, 255)


class Sparkline(QWidget):
    """A minimal bar sparkline over one daily series (the Qt stand-in
    for the TUI's textual ``Sparkline`` widget): oldest day left, zero
    baseline at the bottom, the maximum scaled to the widget height."""

    def __init__(self, color: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: list[float] = []
        self._color = color
        self.setFixedHeight(36)

    @property
    def data(self) -> list[float]:
        return self._data

    @data.setter
    def data(self, values: list[float]) -> None:
        self._data = list(values)
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 2, -1, -2)
        if not self._data:
            return
        peak = max(self._data) or 1.0
        width = rect.width() / len(self._data)
        pen = QPen(self._color, 1.0)
        painter.setPen(pen)
        painter.setBrush(self._color)
        for i, value in enumerate(self._data):
            height = rect.height() * (value / peak)
            bar = QRectF(
                rect.left() + i * width,
                rect.bottom() - height,
                max(1.0, width - 1.0),
                height,
            )
            painter.drawRect(bar)
        painter.end()


class StatsDialog(QDialog):
    """Modal project statistics: day table + created/completed sparklines."""

    def __init__(self, project_root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Статистика")
        self.resize(760, 480)
        # The MAIN checkout's root — the stats live in the main
        # checkout's .drcode/ (the same storage root the manager uses).
        self._project_root = Path(project_root)
        self._period = _PERIODS[0]
        self._days: list[tuple[str, DayStats]] | None = None

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._created_spark = Sparkline(_SPARK_CREATED)
        self._completed_spark = Sparkline(_SPARK_COMPLETED)
        hint = QLabel("p — окно 14/30 дней · esc — close")
        hint.setAlignment(Qt.AlignmentFlag.AlignRight)
        hint.setStyleSheet("color: #6b7280;")

        sparks = QHBoxLayout()
        created_box = QVBoxLayout()
        created_box.addWidget(QLabel("Создано"))
        created_box.addWidget(self._created_spark)
        completed_box = QVBoxLayout()
        completed_box.addWidget(QLabel("Выполнено"))
        completed_box.addWidget(self._completed_spark)
        sparks.addLayout(created_box)
        sparks.addLayout(completed_box)

        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        layout.addLayout(sparks)
        layout.addWidget(hint)

        # The "p" window toggle (the TUI dialog's binding).
        QShortcut(QKeySequence("P"), self, self.action_toggle_period)

    # -- loading (thread) ----------------------------------------------------------

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Every open is a fresh read: the stats file changes under the
        running pipeline between two looks."""
        super().showEvent(event)  # type: ignore[arg-type]
        self._load_days()

    def _load_days(self) -> None:
        """Read the store off the loop thread: constructing the
        throwaway StatsStore does the (blocking) file read; the snapshot
        needs no lock of its own — the store instance is local to this
        read."""

        def read() -> list[tuple[str, DayStats]]:
            return StatsStore(self._project_root).snapshot(self._period)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (a bare constructor in a test): a
            # synchronous read is fine there.
            self._days = read()
            self._render_days()
            return
        self._job = asyncio.ensure_future(self._read_async(read))

    async def _read_async(self, read: Callable[[], list[tuple[str, DayStats]]]) -> None:
        self._days = await asyncio.to_thread(read)
        self._render_days()

    # -- rendering -------------------------------------------------------------------

    @staticmethod
    def _average(day: DayStats, kind: str) -> str:
        """The on-the-fly average for one kind ("dev"/"testing"/"resolver"):
        seconds / finished stays, human-formatted — "—" when nothing
        finished that day."""
        seconds = getattr(day, f"{kind}_seconds")
        rounds = getattr(day, f"{kind}_rounds")
        if rounds <= 0:
            return "—"
        return format_seconds(seconds / rounds)

    def _render_days(self) -> None:
        if self._days is None:
            return
        self._table.setRowCount(0)
        for day_key, day in reversed(self._days):  # newest day on top
            row = self._table.rowCount()
            self._table.insertRow(row)
            cells = (
                day_key,
                str(day.created),
                str(day.completed),
                str(day.cancelled),
                str(day.failed),
                self._average(day, "dev"),
                self._average(day, "testing"),
                self._average(day, "resolver"),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row, column, item)
        # Sparklines over the same window, oldest → newest (left → right).
        self._created_spark.data = [float(day.created) for _, day in self._days]
        self._completed_spark.data = [float(day.completed) for _, day in self._days]

    # -- actions ----------------------------------------------------------------------

    def action_toggle_period(self) -> None:
        """The "p" toggle: flip the window between 14 and 30 days (a
        different window needs a fresh, wider snapshot from disk)."""
        idx = _PERIODS.index(self._period)
        self._period = _PERIODS[(idx + 1) % len(_PERIODS)]
        self._load_days()
