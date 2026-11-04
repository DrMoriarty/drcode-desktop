"""Plan task 10 acceptance tests: the provider-catalog editor from the GUI.

The dialog drives the exact production seam the TUI screen drives —
``drcode.providers.UserModelsStore.save`` — and the assertions are made
against the CONFIG FILES THEMSELVES (the same ``models.json`` bytes the
TUI writes), so GUI↔TUI interchangeability on one machine is proven by
construction:

- when models.json does not exist yet, the dialog pre-fills from the
  legacy user-level drcode.json and the project's ``custom_models``
  (``load_seeded_catalog``) — in memory only, until the explicit Save;
- Save writes models.json in the TUI format through the store's atomic
  tmp+rename write, backing the previous version up to ``.bak``;
  Cancel/ESC (reject) writes nothing;
- after a Save the live shell drops its per-role resolution cache — the
  project model-settings dialog (plan task 6) picks the new catalog up
  on its next open;
- validation (name/model-id without "/", duplicates, numeric caps) and
  the optional probe / fetch-models helpers behave like the TUI screen's.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from drcode.config import Config
from drcode.providers import (
    AgentRole,
    ModelEntry,
    ModelRef,
    Provider,
    UserModelsConfig,
    UserModelsStore,
    resolve,
)
from PySide6.QtWidgets import QApplication, QDialog
from qasync import QEventLoop

from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.model_catalog_dialog import ModelCatalogDialog
from drcodedesktop.model_settings_dialog import ModelSettingsDialog
from drcodedesktop.qt_host import QtHost
from tests.test_user_input import EventSink

Setup = Callable[[CoreShell], None]
Scenario = Callable[..., Any]

# The QApplication must outlive every widget the tests create (the
# test_user_input harness convention).
_APP: QApplication | None = None


def run_catalog_scenario(
    tmp_path: Path, scenario: Scenario, setup: Setup | None = None
) -> tuple[CoreShell, EventSink]:
    """The widget-test harness (the test_settings_models variant): the
    shell runs on a qasync loop, `setup` applies right after start, and
    `scenario` drives the dialog directly (it never exec — `_save` /
    `reject` are invoked as plain methods). An async scenario coroutine
    is awaited on the same loop (the probe/fetch paths)."""
    global _APP
    app: QApplication = QApplication.instance() or QApplication([])  # type: ignore[assignment]
    _APP = app
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    project_root = tmp_path / "proj"
    project_root.mkdir(exist_ok=True)  # a test may pre-seed .drcode files
    hub = EventHub(log_path=None)
    sink = EventSink(hub)
    shell = CoreShell(project_root, QtHost(hub), hub)

    async def driver() -> None:
        shell.start()
        if setup is not None:
            setup(shell)
        result = scenario(shell)
        if inspect.isawaitable(result):
            await result
        shell.shutdown()
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    with loop:
        loop.run_until_complete(driver())
    return shell, sink


def sandbox_user_config(monkeypatch: Any, tmp_path: Path) -> Path:
    """A sandboxed user-config dir (the real ~/.config/drcode must never
    be touched): the providers module reads it dynamically."""
    cfg_dir = tmp_path / "user-cfg"
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("drcode.config._USER_CONFIG_DIR", cfg_dir)
    # Reload the derived USER_CONFIG_FILE of the config module too (the
    # legacy flat-config reader uses it).
    monkeypatch.setattr("drcode.config.USER_CONFIG_FILE", cfg_dir / "drcode.json")
    monkeypatch.setattr(
        "drcode.providers._config.USER_CONFIG_FILE", cfg_dir / "drcode.json"
    )
    monkeypatch.delenv("DRCODE_TASK_ID", raising=False)
    return cfg_dir


def no_env_models(monkeypatch: Any) -> None:
    """The env layer must not shadow the catalog layers here."""
    monkeypatch.delenv("DRCODE_MODEL", raising=False)
    monkeypatch.delenv("DRCODE_BASE_URL", raising=False)


def write_user_flat_config(cfg_dir: Path) -> None:
    """A legacy user-level drcode.json with the flat model keys (the
    pre-migration shape `load_seeded_catalog` seeds from)."""
    (cfg_dir / "drcode.json").write_text(
        json.dumps(
            {
                "model": "qwen3-coder-30b",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "k-user",
                "max_context_tokens": 64000,
                "compaction_threshold": 80,
            }
        )
    )


def write_project_custom_models(shell: CoreShell) -> None:
    """A project drcode.json with one custom model (the second legacy
    seed source)."""
    state = shell.project_root / ".drcode"
    state.mkdir(exist_ok=True)
    (state / "drcode.json").write_text(
        json.dumps(
            {
                "custom_models": {
                    "tester": {
                        "model": "deepseek-r1",
                        "base_url": "http://localhost:10/v1",
                        "api_key": "k-proj",
                        "max_context_tokens": 32000,
                    }
                }
            }
        )
    )


def seed_catalog(monkeypatch: Any, tmp_path: Path, provider: str = "gateway") -> None:
    """A pre-existing models.json (the catalog is the source of truth —
    no seeding)."""
    cfg_dir = sandbox_user_config(monkeypatch, tmp_path)
    UserModelsStore().save(
        UserModelsConfig(
            providers=[
                Provider(
                    name=provider,
                    base_url="https://gw.example/v1",
                    api_key="k-secret",
                    health_check_url="https://gw.example/v1/health",
                    models=[],
                )
            ],
            defaults={},
        )
    )
    del cfg_dir


def dialog_model_rows(dialog: ModelCatalogDialog) -> list[tuple[str, str, str]]:
    table = dialog._table
    rows: list[tuple[str, str, str]] = []
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        assert item is not None
        ctx = table.item(row, 1)
        pct = table.item(row, 2)
        assert ctx is not None and pct is not None
        rows.append((item.text(), ctx.text(), pct.text()))
    return rows


# -- legacy pre-fill -------------------------------------------------------------------


def test_legacy_prefill_in_memory_only(tmp_path: Path, monkeypatch: Any) -> None:
    """With no models.json, the dialog pre-fills from the user-level
    flat drcode.json and the project's custom_models — and NOTHING is
    written: the seed stays in memory until the explicit Save."""
    cfg_dir = sandbox_user_config(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        # The legacy data lands AFTER the shell start: its one-time
        # startup migration ran on an empty config (the shell writes the
        # catalog itself when the flat keys are present — that path is
        # the migration, not this screen's seed).
        write_user_flat_config(cfg_dir)
        write_project_custom_models(shell)
        dialog = ModelCatalogDialog(shell)
        # Both seed sources materialized as providers (named after the
        # base-URL host).
        names = [p.name for p in dialog.catalog.providers]
        assert names == ["127.0.0.1", "localhost"]
        flat = dialog.catalog.providers[0]
        assert flat.base_url == "http://127.0.0.1:9/v1"
        assert flat.api_key == "k-user"
        assert [m.id for m in flat.models] == ["qwen3-coder-30b"]
        assert flat.models[0].max_context_tokens == 64000
        # compaction_threshold is carried over from the flat config.
        assert flat.models[0].compaction_threshold == 80
        proj = dialog.catalog.providers[1]
        assert [m.id for m in proj.models] == ["deepseek-r1"]
        # The status line surfaces the seed note (the TUI screen does).
        assert "Pre-filled" in dialog._status.text()
        # Cancel: the seed is discarded — models.json was never created.
        dialog.reject()
        assert not UserModelsStore().path.exists()
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_prefill_skipped_when_catalog_exists(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An existing models.json is the source of truth: the legacy data
    is NOT merged in on top of it."""
    cfg_dir = sandbox_user_config(monkeypatch, tmp_path)
    UserModelsStore().save(
        UserModelsConfig(
            providers=[
                Provider(name="gateway", base_url="https://gw.example/v1", models=[])
            ]
        )
    )
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        write_user_flat_config(cfg_dir)
        write_project_custom_models(shell)
        dialog = ModelCatalogDialog(shell)
        assert [p.name for p in dialog.catalog.providers] == ["gateway"]
        assert dialog.seed_note is None
        dialog.reject()
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


