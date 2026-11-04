"""The provider-catalog editor dialog (plan task 10): the GUI twin of the
TUI's ``tui.model_setup_app.ModelCatalogScreen`` (mounted in the TUI as
"Provider catalog…").

Edits the user-level catalog (``~/.config/drcode/models.json``, see
``drcode.providers``) — together with the TUI screen, one of the only two
UIs that write that file, so no manual JSON editing is ever needed. The
dialog writes EXACTLY the file the TUI writes (via
``drcode.providers.UserModelsStore`` with its ``.bak`` fallback and the
atomic tmp+rename save), so GUI and TUI stay interchangeable on one
machine.

Ported semantics, one to one:

- CRUD of providers (name without ``/``, base URL, API key, cached
  ``health_check_url``) and of their models (``ModelEntry``: id,
  max_context_tokens, max_tokens-free — the id / context tokens /
  compaction-limit triple edited in the table below the connection form);
  renaming a provider or a model keeps the role-default references
  pointing at it; deleting a provider drops its role defaults;
- per agent role (coding / research / tester / compaction / resolver) a
  default model can be picked from the catalog — used when the project
  does not pin its own;
- pre-fill from legacy data (``providers.load_seeded_catalog``): when
  models.json does not exist yet, the dialog starts filled from the
  user-level drcode.json and the project's ``custom_models`` — in memory
  only, until the user presses Save (quitting without saving discards the
  seed);
- a live connection check (``agent_model.probe_model``, caching the
  verified ``health_check_url``) and one-click model auto-discovery
  (``agent_model.fetch_models`` — existing entries keep their manual
  edits, new ones are appended);
- the draft semantics of the TUI screen: nothing is persisted until the
  explicit Save (Cancel/ESC closes without writing anything); a
  validation error keeps the dialog open with the message shown.

On Save the dialog persists the catalog and, when a live shell is
attached, drops the per-role resolution cache — the project model
settings dialog (plan task 6) picks the new catalog up on its next
open, exactly like the TUI's ``_model_catalog_done``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from drcode.agent_model import ModelFetchError, fetch_models, probe_model
from drcode.providers import (
    USER_MODELS_FILENAME,
    AgentRole,
    ModelEntry,
    ModelRef,
    Provider,
    UserModelsStore,
    load_seeded_catalog,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from .core_shell import CoreShell

_PROBE_TIMEOUT = 15.0

_ROLES: tuple[AgentRole, ...] = tuple(AgentRole)

_INHERIT = "inherit"


class ModelCatalogDialog(QDialog):
    """Modal "Provider catalog" editor over ``UserModelsStore`` — draft
    semantics, explicit Save, Cancel/ESC writes nothing."""

    def __init__(
        self,
        shell: CoreShell | None = None,
        parent: QWidget | None = None,
        *,
        store: UserModelsStore | None = None,
        project_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._shell = shell
        self._store = store or UserModelsStore()
        self._project_root = (
            project_root
            if project_root is not None
            else (shell.project_root if shell is not None else None)
        )
        self.setWindowTitle("Provider catalog")
        self.setModal(True)
        self.setMinimumWidth(760)
        # Load the catalog, pre-filling it from the legacy user-level flat
        # config and the project's custom models when the catalog file
        # does not exist yet (one-time migration aid; in-memory only
        # until the user presses Save).
        self.catalog, self.seed_note = load_seeded_catalog(
            self._store, project_root=self._project_root
        )
        self._current: str | None = None
        self._selected_model: str | None = None
        # Guards against re-entrant commits while a fetch/probe runs.
        self._busy = False
        # Set while removing a model: the table rebuild fires
        # currentCellChanged for the next row, which must not refill the
        # editor (Remove always ends with empty inputs).
        self._suppress_cell = False

        layout = QVBoxLayout(self)
        self._title = QLabel(
            "Model catalog — OpenAI-compatible providers and their models. "
            f"Stored in {self._store.path.name} (user level)."
        )
        layout.addWidget(self._title)

        main = QHBoxLayout()
        layout.addLayout(main)

        # -- left column: providers + role defaults ---------------------------
        left = QVBoxLayout()
        main.addLayout(left, 0)
        left.addWidget(self._section_label("Providers"))
        self._provider_list = QListWidget()
        self._provider_list.itemSelectionChanged.connect(self._provider_row_changed)
        left.addWidget(self._provider_list)
        btns = QHBoxLayout()
        self._add_btn = QPushButton("+ Add")
        self._add_btn.clicked.connect(self._add_provider)
        self._del_provider_btn = QPushButton("Delete")
        self._del_provider_btn.clicked.connect(self._delete_provider)
        btns.addWidget(self._add_btn)
        btns.addWidget(self._del_provider_btn)
        left.addLayout(btns)

        left.addWidget(self._section_label("Role defaults"))
        grid = QGridLayout()
        self._role_combos: dict[AgentRole, QComboBox] = {}
        for row, role in enumerate(_ROLES):
            combo = QComboBox()
            self._role_combos[role] = combo
            grid.addWidget(QLabel(role.value), row, 0)
            grid.addWidget(combo, row, 1)
        left.addLayout(grid)
        left.addStretch(1)

        # -- right column: connection form + models ----------------------------
        right = QVBoxLayout()
        main.addLayout(right, 1)
        form = QGridLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("provider name, e.g. local-swap")
        self._url = QLineEdit()
        self._url.setPlaceholderText("base URL, e.g. http://localhost:8080/v1")
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("API key (optional — leave empty if not needed)")
        self._health = QLineEdit()
        self._health.setPlaceholderText("health-check URL (cached by Check connection)")
        for row, (label, widget) in enumerate(
            (
                ("Name", self._name),
                ("Base URL", self._url),
                ("API key", self._key),
                ("Health check URL", self._health),
            )
        ):
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(widget, row, 1)
        right.addLayout(form)

        check_row = QHBoxLayout()
        self._check_btn = QPushButton("Check connection")
        self._check_btn.clicked.connect(self._check_connection_clicked)
        self._fetch_btn = QPushButton("Fetch models")
        self._fetch_btn.clicked.connect(self._fetch_models_clicked)
        check_row.addWidget(self._check_btn)
        check_row.addWidget(self._fetch_btn)
        check_row.addStretch(1)
        right.addLayout(check_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        right.addWidget(self._status)

        right.addWidget(self._section_label("Models"))
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(("model id", "context", "compact limit"))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        # currentCellChanged fires on rebuilds too; _suppress_cell guards
        # the Remove path (the same swallow the TUI's RowHighlighted does).
        self._table.currentCellChanged.connect(self._table_cell_changed)
        right.addWidget(self._table)

        editor = QGridLayout()
        self._model_id = QLineEdit()
        self._model_id.setPlaceholderText("model id")
        self._ctx = QLineEdit()
        self._ctx.setPlaceholderText("context tokens")
        self._pct = QLineEdit()
        self._pct.setPlaceholderText("compact % (empty = inherit)")
        editor.addWidget(self._model_id, 0, 0)
        editor.addWidget(self._ctx, 0, 1)
        editor.addWidget(self._pct, 0, 2)
        self._model_id.returnPressed.connect(self._update_model)
        self._ctx.returnPressed.connect(self._update_model)
        self._pct.returnPressed.connect(self._update_model)
        right.addLayout(editor)

        model_btns = QHBoxLayout()
        self._update_btn = QPushButton("Update")
        self._update_btn.clicked.connect(self._update_model)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.clicked.connect(self._remove_model)
        self._add_model_btn = QPushButton("Add model")
        self._add_model_btn.clicked.connect(self._add_model)
        model_btns.addWidget(self._update_btn)
        model_btns.addWidget(self._remove_btn)
        model_btns.addWidget(self._add_model_btn)
        model_btns.addStretch(1)
        right.addLayout(model_btns)

        # -- footer --------------------------------------------------------------
        footer = QHBoxLayout()
        hint = QLabel(
            "Cancel closes without saving · selecting a table row fills the "
            "editor; Update writes the edited id/context/limit back, Add "
            "model creates a new model from the same fields, Remove deletes it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888;")
        footer.addWidget(hint, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self._save_btn = QPushButton("Save")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self._save)
        footer.addWidget(self._save_btn)
        layout.addLayout(footer)

        # -- initial state (the TUI's on_mount) ------------------------------
        if self._store.last_warning:
            self._status.setText(f"⚠ {self._store.last_warning}")
        if self.catalog.providers and self._current is None:
            self._current = self.catalog.providers[0].name
        self._refresh_providers()
        self._refresh_roles()
        self._load_form(self._current)
        if self._store.last_warning:
            # Re-assert after _load_form (it clears the status line).
            self._status.setText(f"⚠ {self._store.last_warning}")
        elif self.seed_note:
            self._status.setText(f"ⓘ {self.seed_note}")

    # -- construction helpers -------------------------------------------------------

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold;")
        return label

    # -- draft helpers ----------------------------------------------------------------

    def _provider(self) -> Provider | None:
        if self._current is None:
            return None
        return next(
            (p for p in self.catalog.providers if p.name == self._current), None
        )

    def _current_model_entry(self) -> ModelEntry | None:
        provider = self._provider()
        model_id = self._selected_model
        if provider is None or model_id is None:
            return None
        return next((m for m in provider.models if m.id == model_id), None)

    def _status_msg(self, text: str) -> None:
        self._status.setText(text)

    def _commit_form(self) -> bool:
        """Flush the provider form into the catalog draft. False on a
        validation error (the status line explains) — the TUI screen's
        same contract."""
        provider = self._provider()
        if provider is None:
            return True
        name = self._name.text().strip()
        url = self._url.text().strip()
        key = self._key.text().strip()
        health = self._health.text().strip()
        if not name:
            self._status_msg("✗ provider name must not be empty")
            return False
        if "/" in name:
            self._status_msg("✗ provider name must not contain '/'")
            return False
        if not url:
            self._status_msg("✗ base URL must not be empty")
            return False
        if name != provider.name and any(
            p.name == name for p in self.catalog.providers
        ):
            self._status_msg(f"✗ provider '{name}' already exists")
            return False
        if name != provider.name:
            # Rename: keep the role-default references pointing at the
            # same provider.
            old = provider.name
            provider.name = name
            for ref in self.catalog.defaults.values():
                if ref is not None and ref.provider == old:
                    ref.provider = name
            self._current = name
            # The role combos list "provider/model" options: rebuild them,
            # renaming the picks that referenced the old provider name.
            picks = {
                role: (combo.currentData() or "")
                for role, combo in self._role_combos.items()
            }
            self._refresh_roles()
            for role, value in picks.items():
                if value:
                    if value.startswith(f"{old}/"):
                        value = f"{name}/{value.partition('/')[2]}"
                    combo = self._role_combos[role]
                    index = combo.findData(value)
                    combo.setCurrentIndex(index if index >= 0 else 0)
        provider.base_url = url
        provider.api_key = key
        provider.health_check_url = health
        return True

    # -- refresh -----------------------------------------------------------------------

    def _refresh_providers(self) -> None:
        self._provider_list.blockSignals(True)
        self._provider_list.clear()
        for provider in self.catalog.providers:
            self._provider_list.addItem(
                f"{provider.name}  ({len(provider.models)} models)"
            )
        if self._current is not None:
            names = [p.name for p in self.catalog.providers]
            if self._current in names:
                self._provider_list.setCurrentRow(names.index(self._current))
        self._provider_list.blockSignals(False)

    def _refresh_models(self) -> None:
        self._suppress_cell = True
        self._table.setRowCount(0)
        provider = self._provider()
        if provider is not None:
            for entry in provider.models:
                row = self._table.rowCount()
                self._table.insertRow(row)
                pct = entry.compaction_threshold
                self._table.setItem(row, 0, QTableWidgetItem(entry.id))
                self._table.setItem(
                    row, 1, QTableWidgetItem(str(entry.max_context_tokens))
                )
                self._table.setItem(
                    row, 2, QTableWidgetItem(_INHERIT if pct is None else f"{pct}%")
                )
        self._suppress_cell = False

    def _refresh_roles(self) -> None:
        model_refs = [
            f"{provider.name}/{entry.id}"
            for provider in self.catalog.providers
            for entry in provider.models
        ]
        for role in _ROLES:
            combo = self._role_combos[role]
            previous = combo.currentData() or ""
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(f"{role.value}: (none)", "")
            for ref in model_refs:
                combo.addItem(ref, ref)
            # Keep the user's (possibly not yet saved) pick when it is
            # still addressable; otherwise fall back to the catalog's
            # saved default for the role.
            if previous and combo.findData(previous) >= 0:
                combo.setCurrentIndex(combo.findData(previous))
            else:
                ref = self.catalog.defaults.get(role)
                saved = f"{ref.provider}/{ref.model}" if ref else ""
                index = combo.findData(saved)
                combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _load_form(self, name: str | None) -> None:
        self._current = name
        self._selected_model = None
        provider = self._provider()
        self._name.setText(provider.name if provider else "")
        self._url.setText(provider.base_url if provider else "")
        self._key.setText(provider.api_key if provider else "")
        self._health.setText(provider.health_check_url if provider else "")
        self._clear_model_inputs()
        self._refresh_models()
        self._status_msg("")

    # -- provider list events ----------------------------------------------------------

    def _provider_row_changed(self) -> None:
        items = self._provider_list.selectedItems()
        if not items:
            return
        text = items[0].text()
        name = text.partition("  (")[0]
        if name == self._current:
            return
        if not self._commit_form():
            # Re-select the committed provider (the form refused the
            # switch); block signals to avoid recursion.
            self._refresh_providers()
            return
        self._load_form(name)

    def _add_provider(self) -> None:
        if self._busy or not self._commit_form():
            return
        base = "new-provider"
        name = base
        n = 2
        while any(p.name == name for p in self.catalog.providers):
            name = f"{base}-{n}"
            n += 1
        self.catalog.providers.append(Provider(name=name, base_url=""))
        self._refresh_providers()
        self._load_form(name)
        self._name.setFocus()

    def _delete_provider(self) -> None:
        if self._busy or not self._commit_form():
            return
        provider = self._provider()
        if provider is None:
            return
        self.catalog.providers = [
            p for p in self.catalog.providers if p.name != provider.name
        ]
        for role in _ROLES:
            ref = self.catalog.defaults.get(role)
            if ref is not None and ref.provider == provider.name:
                self.catalog.defaults.pop(role, None)
        self._current = (
            self.catalog.providers[0].name if self.catalog.providers else None
        )
        self._refresh_providers()
        self._refresh_roles()
        self._load_form(self._current)

    # -- connection check / model discovery --------------------------------------------

    def _check_connection_clicked(self) -> None:
        if self._busy:
            return
        job = asyncio.ensure_future(self._check_connection())
        del job  # fire-and-forget; exceptions surface in the status line

    def _fetch_models_clicked(self) -> None:
        if self._busy:
            return
        job = asyncio.ensure_future(self._fetch_models())
        del job

    async def _check_connection(self) -> None:
        """The TUI screen's connection check: ``probe_model`` with the
        first model when one exists (caching the verified endpoint),
        ``fetch_models`` as the empty-catalog fallback."""
        if not self._commit_form():
            return
        provider = self._provider()
        if provider is None:
            return
        self._busy = True
        try:
            self._status_msg("Checking connection…")
            if provider.models:
                ok, detail, checked = await probe_model(
                    provider.models[0].id,
                    provider.base_url,
                    provider.api_key,
                    timeout=_PROBE_TIMEOUT,
                    health_check_url=provider.health_check_url,
                )
                if ok:
                    provider.health_check_url = checked
                    self._health.setText(checked)
                    self._status_msg(f"✓ connection ok (via {checked})")
                else:
                    self._status_msg(f"✗ connection failed: {detail}")
            else:
                try:
                    entries = await fetch_models(provider.base_url, provider.api_key)
                except ModelFetchError as e:
                    self._status_msg(f"✗ connection failed: {e}")
                else:
                    self._status_msg(
                        f"✓ connection ok — {len(entries)} models available "
                        "(use “Fetch models” to import them)"
                    )
        finally:
            self._busy = False

    async def _fetch_models(self) -> None:
        """Auto-discovery: existing entries keep their manual edits, new
        ones are appended (the TUI screen's same merge rule)."""
        if not self._commit_form():
            return
        provider = self._provider()
        if provider is None:
            return
        self._busy = True
        try:
            self._status_msg("Fetching models…")
            try:
                entries = await fetch_models(provider.base_url, provider.api_key)
            except ModelFetchError as e:
                self._status_msg(f"✗ fetch failed: {e}")
                return
            existing = {m.id for m in provider.models}
            fresh = [e for e in entries if e.id not in existing]
            provider.models.extend(fresh)
            self._refresh_models()
            self._refresh_providers()
            self._refresh_roles()
            self._status_msg(f"✓ fetched {len(entries)} models ({len(fresh)} new)")
        finally:
            self._busy = False

    # -- model table -------------------------------------------------------------------

    def _table_cell_changed(self, row: int, _col: int, _pr: int, _pc: int) -> None:
        if self._suppress_cell or row < 0:
            return
        item = self._table.item(row, 0)
        if item is not None:
            self._select_model(item.text())

    def _select_model(self, model_id: str) -> None:
        """Fill the editor line with the selected model's actual values."""
        provider = self._provider()
        entry = (
            next((m for m in provider.models if m.id == model_id), None)
            if provider is not None
            else None
        )
        if entry is None:
            return
        self._selected_model = model_id
        self._model_id.setText(entry.id)
        self._ctx.setText(str(entry.max_context_tokens))
        pct = entry.compaction_threshold
        self._pct.setText("" if pct is None else str(pct))

    def _clear_model_inputs(self) -> None:
        self._model_id.setText("")
        self._ctx.setText("")
        self._pct.setText("")

    def _parse_caps(self) -> tuple[int, int | None] | None:
        """Parse the context / compaction-limit editor fields into
        (context_tokens, compaction_threshold); None on a validation
        error (the status line explains)."""
        raw_ctx = self._ctx.text().strip()
        try:
            ctx = int(raw_ctx)
        except ValueError:
            self._status_msg("✗ context tokens must be a positive integer")
            return None
        if ctx <= 0:
            self._status_msg("✗ context tokens must be a positive integer")
            return None
        raw_pct = self._pct.text().strip()
        pct: int | None = None
        if raw_pct:
            try:
                pct = int(raw_pct)
            except ValueError:
                self._status_msg("✗ compact % must be an integer between 1 and 100")
                return None
            if not 1 <= pct <= 100:
                self._status_msg("✗ compact % must be an integer between 1 and 100")
                return None
        return ctx, pct

    def _update_model(self) -> None:
        """Write the edited id / context / compaction limit back into the
        selected model."""
        entry = self._current_model_entry()
        if entry is None:
            self._status_msg("Select a model row first (click a table row).")
            return
        provider = self._provider()
        assert provider is not None
        new_id = self._model_id.text().strip() or entry.id
        if "/" in new_id:
            self._status_msg("✗ model id must not contain '/'")
            return
        caps = self._parse_caps()
        if caps is None:
            return
        ctx, pct = caps
        if new_id != entry.id:
            if any(m.id == new_id for m in provider.models):
                self._status_msg(f"✗ model '{new_id}' already exists")
                return
            # Rename: keep the role-default references pointing at the
            # same model.
            old = entry.id
            entry.id = new_id
            for ref in self.catalog.defaults.values():
                if (
                    ref is not None
                    and ref.provider == provider.name
                    and ref.model == old
                ):
                    ref.model = new_id
            self._selected_model = new_id
        if ctx != entry.max_context_tokens:
            entry.max_context_tokens = ctx
        entry.compaction_threshold = pct
        self._refresh_models()
        self._refresh_roles()
        limit = f"{pct}% of context" if pct is not None else "inherits"
        self._status_msg(f"✓ {entry.id}: context {ctx}, compaction {limit}")

    def _remove_model(self) -> None:
        provider = self._provider()
        entry = self._current_model_entry()
        if provider is None or entry is None:
            self._status_msg("Select a model row first (click a table row).")
            return
        provider.models = [m for m in provider.models if m.id != entry.id]
        self._refresh_models()
        self._refresh_roles()
        self._selected_model = None
        # The rebuild fires currentCellChanged for the next row — swallow
        # it, so Remove always ends with empty editor inputs.
        self._suppress_cell = True
        self._clear_model_inputs()
        self._suppress_cell = False
        self._status_msg(f"✓ removed {entry.id}")

    def _add_model(self) -> None:
        """Create a new model from the editor fields (id, context, limit)
        — the same inputs the row selection fills for editing."""
        if not self._commit_form():
            return
        provider = self._provider()
        if provider is None:
            return
        model_id = self._model_id.text().strip()
        if not model_id:
            self._status_msg("Enter a model id to add.")
            return
        if "/" in model_id:
            self._status_msg("✗ model id must not contain '/'")
            return
        if any(m.id == model_id for m in provider.models):
            self._status_msg(f"✗ model '{model_id}' already exists")
            return
        caps = self._parse_caps()
        if caps is None:
            return
        ctx, pct = caps
        provider.models.append(
            ModelEntry(
                id=model_id,
                max_context_tokens=ctx,
                compaction_threshold=pct,
            )
        )
        self._selected_model = model_id
        self._refresh_models()
        self._refresh_providers()
        self._refresh_roles()
        limit = f"{pct}% of context" if pct is not None else "inherits"
        self._status_msg(f"✓ added {model_id}: context {ctx}, compaction {limit}")

    # -- save --------------------------------------------------------------------------

    def _apply_role_defaults(self) -> None:
        """Read the role combos into the draft's defaults (empty = no
        default for the role)."""
        for role in _ROLES:
            value = self._role_combos[role].currentData() or ""
            if value:
                provider_name, _, model_id = value.partition("/")
                self.catalog.defaults[role] = ModelRef(
                    provider=provider_name, model=model_id
                )
            else:
                self.catalog.defaults.pop(role, None)

    def _save(self) -> bool:
        """Persist the draft through the TUI's own store (atomic write,
        ``.bak`` backup of the previous version) and drop the shell's
        per-role resolution cache. A validation/write error keeps the
        dialog open with the message shown. Returns True when the dialog
        should close."""
        if self._busy or not self._commit_form():
            return False
        self._apply_role_defaults()
        try:
            self._store.save(self.catalog)
        except (OSError, ValueError) as e:
            self._status_msg(f"✗ could not write {USER_MODELS_FILENAME}: {e}")
            return False
        self._status_msg(f"✓ saved to {self._store.path}")
        if self._shell is not None:
            # The project model-settings dialog (plan task 6) reads the
            # catalog on open; the live per-role resolutions must be
            # recomputed against the new file (the TUI's
            # _model_catalog_done does the same).
            self._shell.invalidate_resolved_configs()
        self.accept()
        return True
