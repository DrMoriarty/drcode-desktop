"""Plan task 6 acceptance tests: settings and models from the GUI.

The dialogs drive the exact production seams —
``CoreShell.apply_task_settings`` / ``CoreShell.apply_model_settings`` —
and the assertions are made against the CONFIG FILES THEMSELVES (the
same files the TUI writes), so GUI↔TUI interchangeability on one
project is proven by construction:

- the per-role model selection writes ``models[role]`` into
  ``.drcode/drcode.json`` (a catalog reference or "custom" with the
  ``custom_models[role]`` entry) — read back with the core's
  ``load_project_model_slots`` / ``load_project_custom_models`` and
  resolved through ``providers.resolve`` (the full layering: project
  slot > user default, the per-role max_tokens override last);
- the project settings draft lands in ``.drcode/settings.json`` via
  ``ProjectSettingsStore`` (the TUI's store) and merges into the LIVE
  settings object the TaskEnvironment shares;
- cancel discards the draft: no file is touched;
- the live hints (the shared ``task_settings_hints``) surface in the
  dialog, with the explicit fix button applying to the draft only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from drcode.config import Config
from drcode.providers import (
    AgentRole,
    CustomModelEntry,
    ModelEntry,
    ModelRef,
    Provider,
    UserModelsConfig,
    UserModelsStore,
    load_project_custom_models,
    load_project_model_slots,
    resolve,
    set_project_custom_model,
    set_project_model,
)
from drcode.settings import (
    CompletedRetention,
    ProjectSettings,
    ProjectSettingsStore,
    TaskIsolation,
    Verification,
)
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton
from qasync import QEventLoop

from drcodedesktop.core_shell import CoreShell
from drcodedesktop.event_hub import EventHub
from drcodedesktop.model_settings_dialog import ModelSettingsDialog
from drcodedesktop.qt_host import QtHost
from drcodedesktop.settings_dialog import (
    ProjectSettingsDialog,
    copy_task_settings,
)
from tests.test_user_input import EventSink

Scenario = Callable[[CoreShell], None]

# The QApplication must outlive every widget the tests create (the
# test_user_input harness convention).
_APP: QApplication | None = None


def run_dialog_scenario(
    tmp_path: Path, scenario: Scenario, setup: Callable[[CoreShell], None] | None = None
) -> tuple[CoreShell, EventSink]:
    """A synchronous variant of run_shell_scenario for the widget tests:
    the shell runs on a qasync loop, `setup` applies right after start,
    `scenario` drives the dialogs directly (they never exec —
    `_apply`/`reject` are invoked as plain methods)."""
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
        scenario(shell)
        shell.shutdown()
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)

    with loop:
        loop.run_until_complete(driver())
    return shell, sink


def seed_catalog(
    monkeypatch: Any, tmp_path: Path, provider: str = "gateway"
) -> None:
    """A sandboxed user catalog (the real ~/.config/drcode must never be
    touched): the config dir is re-pointed BEFORE any store use — the
    providers module reads it dynamically."""
    cfg_dir = tmp_path / "user-cfg"
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("drcode.config._USER_CONFIG_DIR", cfg_dir)
    monkeypatch.delenv("DRCODE_TASK_ID", raising=False)
    UserModelsStore().save(
        UserModelsConfig(
            providers=[
                Provider(
                    name=provider,
                    base_url="https://gw.example/v1",
                    api_key="k-secret",
                    models=[],
                )
            ],
            defaults={},
        )
    )


def add_catalog_model(
    provider: str = "gateway", model: str = "m1", context: int = 200_000
) -> None:
    """Append one model to the (already redirected) user catalog."""
    store = UserModelsStore()
    catalog = store.load()
    prov = next((p for p in catalog.providers if p.name == provider), None)
    if prov is None:
        prov = Provider(name=provider, base_url="https://gw.example/v1", api_key="k")
        catalog.providers.append(prov)
    prov.models.append(ModelEntry(id=model, max_context_tokens=context))
    store.save(catalog)


def no_env_models(monkeypatch: Any) -> None:
    """The env layer must not shadow the catalog/project layers here —
    and the agent-guard of UserModelsStore.save (incident ebe6ba15)
    must not fire: the tests write only into sandboxed catalogs."""
    monkeypatch.delenv("DRCODE_MODEL", raising=False)
    monkeypatch.delenv("DRCODE_BASE_URL", raising=False)
    monkeypatch.delenv("DRCODE_TASK_ID", raising=False)


def project_json(shell: CoreShell) -> dict[str, Any]:
    path = shell.project_root / ".drcode" / "drcode.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# -- model selection: catalog references -------------------------------------------


def test_catalog_ref_selection_writes_project_slot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Picking a catalog model for a role writes the "<provider>/<model>"
    reference into the project's drcode.json (the TUI's `set_project_model`
    file), and the shell's cached resolution picks the new connection up
    after the invalidation."""
    seed_catalog(monkeypatch, tmp_path)
    add_catalog_model(model="m1", context=128_000)
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        section = dialog._sections[AgentRole.CODING]
        # The catalog models are offered; "User default" is the initial
        # selection (nothing selected anywhere).
        assert section.combo.findData("gateway/m1") >= 0
        assert section.combo.currentData() == "__user_default__"

        section.combo.setCurrentIndex(section.combo.findData("gateway/m1"))
        assert dialog._apply() is True

        # The file the TUI writes: models.coding = "gateway/m1".
        assert project_json(shell)["models"]["coding"] == "gateway/m1"
        slots = load_project_model_slots(shell.project_root)
        assert slots[AgentRole.CODING] == ModelRef(provider="gateway", model="m1")

        # The resolution cache was invalidated: the shell now resolves
        # the role to the catalog connection.
        config = shell.resolved_config(AgentRole.CODING)
        assert config.model == "m1"
        assert config.base_url == "https://gw.example/v1"
        assert config.api_key == "k-secret"
        assert config.max_context_tokens == 128_000
        assert shell.resolved_model_key(AgentRole.CODING) == "gateway/m1"
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


