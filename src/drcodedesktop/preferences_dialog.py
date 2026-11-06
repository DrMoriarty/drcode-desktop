"""The shell's own Preferences dialog (GUI appearance + tray
notifications) — deliberately separate from the project's drcode
settings (the Settings menu dialogs):

- Font: ONE family+size pair shared by the task window (the chat pane)
  and the task list (the Queue tree). Empty family / zero size mean the
  system fixed font at its default size — the pre-Preferences look.
- Notifications: four checkboxes, one per pipeline event kind (task
  completed, tester failure, clarification, queue empty / pipeline
  blocked). They drive the GUI's own system-tray channel and are
  completely independent of the project's voice_tts_* / ntfy settings.

OK and Apply persist into ``gui.json`` (GuiConfig) and emit
:attr:`applied` — the window re-styles the open views and refreshes the
tray icon without reopening the project. Cancel discards.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QGroupBox,
    QHBoxLayout,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .gui_config import GuiConfig

_MIN_FONT_SIZE = 6
_MAX_FONT_SIZE = 32


def chat_font(config: GuiConfig) -> QFont:
    """The effective shared chat/queue font: the configured family and
    size over the system fixed font defaults."""
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if config.font_family:
        font.setFamily(config.font_family)
    if config.font_size > 0:
        font.setPointSize(config.font_size)
    return font


class PreferencesDialog(QDialog):
    """Modal form: the shared font pair + the tray-notification
    checkboxes; OK / Apply persist, Cancel discards."""

    #: Emitted after the form was written into the config (by OK or
    #: Apply) — the window re-applies font and tray state on it.
    applied = Signal()

    def __init__(self, config: GuiConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(420)
        self._config = config

        layout = QVBoxLayout(self)

        font_box = QGroupBox("Font (task window and task list)")
        font_layout = QVBoxLayout(font_box)
        row = QHBoxLayout()
        self._family = QFontComboBox()
        if config.font_family:
            self._family.setCurrentFont(QFont(config.font_family))
        self._size = QSpinBox()
        self._size.setRange(_MIN_FONT_SIZE, _MAX_FONT_SIZE)
        self._size.setSpecialValueText("default")
        initial = config.font_size if config.font_size > 0 else _MIN_FONT_SIZE
        self._size.setValue(initial)
        row.addWidget(self._family, stretch=1)
        row.addWidget(self._size)
        font_layout.addLayout(row)
        layout.addWidget(font_box)

        notify_box = QGroupBox("System notifications (tray)")
        notify_layout = QVBoxLayout(notify_box)
        self._notify_checks = {
            "notify_completed": QCheckBox("Task completed"),
            "notify_tester_failed": QCheckBox(
                "Tester failed (task returned to the queue)"
            ),
            "notify_clarification": QCheckBox("Task needs clarification"),
            "notify_pipeline": QCheckBox("All tasks done / pipeline blocked"),
        }
        for field, check in self._notify_checks.items():
            check.setChecked(getattr(config, field))
            notify_layout.addWidget(check)
        layout.addWidget(notify_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self._on_button)
        layout.addWidget(buttons)

    # -- button plumbing -----------------------------------------------------------

    def _on_button(self, button: QAbstractButton) -> None:
        """The Apply button of the button box (OK/Cancel are wired to
        accepted/rejected separately)."""
        role = QDialogButtonBox().buttonRole(button)
        if role == QDialogButtonBox.ButtonRole.ApplyRole:
            self._apply()

    def _on_ok(self) -> None:
        self._apply()
        self.accept()

    def _apply(self) -> None:
        """Write the form into the config, persist, announce."""
        self._config.font_family = self._family.currentFont().family()
        size = self._size.value()
        # The special "default" value (= the minimum) means: keep the
        # font's own default size (0 in the config).
        self._config.font_size = size if size > _MIN_FONT_SIZE else 0
        for field, check in self._notify_checks.items():
            setattr(self._config, field, check.isChecked())
        self._config.save()
        self.applied.emit()
