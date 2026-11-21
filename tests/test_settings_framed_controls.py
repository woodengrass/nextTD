from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import Button, DataTable, Select
from textual.widgets._select import SelectOverlay

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.models import PluginState
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_sections_are_framed_and_their_edges_are_clickable(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    app, _manager = make_app(tmp_path)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        plugins = app.query_one("#section-plugins", Button)
        assert plugins.region.height >= 3
        assert all(edge[0] != "none" for edge in plugins.styles.border)
        await pilot.click(plugins, offset=(0, 1))
        assert app.query_one(SettingsView).section == "plugins"
        general = app.query_one("#section-general", Button)
        await pilot.click(general, offset=(general.region.width - 1, 1))
        assert app.query_one(SettingsView).section == "general"


@pytest.mark.anyio
async def test_section_frame_does_not_change_with_focus_or_hover(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        button = app.query_one("#section-plugins", Button)
        border = button.styles.border
        await pilot.press("shift+tab")
        assert app.focused is button
        assert button.styles.border == border
        await pilot.hover(button, offset=(0, 1))
        assert button.styles.border == border


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["provider", "mode"])
@pytest.mark.parametrize("position", ["center", "label", "edge"])
async def test_appearance_frame_click_opens_and_mouse_choice_persists(
    tmp_path: Path, field: str, position: str
) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    async with app.run_test(size=(80, 24)) as pilot:
        frame = app.query_one(f"#icon-{field}-frame")
        selector = app.query_one(f"#icon-{field}", Select)
        assert frame.region.height == 3
        assert all(side[0] != "none" for side in frame.styles.border)
        offset = {"center": frame.region.width // 2, "label": 1, "edge": 0}[position], 1
        await pilot.click(frame, offset=offset)
        assert selector.expanded
        overlay = selector.query_one(SelectOverlay)
        await pilot.click(overlay, offset=(1, 1))
        expected = Appearance(provider_id="icons") if field == "provider" else Appearance(icon_mode="codicons")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == expected


@pytest.mark.anyio
async def test_clicking_plugin_row_targets_that_plugin_for_an_action(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        table = app.query_one("#plugin-list", DataTable)
        row = view.plugin_ids.index("monitor")
        await pilot.click(offset=(table.content_region.x + 1, table.content_region.y + 1 + row))
        assert view.selected_plugin() == "monitor"
        await pilot.click("#plugin-enable")
        await app.workers.wait_for_complete()
        assert "monitor" in manager.store.read().enabled


@pytest.mark.anyio
async def test_disabled_action_cannot_be_triggered_by_its_frame_or_shortcut(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("desktop"))
        await pilot.pause()
        button = view.query_one("#plugin-uninstall", Button)
        assert button.disabled
        await pilot.click(button, offset=(0, 1))
        await pilot.press("u")
        assert manager.store.read() == before
        assert len(app.screen_stack) == 1


@pytest.mark.anyio
async def test_mouse_uninstall_cancel_keeps_the_selected_plugin_installed(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"monitor"}, enabled=state.enabled))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        table = view.query_one(DataTable)
        row = view.plugin_ids.index("monitor")
        await pilot.click(offset=(table.content_region.x + 1, table.content_region.y + 1 + row))
        await pilot.click("#plugin-uninstall")
        assert app.focused is app.screen.query_one("#cancel", Button)
        await pilot.click("#cancel")
        assert "monitor" in manager.store.read().installed


@pytest.mark.anyio
async def test_native_narrow_pane_keeps_controls_reachable(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path, monitor=True)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=(35, 24)) as pilot:
        await pilot.pause()
        provider = app.query_one("#icon-provider-frame")
        mode = app.query_one("#icon-mode-frame")
        assert provider.region.bottom <= mode.region.y
        assert mode.region.right <= app.screen.region.right
        await pilot.click("#section-plugins")
        actions = app.query_one("#plugin-actions")
        buttons = list(actions.query(Button))
        assert actions.content_region.contains_region(buttons[1].region)
        assert app.query_one("#settings-intro").region.height >= 3
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        assert "monitor" in manager.store.read().enabled
        await pilot.press("tab", "tab")
        uninstall = app.query_one("#plugin-uninstall", Button)
        assert app.focused is uninstall
        assert app.query_one("#plugin-actions").content_region.contains_region(uninstall.region)