def test_custom_model_written_and_activated(tmp_path: Path, monkeypatch: Any) -> None:
    """The "Custom…" path: the inline connection form lands in
    custom_models[role] AND is activated (models[role] = "custom") in one
    apply — and the shell's resolution returns exactly that connection."""
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        section = dialog._sections[AgentRole.TESTER]
        section.combo.setCurrentIndex(section.combo.findData("__custom__"))
        section.custom_model.setText("qwen3-coder-30b")
        section.custom_base_url.setText("http://127.0.0.1:9/v1")
        section.custom_api_key.setText("kk")
        section.custom_context.setValue(131_072)
        section.custom_max_tokens.setText("4096")
        assert dialog._apply() is True

        data = project_json(shell)
        assert data["models"]["tester"] == "custom"
        custom = data["custom_models"]["tester"]
        assert custom["model"] == "qwen3-coder-30b"
        assert custom["base_url"] == "http://127.0.0.1:9/v1"
        assert custom["max_context_tokens"] == 131_072
        assert custom["max_tokens"] == 4096

        # Read back through the core's own loader + resolution.
        loaded = load_project_custom_models(shell.project_root)
        assert loaded[AgentRole.TESTER].model == "qwen3-coder-30b"
        config = shell.resolved_config(AgentRole.TESTER)
        assert config.model == "qwen3-coder-30b"
        assert config.base_url == "http://127.0.0.1:9/v1"
        assert config.max_context_tokens == 131_072
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


