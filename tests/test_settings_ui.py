import threading
from pathlib import Path

import pytest
from textual.content import Content
from textual.widgets import ContentSwitcher, DataTable, Select, Static

from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.monitor.plugin import MonitorPlugin
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


def text_of(widget: Static) -> str:
    content = widget.render()
    assert isinstance(content, Content)
    return content.plain


def make_app(tmp_path: Path, *, monitor: bool = False, plugin: str = "settings") -> tuple[ToolPane, PluginManager]:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": (), "dependency_hook": None}
    )
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    installed = frozenset({"desktop", "files", "settings"} | ({"monitor"} if monitor else set()))
    manager.store.write(PluginState(installed=installed, enabled=installed))
    plugins: dict[str, ViewPlugin] = {"settings": SettingsPlugin()}
    if monitor:
        plugins["monitor"] = MonitorPlugin()
    return ToolPane(plugins[plugin], PluginContext(tmp_path, manager)), manager


@pytest.mark.anyio
async def test_settings_tool_pane_opens_on_general_with_appearance_focused(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)):
        assert app.query_one(SettingsView).is_on_screen
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"
        assert app.focused is app.query_one("#icon-provider", Select)


@pytest.mark.anyio
async def test_settings_keyboard_focus_starts_on_plugin_list_in_plugins(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("shift+tab", "enter", "down")
        assert app.query_one(SettingsView).selected_plugin() == "files"


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
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.click(f"#plugin-{operation}")
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
async def test_escape_cancels_uninstall_confirmation_without_leaving_plugins(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"monitor"}, enabled=state.enabled))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.click("#plugin-uninstall")
        await pilot.press("escape")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-plugins"
        assert "monitor" in manager.store.read().installed


@pytest.mark.anyio
async def test_slow_operation_keeps_settings_responsive_without_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            await pilot.click("#section-plugins")
            view = app.query_one(SettingsView)
            view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
            await pilot.pause()
            await pilot.click("#plugin-enable")
            assert started.wait(timeout=1)
            await pilot.press("escape")
            assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-plugins"
        finally:
            release.set()
        await app.workers.wait_for_complete()
    assert "monitor" in manager.store.read().enabled


@pytest.mark.anyio
async def test_settings_back_posts_close_view_for_the_native_host(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#settings-back")
        await pilot.pause()
        assert app.query_one(SettingsView).is_on_screen
