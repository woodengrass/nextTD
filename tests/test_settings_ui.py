import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from textual.content import Content
from textual.widgets import Button, ContentSwitcher, DataTable, ListView, Static

from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.removal import RemovalRequest
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.files.plugin import FilesPlugin
from tdnext.plugins.files.view import FilesView
from tdnext.plugins.monitor.plugin import MonitorPlugin
from tdnext.plugins.settings.operations import OperationResult
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


def text_of(widget: Static) -> str:
    content = widget.render()
    assert isinstance(content, Content)
    return content.plain


@contextmanager
def allow_removal(request: RemovalRequest) -> Iterator[tuple[str, ...]]:
    yield ()


def make_app(tmp_path: Path, *, monitor: bool = False) -> tuple[DesktopApp, PluginManager]:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": (), "dependency_hook": None}
    )
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    manager.enable("files")
    manager.enable("settings")
    if monitor:
        manager.enable("monitor")
    views: list[tuple[PluginManifest, ViewPlugin]] = [(manifests["files"], FilesPlugin())]
    if monitor:
        views.append((manifests["monitor"], MonitorPlugin()))
    views.append((manifests["settings"], SettingsPlugin()))
    return DesktopApp(PluginContext(tmp_path, manager), views), manager


@pytest.mark.anyio
@pytest.mark.parametrize("plugin_id", ["desktop", "settings"])
async def test_running_plugin_can_be_selected_for_uninstall(tmp_path: Path, plugin_id: str) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index(plugin_id))
        await pilot.pause()
        button = view.query_one("#plugin-install", Button)
        assert not button.disabled
        await pilot.click(button)
        assert app.screen.query_one("#confirm-plugin-change", Button)
        await pilot.click("#cancel")


@pytest.mark.anyio
async def test_settings_opens_from_header_not_sidebar_and_escape_restores_files(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        files = app.query_one(FilesView)
        files.directory = folder
        files.reload_directory()
        original_focus = app.focused
        await pilot.press("alt+o")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        assert len(app.query_one("#destinations", ListView).children) == 1
        assert app.query_one("#header-settings", Button).display
        await pilot.press("escape")
        assert app.query_one(ContentSwitcher).current == "view-files"
        assert app.query_one(FilesView) is files
        assert files.directory == folder
        assert app.focused is original_focus
        assert text_of(app.query_one("#shortcuts", Static)).splitlines()[0] == f"Tool (Files): {FilesPlugin.shortcuts}"


@pytest.mark.anyio
async def test_settings_back_restores_monitor_and_its_footer(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    async with app.run_test(size=(80, 24)) as pilot:
        rail = app.query_one("#destinations", ListView)
        rail.focus()
        rail.index = 1
        await pilot.press("enter", "alt+o")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        await pilot.click("#settings-back")
        assert app.query_one(ContentSwitcher).current == "view-monitor"
        assert text_of(app.query_one("#shortcuts", Static)).splitlines()[0] == (
            f"Tool (Monitor): {MonitorPlugin.fallback_shortcuts}"
        )


@pytest.mark.anyio
async def test_header_action_can_be_opened_by_mouse(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        original_focus = app.focused
        await pilot.click("#header-settings")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        await pilot.click("#settings-back")
        assert app.focused is original_focus


@pytest.mark.anyio
async def test_settings_keyboard_focus_starts_on_plugin_list_in_plugins(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o", "shift+tab", "enter", "down")
        assert app.query_one(SettingsView).selected_plugin() == "files"


@pytest.mark.anyio
async def test_non_sidebar_manifest_is_a_generic_header_action(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    custom = manager.manifests["files"].model_copy(update={"id": "custom", "name": "Custom", "placement": "header"})
    views: list[tuple[PluginManifest, ViewPlugin]] = [
        (manager.manifests["files"], FilesPlugin()), (custom, FilesPlugin())
    ]
    app = DesktopApp(app.context, views)
    async with app.run_test(size=(80, 24)) as pilot:
        assert len(app.query_one("#destinations", ListView).children) == 1
        await pilot.click("#header-custom")
        assert app.query_one(ContentSwitcher).current == "view-custom"


@pytest.mark.anyio
async def test_disabled_settings_has_no_header_action(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.disable("settings")
    app = DesktopApp(app.context, [(manager.manifests["files"], FilesPlugin())])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        assert not app.query("#header-settings")
        assert app.query_one(ContentSwitcher).current == "view-files"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "initial", "expected"),
    [
        ("install", "available", "installed"),
        ("enable", "available", "enabled"),
        ("disable", "enabled", "installed"),
        ("uninstall", "installed", "available"),
    ],
)
async def test_plugin_management_actions_update_real_state(
    tmp_path: Path, operation: str, initial: str, expected: str
) -> None:
    app, manager = make_app(tmp_path)
    baseline = manager.store.read()
    installed = baseline.installed | ({"monitor"} if initial != "available" else set())
    enabled = baseline.enabled | ({"monitor"} if initial == "enabled" else set())
    manager.store.write(PluginState(installed=frozenset(installed), enabled=frozenset(enabled)))
    if operation == "uninstall":
        manager.removal_guard = allow_removal
        manager.enable("monitor")
        manager.disable("monitor")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        if operation == "uninstall":
            def uninstall_locally(managed: PluginManager, plugin_id: str) -> OperationResult:
                return OperationResult("uninstall", plugin_id, tuple(sorted(managed.uninstall(plugin_id))))

            view.uninstall_runner = uninstall_locally
        await pilot.pause()
        await pilot.click(f"#plugin-{'install' if operation == 'uninstall' else operation}")
        if operation == "uninstall":
            await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        await pilot.pause()
        state = manager.store.read()
        assert ("monitor" in state.installed) == (expected != "available")
        assert ("monitor" in state.enabled) == (expected == "enabled")
        assert "Restart desktop" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_enable_failure_is_visible_and_does_not_change_state(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": ("tdnext-unavailable-for-test",)}
    )
    manager.manifests = manifests
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.click("#plugin-enable")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert manager.store.read() == before
        assert "Operation failed" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_disable_confirms_and_reports_dependent_cascade(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path, monitor=True)
    manifests = dict(manager.manifests)
    manifests["extra"] = manifests["files"].model_copy(
        update={"id": "extra", "name": "Extra", "requires_plugins": ("monitor",)}
    )
    manager.manifests = manifests
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"extra"}, enabled=state.enabled | {"extra"}))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.click("#plugin-disable")
        assert any("extra" in text_of(item) for item in app.screen.query(Static))
        await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not ({"monitor", "extra"} & manager.store.read().enabled)
        assert "extra" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_escape_cancels_uninstall_confirmation_without_leaving_settings(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.install("monitor")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.click("#plugin-install")
        await pilot.press("escape")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        assert "monitor" in manager.store.read().installed


@pytest.mark.anyio
async def test_slow_operation_keeps_back_responsive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, manager = make_app(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = manager.enable

    def slow_enable(plugin_id: str, *, approved: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test operation timed out")
        return original(plugin_id, approved=approved)

    monkeypatch.setattr(manager, "enable", slow_enable)
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.press("alt+o")
            await pilot.click("#section-plugins")
            view = app.query_one(SettingsView)
            view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
            await pilot.pause()
            await pilot.click("#plugin-enable")
            assert started.wait(timeout=1)
            await pilot.press("escape")
            assert app.query_one(ContentSwitcher).current == "view-files"
        finally:
            release.set()
        await app.workers.wait_for_complete()
    assert "monitor" in manager.store.read().enabled