def test_custom_form_validation_keeps_dialog_open(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A custom slot without a model/base_url refuses to apply: the
    dialog stays open (accept never called), nothing is written."""
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        section = dialog._sections[AgentRole.CODING]
        section.combo.setCurrentIndex(section.combo.findData("__custom__"))
        assert dialog._apply() is False
        assert "custom model needs" in dialog._note.text()
        assert "models" not in project_json(shell)
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


def test_user_default_clears_the_slot(tmp_path: Path, monkeypatch: Any) -> None:
    """Choosing "User default" over an existing slot writes null (the
    core's invariant: all-null → the key is dropped) and the resolution
    falls through to the user-level default."""
    seed_catalog(monkeypatch, tmp_path)
    add_catalog_model(model="m1")
    add_catalog_model(model="fallback", provider="other")
    no_env_models(monkeypatch)

    def setup(shell: CoreShell) -> None:
        # The TUI-side pre-selection: a project slot + a user default.
        set_project_model(
            shell.project_root,
            AgentRole.CODING,
            ModelRef(provider="gateway", model="m1"),
        )
        store = UserModelsStore()
        catalog = store.load()
        catalog.defaults[AgentRole.CODING] = ModelRef(
            provider="other", model="fallback"
        )
        store.save(catalog)
        shell.invalidate_resolved_configs()

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        section = dialog._sections[AgentRole.CODING]
        # The dialog pre-selects the saved project slot (TUI-parity read).
        assert section.combo.currentData() == "gateway/m1"

        section.combo.setCurrentIndex(section.combo.findData("__user_default__"))
        assert dialog._apply() is True

        data = project_json(shell)
        # The core's invariant: all-null slots → the key is dropped
        # (absence == null, the exact bytes the TUI writes).
        assert "models" not in data
        # Resolution falls through to the user-level default.
        config = shell.resolved_config(AgentRole.CODING)
        assert config.model == "fallback"
        assert shell.resolved_model_key(AgentRole.CODING) == "other/fallback"
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario, setup)


# -- max_tokens overrides -------------------------------------------------------------


def test_role_max_tokens_override_layering(tmp_path: Path, monkeypatch: Any) -> None:
    """The override lands in settings.json `role_max_tokens` (merged
    against the STORED file) and providers.resolve applies it as the LAST
    layer — on top of the catalog entry's own max_tokens."""
    seed_catalog(monkeypatch, tmp_path)
    add_catalog_model(model="m1")
    # The catalog entry itself caps at 2048 — the override must beat it.
    store = UserModelsStore()
    catalog = store.load()
    catalog.providers[0].models[0].max_tokens = 2048
    catalog.defaults[AgentRole.TESTER] = ModelRef(provider="gateway", model="m1")
    store.save(catalog)
    no_env_models(monkeypatch)

    def setup(shell: CoreShell) -> None:
        # A pre-existing unrelated override must survive the merge.
        shell.settings_store.save(ProjectSettings(role_max_tokens={"coding": 32768}))

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        tester = dialog._sections[AgentRole.TESTER]
        assert tester.override.text() == ""  # no stored override for tester
        tester.override.setText("16384")
        assert dialog._apply() is True

        stored = ProjectSettingsStore(shell.project_root).load()
        assert stored.role_max_tokens == {"coding": 32768, "tester": 16384}
        # The live object (shared with TaskEnvironment) carries it too.
        assert shell.settings.role_max_tokens["tester"] == 16384

        # The resolution layering: catalog entry 2048 < override 16384.
        config = Config(project_root=shell.project_root)
        resolved = resolve(config, AgentRole.TESTER)
        assert resolved.model == "m1"
        assert resolved.max_tokens == 16384
        # The untouched role keeps its own stored override.
        coding = resolve(config, AgentRole.CODING)
        assert coding.max_tokens == 32768
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario, setup)


def test_role_max_tokens_inherit_removes_the_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Emptying a previously-set override removes the key (empty =
    inherit — the TUI dialog's same semantics)."""
    no_env_models(monkeypatch)

    def setup(shell: CoreShell) -> None:
        shell.settings_store.save(ProjectSettings(role_max_tokens={"tester": 16384}))

    def scenario(shell: CoreShell) -> None:
        dialog = ModelSettingsDialog(shell)
        assert dialog._sections[AgentRole.TESTER].override.text() == "16384"
        dialog._sections[AgentRole.TESTER].override.setText("")
        assert dialog._apply() is True
        stored = ProjectSettingsStore(shell.project_root).load()
        assert stored.role_max_tokens == {}
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario, setup)


# -- project settings dialog ----------------------------------------------------------


def test_project_settings_written_via_tui_store(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The settings draft is persisted into .drcode/settings.json (the
    TUI's ProjectSettingsStore file) and merged into the LIVE settings
    object — the instance TaskEnvironment holds."""
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ProjectSettingsDialog(shell)
        # The dialog edits a deep copy: touching widgets does not move
        # the live settings until Apply.
        dialog._isolation.setCurrentIndex(
            dialog._isolation.findData(TaskIsolation.WORKTREE.value)
        )
        dialog._parallel.setCurrentIndex(dialog._parallel.findData(3))
        dialog._retention.setCurrentIndex(
            dialog._retention.findData(CompletedRetention.DAYS_90.value)
        )
        dialog._verification.setCurrentIndex(
            dialog._verification.findData(Verification.NONE.value)
        )
        dialog._start_branch.setText("main")
        assert shell.settings.task_isolation is TaskIsolation.OFF  # draft only
        assert dialog._apply() is True

        stored = ProjectSettingsStore(shell.project_root).load()
        assert stored.task_isolation is TaskIsolation.WORKTREE
        assert stored.parallel_tasks == 3
        assert stored.completed_retention is CompletedRetention.DAYS_90
        assert stored.verification is Verification.NONE
        assert stored.isolation_start_branch == "main"
        # The live object is updated IN PLACE (the shared instance).
        assert shell.settings is not stored
        assert shell.settings.task_isolation is TaskIsolation.WORKTREE
        assert shell.settings.parallel_tasks == 3
        assert shell.task_env.settings.task_isolation is TaskIsolation.WORKTREE
        # Untouched fields survive (the copy semantics of copy_task_settings).
        assert shell.settings.subagents_coder == 0
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


def test_project_settings_cancel_discards(tmp_path: Path, monkeypatch: Any) -> None:
    """Cancel (reject) discards everything: the file is not even
    created, the live settings keep their values."""
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ProjectSettingsDialog(shell)
        dialog._isolation.setCurrentIndex(
            dialog._isolation.findData(TaskIsolation.WORKTREE.value)
        )
        dialog.reject()
        assert not (shell.project_root / ".drcode" / "settings.json").exists()
        assert shell.settings.task_isolation is TaskIsolation.OFF
        assert (
            ProjectSettingsStore(shell.project_root).load().task_isolation
            is TaskIsolation.OFF
        )
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


def test_settings_hint_rendered_and_fix_applies_to_draft(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Parallel tasks > 1 without worktree isolation raises the dormant
    hint (the SHARED task_settings_hints source); the fix button flips
    the DRAFT's isolation — never the live settings — and re-renders."""
    no_env_models(monkeypatch)

    def scenario(shell: CoreShell) -> None:
        dialog = ProjectSettingsDialog(shell)
        dialog._parallel.setCurrentIndex(dialog._parallel.findData(2))
        texts = [
            label.text()
            for row in range(dialog._hints_box.count())
            for label in dialog._hints_box.itemAt(row).widget().findChildren(QLabel)
        ]
        assert any("dormant without worktree isolation" in t for t in texts)
        # The live settings are untouched by the hint.
        assert shell.settings.task_isolation is TaskIsolation.OFF

        buttons = [
            button
            for row in range(dialog._hints_box.count())
            for button in dialog._hints_box.itemAt(row).widget().findChildren(
                QPushButton
            )
        ]
        assert len(buttons) == 1
        buttons[0].click()
        assert dialog._isolation.currentData() == TaskIsolation.WORKTREE.value
        assert shell.settings.task_isolation is TaskIsolation.OFF
        assert not (shell.project_root / ".drcode" / "settings.json").exists()
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


# -- GUI↔TUI interchangeability ------------------------------------------------------


def test_gui_reads_tui_written_configs(tmp_path: Path, monkeypatch: Any) -> None:
    """Configs written BY THE TUI'S OWN APIs are pre-selected correctly
    by the GUI dialogs; a GUI apply afterwards leaves the files readable
    by the TUI's stores (one project, both front-ends)."""
    seed_catalog(monkeypatch, tmp_path)
    add_catalog_model(model="m1")
    add_catalog_model(model="m2")
    no_env_models(monkeypatch)
    # The TUI-written settings file lands BEFORE the shell starts: the
    # live settings load once at init (the TUI's own startup order).
    proj = tmp_path / "proj" / ".drcode"
    proj.mkdir(parents=True, exist_ok=True)
    store = ProjectSettingsStore(tmp_path / "proj")
    settings = store.load()
    settings.task_isolation = TaskIsolation.WORKTREE
    settings.role_max_tokens = {"tester": 16384}
    store.save(settings)

    def setup(shell: CoreShell) -> None:
        # The TUI side of the interchange: writes through the core APIs
        # exactly as tui.actions_models does (read dynamically by the
        # dialogs, no restart needed — same as the TUI).
        set_project_model(
            shell.project_root,
            AgentRole.RESEARCH,
            ModelRef(provider="gateway", model="m2"),
        )
        set_project_custom_model(
            shell.project_root,
            AgentRole.RESOLVER,
            CustomModelEntry(
                model="deepseek-r1",
                base_url="http://127.0.0.1:9/v1",
                max_context_tokens=64_000,
            ),
        )
        shell.invalidate_resolved_configs()

    def scenario(shell: CoreShell) -> None:
        # The model dialog pre-selects the TUI-written slots.
        dialog = ModelSettingsDialog(shell)
        assert (
            dialog._sections[AgentRole.RESEARCH].combo.currentData() == "gateway/m2"
        )
        section = dialog._sections[AgentRole.RESOLVER]
        assert section.is_custom()
        assert section.custom_model.text() == "deepseek-r1"
        assert section.custom_base_url.text() == "http://127.0.0.1:9/v1"
        assert section.custom_context.value() == 64_000
        assert dialog._sections[AgentRole.TESTER].override.text() == "16384"
        # The settings dialog pre-selects the TUI-written isolation.
        sdialog = ProjectSettingsDialog(shell)
        assert sdialog._isolation.currentData() == TaskIsolation.WORKTREE.value
        assert sdialog._parallel.currentData() == 1  # untouched → default

        # The GUI side: apply a change to a third role.
        coding = dialog._sections[AgentRole.CODING]
        coding.combo.setCurrentIndex(coding.combo.findData("gateway/m1"))
        assert dialog._apply() is True

        # The TUI's loaders read the merged result; nothing was lost.
        slots = load_project_model_slots(shell.project_root)
        assert slots[AgentRole.RESEARCH] == ModelRef(provider="gateway", model="m2")
        assert slots[AgentRole.CODING] == ModelRef(provider="gateway", model="m1")
        assert slots[AgentRole.RESOLVER] == "custom"
        customs = load_project_custom_models(shell.project_root)
        assert customs[AgentRole.RESOLVER].model == "deepseek-r1"
        stored = ProjectSettingsStore(shell.project_root).load()
        assert stored.task_isolation is TaskIsolation.WORKTREE
        assert stored.role_max_tokens == {"tester": 16384}
        for widget in (dialog, sdialog):
            widget.deleteLater()

    run_dialog_scenario(tmp_path, scenario, setup)


def test_settings_dialog_reads_legacy_tui_settings(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A settings.json written by an older TUI (the legacy
    `task_finalization` key) loads through the core's migration and the
    dialog shows the mapped values."""
    no_env_models(monkeypatch)
    # Pre-seed BEFORE the shell starts: the live settings load once at
    # init (the file the TUI wrote lands there through its own store).
    proj = tmp_path / "proj" / ".drcode"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "settings.json").write_text(
        json.dumps({"task_finalization": "testing_commit_no_push"})
    )

    def scenario(shell: CoreShell) -> None:
        dialog = ProjectSettingsDialog(shell)
        assert dialog._verification.currentData() == Verification.TESTER.value
        assert dialog._git_tester.currentData() == "local"
        dialog.deleteLater()

    run_dialog_scenario(tmp_path, scenario)


# -- copy semantics (the GUI↔TUI shared contract) --------------------------------------


def test_copy_task_settings_copies_edited_fields_only() -> None:
    """copy_task_settings moves exactly the dialog's fields — GitHub /
    voice / role_max_tokens of the destination stay put."""
    dst = ProjectSettings(github_repo="owner/repo", role_max_tokens={"tester": 99})
    src = dst.model_copy(deep=True)
    src.task_isolation = TaskIsolation.WORKTREE
    src.verification = Verification.NONE
    src.role_max_tokens = {"coding": 1}
    src.github_repo = "other/repo"
    copy_task_settings(dst, src)
    assert dst.task_isolation is TaskIsolation.WORKTREE
    assert dst.verification is Verification.NONE
    assert dst.github_repo == "owner/repo"  # not the dialog's field
    assert dst.role_max_tokens == {"tester": 99}  # not the dialog's field


def test_dialogs_are_modal_qdialogs() -> None:
    """Both dialogs are modal QDialogs (the TUI's ModalScreen twin)."""
    assert issubclass(ProjectSettingsDialog, QDialog)
    assert issubclass(ModelSettingsDialog, QDialog)