# -- save: TUI format, atomic write, .bak backup ---------------------------------------


def test_save_writes_tui_format_with_bak_backup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The first Save creates models.json in the TUI's exact format (read
    back through the core's own store); the second Save backs the
    previous version up to .bak and leaves no temp files behind."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        provider = dialog.catalog.providers[0]
        provider.models.append(
            ModelEntry(id="m1", max_context_tokens=128_000, compaction_threshold=75)
        )
        assert dialog._save() is True

        store = UserModelsStore()
        catalog = store.load()
        assert [p.name for p in catalog.providers] == ["gateway"]
        entry = catalog.providers[0].models[0]
        assert (entry.id, entry.max_context_tokens, entry.compaction_threshold) == (
            "m1",
            128_000,
            75,
        )
        # The exact persisted shape the TUI writes (pydantic json of the
        # catalog model).
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        assert raw["providers"][0]["name"] == "gateway"
        assert raw["providers"][0]["base_url"] == "https://gw.example/v1"
        assert raw["providers"][0]["api_key"] == "k-secret"
        assert raw["providers"][0]["health_check_url"] == "https://gw.example/v1/health"
        assert raw["providers"][0]["models"][0] == {
            "id": "m1",
            "max_context_tokens": 128_000,
            "max_tokens": None,
            "compaction_threshold": 75,
        }
        assert "defaults" in raw
        # The save backed the pre-seeded (empty) catalog up to .bak and
        # left no temp files behind (the atomic tmp+rename write).
        bak = json.loads(store.path.with_suffix(".json.bak").read_text("utf-8"))
        assert bak["providers"][0]["name"] == "gateway"
        assert bak["providers"][0]["models"] == []
        assert not list(store.path.parent.glob("*.tmp"))

        # The second save backs the previous version up.
        provider = catalog.providers[0]
        provider.models.append(ModelEntry(id="m2"))
        store.save(catalog)
        bak = json.loads(store.path.with_suffix(".json.bak").read_text("utf-8"))
        assert [m["id"] for m in bak["providers"][0]["models"]] == ["m1"]
        current = json.loads(store.path.read_text("utf-8"))
        assert [m["id"] for m in current["providers"][0]["models"]] == ["m1", "m2"]

        # The TUI reads the GUI-written file through its own screen API.
        reloaded = UserModelsStore().load()
        assert [m.id for m in reloaded.providers[0].models] == ["m1", "m2"]
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_role_defaults_persisted_on_save(tmp_path: Path, monkeypatch: Any) -> None:
    """The per-role default picked in the dialog lands in models.json
    `defaults[role]` (the "provider/model" string form) — the fallback
    layer of providers.resolve."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(ModelEntry(id="m1"))
        dialog._refresh_roles()
        combo = dialog._role_combos[AgentRole.TESTER]
        combo.setCurrentIndex(combo.findData("gateway/m1"))
        assert dialog._save() is True

        catalog = UserModelsStore().load()
        assert catalog.defaults[AgentRole.TESTER] == ModelRef(
            provider="gateway", model="m1"
        )
        # The saved default resolves for a role the project does not pin.
        config = resolve(
            Config(project_root=shell.project_root),
            AgentRole.TESTER,
        )
        assert config.model == "m1"
        assert config.base_url == "https://gw.example/v1"
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_cancel_writes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    """Cancel/reject discards every draft edit: models.json keeps its
    exact previous bytes and no .bak appears."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)
    before = UserModelsStore().path.read_bytes()

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(ModelEntry(id="junk"))
        dialog._name.setText("renamed")
        dialog._url.setText("http://evil.example/v1")
        dialog.reject()
        store = UserModelsStore()
        assert store.path.read_bytes() == before
        assert not store.path.with_suffix(".json.bak").exists()
        catalog = store.load()
        assert catalog.providers[0].models == []
        assert catalog.providers[0].base_url == "https://gw.example/v1"
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


