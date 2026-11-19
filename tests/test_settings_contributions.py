from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_settings_ui import make_app, text_of
from textual.widgets import Button, DataTable, Select, Static

from tdnext.api import PluginContext, SettingsControls
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.removal import RemovalRequest
from tdnext.core.session import SessionSnapshot, SessionView
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.operations import OperationResult
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


@contextmanager
def allow_removal(request: RemovalRequest) -> Iterator[tuple[str, ...]]:
    yield ()


class OtherControls(SettingsControls):
    def compose(self):
        yield Button("Other setting", id="other-setting")


class OtherPlugin:
    def make_settings_controls(self, _context: PluginContext) -> SettingsControls:
        return OtherControls()


class BrokenPlugin:
    def make_settings_controls(self, _context: PluginContext) -> SettingsControls:
        raise RuntimeError("broken contribution")


@pytest.mark.anyio
@pytest.mark.parametrize("snapshot", [False, True])
async def test_only_session_participants_contribute_settings(tmp_path: Path, snapshot: bool) -> None:
    _desktop, manager = make_app(tmp_path)
    files = manager.manifests["files"].model_copy(update={"source_dir": None})
    other = files.model_copy(update={
        "id": "other", "entrypoint": "test_settings_contributions:OtherPlugin", "source_dir": None,
    })
    manager.manifests = {**manager.manifests, "other": other}
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"other"}, enabled=state.enabled | {"other"}))
    session = SessionSnapshot(
        views=(SessionView(manifest=files, icon="[]"),),
        protected_plugins=frozenset(),
        view_dependencies={},
        participants=(files,),
    ) if snapshot else None
    context = PluginContext(tmp_path, manager, session)
    if snapshot:
        manager.disable("files")
    app = ToolPane("settings", SettingsPlugin(), context)
    async with app.run_test(size=(80, 24)):
        assert app.query_one("#editor-choice", Select).value == "environment"
        assert bool(app.query("#other-setting")) is not snapshot


@pytest.mark.anyio
async def test_snapshot_service_contributes_even_when_live_state_changes(tmp_path: Path) -> None:
    _desktop, manager = make_app(tmp_path)
    files = manager.manifests["files"].model_copy(update={"source_dir": None})
    service = manager.manifests["icons"].model_copy(
        update={"id": "other", "entrypoint": "test_settings_contributions:OtherPlugin", "source_dir": None}
    )
    session = SessionSnapshot(
        views=(SessionView(manifest=files, icon="[]"),),
        protected_plugins=frozenset(),
        view_dependencies={},
        participants=(files, service),
    )
    app = ToolPane("settings", SettingsPlugin(), PluginContext(tmp_path, manager, session))
    async with app.run_test(size=(80, 24)):
        assert app.query_one("#other-setting", Button).display
        assert app.query_one("#editor-choice", Select).value == "environment"


@pytest.mark.anyio
async def test_broken_contributor_is_reported_and_other_controls_work(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    broken = manager.manifests["icons"].model_copy(
        update={"id": "broken", "entrypoint": "test_settings_contributions:BrokenPlugin", "source_dir": None}
    )
    manager.manifests = {**manager.manifests, "broken": broken}
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"broken"}, enabled=state.enabled | {"broken"}))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        message = text_of(app.query_one("#plugin-feedback", Static))
        assert "broken" in message and "broken contribution" in message
        await pilot.press("tab", "tab", "tab", "enter", "down", "enter")
        assert app.query_one("#editor-choice", Select).value == "nvim"
        assert manager.store.path.with_name("editor.json").exists()


@pytest.mark.anyio
async def test_disabled_files_keeps_preferences_without_showing_controls(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    path = manager.store.path.with_name("editor.json")
    path.write_text('{"editor":"vi"}', encoding="utf-8")
    manager.disable("files")
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)):
        assert not app.query("#editor-choice")
        assert path.read_text(encoding="utf-8") == '{"editor":"vi"}'


@pytest.mark.anyio
async def test_install_button_changes_action_with_selection(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.removal_guard = allow_removal
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        def uninstall_locally(managed: PluginManager, plugin_id: str) -> OperationResult:
            return OperationResult("uninstall", plugin_id, tuple(sorted(managed.uninstall(plugin_id))))

        view.uninstall_runner = uninstall_locally
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        button = view.query_one("#plugin-install", Button)
        assert str(button.label) == "Install"
        await pilot.click(button)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "monitor" in manager.store.read().installed
        assert str(button.label) == "Uninstall"
        await pilot.click(button)
        await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        assert "monitor" not in manager.store.read().installed
