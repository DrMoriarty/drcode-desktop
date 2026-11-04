"""Notifications settings dialog (plan task 7) — the GUI port of
``drcode.tui.notifications_dialog`` (ntfy.sh push notifications).

The form collects:

- Enabled (a check box),
- the ntfy.sh topic name to publish to (a plain input).

Save persists the values into the project settings
(``<project>/.drcode/settings.json`` — the very file the TUI writes);
Cancel/close saves nothing. The dialog never probes the network
(publishing a probe message would push a real notification to every
subscriber of the topic): instead, the status line below the form
live-renders whether the current combination would actually deliver
anything, warning about a missing topic when Enabled is on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

_OK_COLOR = "#98c379"
_ERROR_COLOR = "#e06c75"
_DIM_COLOR = "#6b7280"


@dataclass
class NotificationsSetup:
    """Values collected by the dialog (handed back to the window on
    Save — the TUI dialog's dismiss payload)."""

    enabled: bool
    topic: str


class NotificationsDialog(QDialog):
    """Modal form: enable switch, ntfy.sh topic, Save / Cancel."""

    def __init__(
        self, enabled: bool = False, topic: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Notifications")
        self.setMinimumWidth(460)
        self._initial_enabled = enabled
        self._initial_topic = topic

        note = QLabel(
            "Push notifications about important events (task status "
            "changes, GitHub sync, an idle pipeline) published to the "
            "public <b>ntfy.sh</b> service. Subscribe to the same topic "
            "in the ntfy app to receive them. Anyone who knows the "
            "topic can read its messages — pick an unguessable one."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {_DIM_COLOR};")

        self._enabled = QCheckBox("Enabled")
        self._enabled.setChecked(self._initial_enabled)
        self._topic = QLineEdit(self._initial_topic)
        self._topic.setPlaceholderText("my-secret-drcode-topic")
        self._status = QLabel()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self._enabled)
        layout.addWidget(QLabel("ntfy.sh topic"))
        layout.addWidget(self._topic)
        layout.addWidget(self._status)
        layout.addWidget(buttons)

        self._enabled.toggled.connect(lambda _on: self._refresh_status())
        self._topic.textChanged.connect(lambda _text: self._refresh_status())
        self._refresh_status()

    # -- live delivery hint ------------------------------------------------------

    @property
    def setup(self) -> NotificationsSetup:
        """The currently collected values (the Save payload)."""
        return NotificationsSetup(
            enabled=self._enabled.isChecked(),
            topic=self._topic.text().strip(),
        )

    def _refresh_status(self) -> None:
        """Whether the current combination would actually deliver
        anything (no network probe — the TUI dialog's status line)."""
        enabled = self._enabled.isChecked()
        topic = self._topic.text().strip()
        if not enabled:
            self._status.setStyleSheet(f"color: {_DIM_COLOR};")
            self._status.setText("Notifications are off — nothing will be sent.")
            return
        if not topic:
            self._status.setStyleSheet(f"color: {_ERROR_COLOR};")
            self._status.setText("✗ Enabled, but no topic given: nothing will be sent.")
            return
        self._status.setStyleSheet(f"color: {_OK_COLOR};")
        self._status.setText(f"✓ Events will be published to ntfy.sh/{topic}.")
