"""GUI-level configuration (recent projects etc.).

Deliberately separate from drcode's own config layers: this is the
shell's private state, persisted as one JSON file in the GUI config
directory (`~/.config/drcode-desktop/gui.json`, XDG-aware and
relocatable via `DRCODE_DESKTOP_CONFIG_DIR`).
"""

from __future__ import annotations

import json
from pathlib import Path

from .event_hub import _gui_config_dir

MAX_RECENT = 10


class GuiConfig:
    """Load/save the shell's small JSON config (never fatal)."""

    def __init__(self) -> None:
        self.path = _gui_config_dir() / "gui.json"
        self.recent: list[str] = []
        self.last: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            recent = data.get("recent")
            if isinstance(recent, list):
                self.recent = [str(p) for p in recent]
            last = data.get("last")
            if isinstance(last, str):
                self.last = last

    def remember_project(self, project_root: Path) -> None:
        """Push a project to the front of the recents list and persist."""
        entry = str(project_root)
        self.recent = [entry, *(p for p in self.recent if p != entry)]
        self.recent = self.recent[:MAX_RECENT]
        self.last = entry
        self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"recent": self.recent, "last": self.last}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            # Preferences are best-effort.
            pass