# -- cache pickup after Save (the plan-task-6 dialog) ----------------------------------


def test_model_settings_dialog_picks_up_saved_catalog(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """After Save the shell's per-role resolution cache is dropped: the
    project model-settings dialog offers the new model and the live
    resolution returns the new connection."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        # Nothing is resolved yet (no models anywhere).
        assert not shell.resolved_config(AgentRole.CODING).is_ready

        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(ModelEntry(id="m-new"))
        dialog._refresh_roles()
        combo = dialog._role_combos[AgentRole.CODING]
        combo.setCurrentIndex(combo.findData("gateway/m-new"))
        assert dialog._save() is True

        # The cache was invalidated: the shell resolves the new file.
        config = shell.resolved_config(AgentRole.CODING)
        assert config.is_ready
        assert config.model == "m-new"
        assert config.base_url == "https://gw.example/v1"
        assert config.api_key == "k-secret"
        assert shell.resolved_model_key(AgentRole.CODING) == "gateway/m-new"

        # The plan-task-6 dialog picks the new catalog up on open (the
        # new model is offered; the project slot itself is still unset —
        # the role resolves through the catalog default).
        settings_dialog = ModelSettingsDialog(shell)
        section = settings_dialog._sections[AgentRole.CODING]
        assert section.combo.findData("gateway/m-new") >= 0
        assert section.combo.currentData() == "__user_default__"
        settings_dialog.deleteLater()
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


# -- validation ------------------------------------------------------------------------


def test_provider_form_validation(tmp_path: Path, monkeypatch: Any) -> None:
    """The commit gate: empty/duplicated name, a "/" in the name and an
    empty base URL each refuse to commit with the message shown — and
    nothing is saved."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        # A second provider to edit (the first, "gateway", stays intact).
        dialog._add_provider()
        assert dialog._current == "new-provider"
        # An empty base URL refuses to commit...
        dialog._name.setText("gateway")
        assert dialog._commit_form() is False
        assert "base URL must not be empty" in dialog._status.text()
        # ...a duplicate name does too (checked once the URL is set)...
        dialog._url.setText("https://other.example/v1")
        assert dialog._commit_form() is False
        assert "already exists" in dialog._status.text()
        # ...as does a "/" in the name (the reference-format separator).
        dialog._name.setText("some/name")
        assert dialog._commit_form() is False
        assert "must not contain '/'" in dialog._status.text()
        # ...and a bad model-id / caps pair in the model editor.
        dialog._name.setText("ok-name")
        assert dialog._commit_form() is True
        provider = next(
            p for p in dialog.catalog.providers if p.name == "ok-name"
        )
        provider.models.append(ModelEntry(id="m1"))
        dialog._refresh_models()
        dialog._table.selectRow(0)
        dialog._model_id.setText("bad/id")
        dialog._ctx.setText("1000")
        dialog._update_model()
        assert "must not contain '/'" in dialog._status.text()
        dialog._model_id.setText("m1")
        dialog._ctx.setText("not-a-number")
        dialog._update_model()
        assert "context tokens must be a positive integer" in dialog._status.text()
        dialog._ctx.setText("1000")
        dialog._pct.setText("150")
        dialog._update_model()
        assert "compact % must be an integer between 1 and 100" in dialog._status.text()
        # Duplicate model id on add.
        dialog._pct.setText("")
        dialog._add_model()
        assert "already exists" in dialog._status.text()
        # Nothing reached the disk.
        assert UserModelsStore().load().providers[0].models == []
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_rename_provider_keeps_role_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    """Renaming a provider rewrites the role-default references (and the
    role combos) so the saved defaults keep pointing at the same
    provider — the TUI screen's same rename rule."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)
    store = UserModelsStore()
    catalog = store.load()
    catalog.providers[0].models.append(ModelEntry(id="m1"))
    catalog.defaults[AgentRole.CODING] = ModelRef(provider="gateway", model="m1")
    store.save(catalog)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        combo = dialog._role_combos[AgentRole.CODING]
        assert combo.currentData() == "gateway/m1"
        dialog._name.setText("renamed")
        assert dialog._commit_form() is True
        assert combo.currentData() == "renamed/m1"
        assert dialog._save() is True
        saved = UserModelsStore().load()
        assert saved.defaults[AgentRole.CODING] == ModelRef(
            provider="renamed", model="m1"
        )
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_delete_provider_drops_its_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)
    store = UserModelsStore()
    catalog = store.load()
    catalog.providers[0].models.append(ModelEntry(id="m1"))
    catalog.defaults[AgentRole.CODING] = ModelRef(provider="gateway", model="m1")
    store.save(catalog)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog._delete_provider()
        assert dialog.catalog.providers == []
        assert AgentRole.CODING not in dialog.catalog.defaults
        assert dialog._save() is True
        saved = UserModelsStore().load()
        assert saved.providers == []
        assert saved.defaults.get(AgentRole.CODING) is None
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


# -- probe / fetch models (the optional TUI parity helpers) ----------------------------


def test_fetch_models_appends_and_keeps_edits(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Auto-discovery merges: existing entries keep their manual edits,
    new ones are appended (the TUI screen's same rule)."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    async def fake_fetch(base_url: str, api_key: str, **kw: Any) -> list[ModelEntry]:
        assert base_url == "https://gw.example/v1"
        assert api_key == "k-secret"
        return [
            ModelEntry(id="m1", max_context_tokens=999),  # exists — ignored
            ModelEntry(id="m-new", max_context_tokens=12345),
        ]

    monkeypatch.setattr("drcodedesktop.model_catalog_dialog.fetch_models", fake_fetch)

    async def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(
            ModelEntry(id="m1", max_context_tokens=64000)
        )
        dialog._refresh_models()
        await dialog._fetch_models()
        rows = dialog_model_rows(dialog)
        assert rows == [("m1", "64000", "inherit"), ("m-new", "12345", "inherit")]
        assert "fetched 2 models (1 new)" in dialog._status.text()
        # Still in-memory only.
        assert UserModelsStore().load().providers[0].models == []
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_check_connection_caches_health_url(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A successful probe caches the verified endpoint into the
    provider's health_check_url (and the form field) — the TUI screen's
    same behaviour."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    calls: list[tuple[str, str, str]] = []

    async def fake_probe(
        model: str, base_url: str, api_key: str, **kw: Any
    ) -> tuple[bool, str, str]:
        calls.append((model, base_url, api_key))
        return True, "", "https://gw.example/v1/health"

    monkeypatch.setattr("drcodedesktop.model_catalog_dialog.probe_model", fake_probe)

    async def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(ModelEntry(id="m1"))
        dialog._load_form("gateway")
        # The form's cached URL was already correct; probe a MISS first
        # to prove the checked URL is what gets cached.
        dialog._health.setText("")
        await dialog._check_connection()
        assert calls == [("m1", "https://gw.example/v1", "k-secret")]
        assert dialog.catalog.providers[0].health_check_url == (
            "https://gw.example/v1/health"
        )
        assert dialog._health.text() == "https://gw.example/v1/health"
        assert "connection ok" in dialog._status.text()
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_check_connection_failure_message(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A failed probe reports the detail and leaves the cached URL
    alone."""
    seed_catalog(monkeypatch, tmp_path)
    no_env_models(monkeypatch)

    async def fake_probe(
        model: str, base_url: str, api_key: str, **kw: Any
    ) -> tuple[bool, str, str]:
        return False, "unavailable (ConnectError)", ""

    monkeypatch.setattr("drcodedesktop.model_catalog_dialog.probe_model", fake_probe)

    async def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers[0].models.append(ModelEntry(id="m1"))
        dialog._load_form("gateway")
        await dialog._check_connection()
        assert "connection failed: unavailable" in dialog._status.text()
        # The previously cached URL survives the failed probe.
        assert (
            dialog.catalog.providers[0].health_check_url
            == "https://gw.example/v1/health"
        )
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


# -- the agent guard of UserModelsStore.save stays intact ------------------------------


def test_save_under_drcode_task_id_is_blocked(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The incident ebe6ba15 guard: with DRCODE_TASK_ID set, a
    default-constructed store must never write the REAL user catalog —
    the dialog surfaces the error and stays open, writing nothing."""
    sandbox_user_config(monkeypatch, tmp_path)
    no_env_models(monkeypatch)
    monkeypatch.setenv("DRCODE_TASK_ID", "t1")

    def scenario(shell: CoreShell) -> None:
        dialog = ModelCatalogDialog(shell)
        dialog.catalog.providers.append(
            Provider(name="p", base_url="http://127.0.0.1:9/v1")
        )
        assert dialog._save() is False
        assert "could not write" in dialog._status.text()
        # The dialog stays open (reject/accept never ran) and nothing
        # was written.
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert not UserModelsStore().path.exists()
        dialog.deleteLater()

    run_catalog_scenario(tmp_path, scenario)


def test_dialog_is_modal_qdialog() -> None:
    assert issubclass(ModelCatalogDialog, QDialog)
