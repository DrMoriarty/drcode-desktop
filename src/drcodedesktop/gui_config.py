"""GUI-level configuration (recent projects, appearance, notifications).

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
    """Load/save the shell's small JSON config (never fatal).

    Beyond the recents list it carries the Preferences-dialog state:

    - ``font_family`` / ``font_size`` — the single chat/queue font pair
      ("" / 0 = the system fixed font and its default size, i.e. the
      pre-Preferences behaviour);
    - ``notify_*`` — the system-tray notification checkboxes, one per
      pipeline event kind (all off by default: no tray icon, no
      messages until the user asks for them). They are independent of
      the project's voice_tts_* / ntfy settings.
    """

    def __init__(self) -> None:
        self.path = _gui_config_dir() / "gui.json"
        self.recent: list[str] = []
        self.last: str | None = None
        self.font_family: str = ""
        self.font_size: int = 0
        self.notify_completed: bool = False
        self.notify_tester_failed: bool = False
        self.notify_clarification: bool = False
        self.notify_pipeline: bool = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        recent = data.get("recent")
        if isinstance(recent, list):
            self.recent = [str(p) for p in recent]
        last = data.get("last")
        if isinstance(last, str):
            self.last = last
        family = data.get("font_family")
        if isinstance(family, str):
            self.font_family = family
        size = data.get("font_size")
        if isinstance(size, int) and not isinstance(size, bool):
            self.font_size = size
        for field in (
            "notify_completed",
            "notify_tester_failed",
            "notify_clarification",
            "notify_pipeline",
        ):
            value = data.get(field)
            if isinstance(value, bool):
                setattr(self, field, value)

    def remember_project(self, project_root: Path) -> None:
        """Push a project to the front of the recents list and persist."""
        entry = str(project_root)
        self.recent = [entry, *(p for p in self.recent if p != entry)]
        self.recent = self.recent[:MAX_RECENT]
        self.last = entry
        self.save()

    def save(self) -> None:
        """Persist the whole config (best-effort, never fatal)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "recent": self.recent,
                        "last": self.last,
                        "font_family": self.font_family,
                        "font_size": self.font_size,
                        "notify_completed": self.notify_completed,
                        "notify_tester_failed": self.notify_tester_failed,
                        "notify_clarification": self.notify_clarification,
                        "notify_pipeline": self.notify_pipeline,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            # Preferences are best-effort.
            pass

    # Historical alias (remember_project used to be the only writer).
    _save = save
