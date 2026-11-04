"""The project model-settings dialog (plan task 6): the GUI twin of the
TUI's ``tui.project_model_settings_dialog`` — the single modal form for
the per-role model selection and per-role ``max_tokens`` overrides.

One section per agent role (coding / research / tester / compaction /
resolver):

- a model combo offering the user catalog's models
  (``~/.config/drcode/models.json``, providers as separators — the same
  "<provider>/<model-id>" reference format the TUI persists), the
  role's saved custom model ("Custom: <model-id>") and "User default"
  (drop the project slot — available for EVERY role);
- an inline custom-model form (model / base_url / api_key /
  health_check_url / max_context_tokens / max_tokens) shown while the
  custom choice is active, mirroring the six connection fields of
  ``drcode.providers.CustomModelEntry``;
- a max_tokens override input: empty = inherit (the catalog entry's cap
  or the flat-config default — the override key is removed), a value = a
  positive-integer override that beats the more general layers
  (``providers.resolve`` applies it last).

Works on a DRAFT: nothing touches the config files until Apply.
On Apply the collected slots / custom entries / overrides go to
:meth:`drcodedesktop.core_shell.CoreShell.apply_model_settings`, which
writes the very files the TUI writes (``.drcode/drcode.json`` via
``providers.set_project_*``, ``role_max_tokens`` via
``ProjectSettingsStore``) and invalidates the per-role resolution cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from drcode.providers import (
    AgentRole,
    CustomModelEntry,
    ModelRef,
    Provider,
    UserModelsStore,
    load_project_custom_models,
    load_project_model_slots,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .core_shell import CoreShell

# Sentinel values of the model combo (the TUI dialog's same idea); the
# catalog references ride as their own "provider/model" strings.
_CUSTOM = "__custom__"
_USER_DEFAULT = "__user_default__"

_ROLE_TITLES: dict[AgentRole, str] = {
    AgentRole.CODING: "Coding",
    AgentRole.RESEARCH: "Research",
    AgentRole.TESTER: "Tester",
    AgentRole.COMPACTION: "Compaction",
    AgentRole.RESOLVER: "Resolver",
}

THINKING_HINT = (
    "max_tokens override: empty = inherit (the catalog entry's cap or the "
    "flat-config default). Thinking models spend their reasoning tokens "
    "from the same output budget — for the tester/resolver roles set it "
    "noticeably higher (16–32k)."
)


def catalog_options(providers: list[Provider]) -> list[tuple[str, str | None]]:
    """The combo's (text, data) catalog entries, providers as separators
    (data None — Qt skips separators in findData, so they can never be
    selected)."""
    out: list[tuple[str, str | None]] = []
    for provider in providers:
        if out:
            out.append(("", None))  # separator row
        for model in provider.models:
            out.append((f"{provider.name}/{model.id}", f"{provider.name}/{model.id}"))
    return out


class _RoleSection:
    """The widgets of one agent role: the model combo, the inline
    custom-model form and the max_tokens override input."""

    def __init__(
        self,
        options: list[tuple[str, str | None]],
        slot: ModelRef | str | None,
        custom: CustomModelEntry | None,
        override: str,
        effective: str,
        on_change: Callable[[], None],
    ) -> None:
        self.on_change = on_change

        self.combo = QComboBox()
        self.combo.addItem("User default", _USER_DEFAULT)
        for text, data in options:
            if data is None:
                self.combo.insertSeparator(self.combo.count())
            else:
                self.combo.addItem(text, data)
        self.combo.addItem(
            f"Custom: {custom.model}" if custom is not None else "Custom…", _CUSTOM
        )
        if isinstance(slot, ModelRef):
            if self.combo.findData(str(slot)) < 0:
                # A dangling reference (its provider left the catalog):
                # keep the selection visible so Apply re-persists it
                # verbatim instead of silently rewriting the user's slot.
                self.combo.addItem(str(slot), str(slot))
            self._set_current(str(slot))
        elif slot == "custom":
            self._set_current(_CUSTOM)
        else:
            self._set_current(_USER_DEFAULT)

        self.custom_box = QGroupBox("Custom model (free-form connection)")
        form = QGridLayout(self.custom_box)
        self.custom_model = QLineEdit()
        self.custom_base_url = QLineEdit()
        self.custom_api_key = QLineEdit()
        self.custom_health = QLineEdit()
        self.custom_context = QSpinBox()
        self.custom_context.setRange(1, 100_000_000)
        self.custom_context.setSingleStep(1000)
        self.custom_max_tokens = QLineEdit()
        self.custom_max_tokens.setPlaceholderText("inherit")
        for row, (label, widget) in enumerate(
            (
                ("Model", self.custom_model),
                ("Base URL", self.custom_base_url),
                ("API key", self.custom_api_key),
                ("Health check URL", self.custom_health),
                ("Max context tokens", self.custom_context),
                ("Max tokens", self.custom_max_tokens),
            )
        ):
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(widget, row, 1)
        if custom is not None:
            self.custom_model.setText(custom.model)
            self.custom_base_url.setText(custom.base_url)
            self.custom_api_key.setText(custom.api_key)
            self.custom_health.setText(custom.health_check_url)
            self.custom_context.setValue(custom.max_context_tokens)
            self.custom_max_tokens.setText(
                "" if custom.max_tokens is None else str(custom.max_tokens)
            )
        else:
            self.custom_context.setValue(200_000)

        self.override = QLineEdit(override)
        self.override.setPlaceholderText("inherit")

        self.effective_label = QLabel(f"effective now: {effective or 'not set'}")

        self.combo.currentIndexChanged.connect(self._combo_changed)
        self._sync_custom_visibility()

    # -- combo state -----------------------------------------------------------

    def _set_current(self, data: str) -> None:
        index = self.combo.findData(data)
        if index >= 0:
            self.combo.setCurrentIndex(index)

    @property
    def slot_value(self) -> ModelRef | str | None:
        """The draft slot of the role: a ModelRef, the string "custom",
        or None = "User default" (the exact value forms of
        ``drcode.providers.load_project_model_slots``)."""
        data = self.combo.currentData()
        if data == _USER_DEFAULT:
            return None
        if data == _CUSTOM:
            return "custom"
        assert isinstance(data, str)
        provider, sep, model = data.partition("/")
        assert sep  # the item was built from a validated catalog pair
        return ModelRef(provider=provider, model=model)

    def is_custom(self) -> bool:
        return self.combo.currentData() == _CUSTOM

    def custom_entry(self) -> CustomModelEntry | None:
        """The inline form as a CustomModelEntry, or None when the form
        does not describe a usable connection (missing model / base_url,
        a bad max_tokens) — the caller keeps the dialog open and shows
        the error."""
        model = self.custom_model.text().strip()
        base_url = self.custom_base_url.text().strip()
        if not model or not base_url:
            return None
        max_tokens_text = self.custom_max_tokens.text().strip()
        max_tokens: int | None = None
        if max_tokens_text:
            try:
                max_tokens = int(max_tokens_text)
            except ValueError:
                return None
            if max_tokens <= 0:
                return None
        return CustomModelEntry(
            model=model,
            base_url=base_url,
            api_key=self.custom_api_key.text().strip(),
            health_check_url=self.custom_health.text().strip(),
            max_context_tokens=self.custom_context.value(),
            max_tokens=max_tokens,
        )

    def override_value(self) -> int | str | None:
        """The max_tokens override: None = inherit, a positive int, or
        the string "invalid" when the text is not a usable number."""
        text = self.override.text().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return "invalid"
        return value if value > 0 else "invalid"

    # -- reactions ----------------------------------------------------------------

    def _combo_changed(self) -> None:
        self._sync_custom_visibility()
        self.on_change()

    def _sync_custom_visibility(self) -> None:
        self.custom_box.setVisible(self.is_custom())


class ModelSettingsDialog(QDialog):
    """Modal "Project model settings" form over
    ``CoreShell.apply_model_settings`` — one section per role, draft
    semantics, Apply / Cancel."""

    def __init__(self, shell: CoreShell, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shell = shell
        self._project_root = shell.project_root
        self.setWindowTitle("Project model settings")
        self.setModal(True)
        self.setMinimumWidth(560)

        catalog = UserModelsStore().load()
        options = catalog_options(catalog.providers)
        slots = load_project_model_slots(self._project_root)
        customs = load_project_custom_models(self._project_root)
        # The overrides are merged against the STORED file by the shell
        # seam; the dialog only needs the current values to pre-fill.
        stored_overrides = shell.settings_store.load().role_max_tokens

        layout = QVBoxLayout(self)
        self._note = QLabel("")
        self._note.setStyleSheet("color: #cd3131;")
        self._sections: dict[AgentRole, _RoleSection] = {}

        for role in AgentRole:
            section = _RoleSection(
                options=options,
                slot=slots.get(role),
                custom=customs.get(role),
                override=str(stored_overrides.get(role.value, "")),
                effective=self._effective_label(role),
                on_change=self._clear_note,
            )
            self._sections[role] = section
            layout.addWidget(self._role_group(role, section))

        hint = QLabel(THINKING_HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        layout.addWidget(hint)
        layout.addWidget(self._note)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        apply = QPushButton("Apply")
        apply.setDefault(True)
        apply.clicked.connect(self._apply)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

    # -- construction helpers ------------------------------------------------------

    def _effective_label(self, role: AgentRole) -> str:
        """The role's currently effective model (the TUI dialog gets the
        resolved configs from the actions layer — same source here)."""
        resolved = self._shell.resolved_config(role)
        key = self._shell.resolved_model_key(role)
        if not resolved.is_ready:
            return ""
        return key or resolved.model

    def _role_group(self, role: AgentRole, section: _RoleSection) -> QGroupBox:
        group = QGroupBox(_ROLE_TITLES[role])
        grid = QGridLayout(group)
        grid.addWidget(QLabel("Model:"), 0, 0)
        grid.addWidget(section.combo, 0, 1)
        grid.addWidget(section.custom_box, 1, 0, 1, 2)
        grid.addWidget(QLabel("max_tokens:"), 2, 0)
        grid.addWidget(section.override, 2, 1)
        grid.addWidget(section.effective_label, 3, 0, 1, 2)
        return group

    # -- apply -----------------------------------------------------------------------

    def _clear_note(self) -> None:
        self._note.setText("")

    def _apply(self) -> bool:
        """Validate the draft and hand it to the shell seam. A validation
        error keeps the dialog open with the message shown (the TUI
        dialog's same contract). Returns True when the dialog should
        close."""
        self._clear_note()
        slots: dict[AgentRole, ModelRef | str | None] = {}
        customs: dict[AgentRole, CustomModelEntry] = {}
        overrides: dict[AgentRole, int] = {}
        for role, section in self._sections.items():
            slot = section.slot_value
            if slot == "custom":
                entry = section.custom_entry()
                if entry is None:
                    self._note.setText(
                        f"{_ROLE_TITLES[role]}: the custom model needs a "
                        "model id and a base URL (max_tokens must be a "
                        "positive integer when set)."
                    )
                    return False
                customs[role] = entry
            slots[role] = slot
            override = section.override_value()
            if override == "invalid":
                self._note.setText(
                    f"{_ROLE_TITLES[role]}: max_tokens must be a positive "
                    "integer (empty = inherit)."
                )
                return False
            if override is not None:
                assert isinstance(override, int)
                overrides[role] = override
        self._shell.apply_model_settings(slots, customs, overrides)
        self.accept()
        return True
