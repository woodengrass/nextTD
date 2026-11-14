from pathlib import Path

import pytest
from test_settings_ui import make_app, text_of
from textual.widgets import Button, ContentSwitcher, DataTable, Select, Static

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_general_opens_with_appearance_focused_and_plugins_hidden(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    app, _manager = make_app(tmp_path)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        await pilot.press("alt+o")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"
        assert app.focused is app.query_one("#icon-provider", Select)
        assert not app.query_one("#settings-plugins").display
        assert app.query_one("#settings-general").display


@pytest.mark.anyio
async def test_keyboard_section_navigation_moves_focus_into_each_section(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o", "shift+tab")
        assert app.focused is app.query_one("#section-plugins", Button)
        await pilot.press("enter")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-plugins"
        assert app.focused is app.query_one("#plugin-list", DataTable)
        await pilot.press("shift+tab", "shift+tab", "enter")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"
        assert app.focused is app.query_one("#icon-provider", Select)


@pytest.mark.anyio
async def test_pointer_switches_sections_without_losing_plugin_selection(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.click("#section-general")
        await pilot.click("#section-plugins")
        assert view.selected_plugin() == "monitor"
        assert app.focused is table


@pytest.mark.anyio
async def test_keyboard_appearance_change_persists_through_section_changes_and_reopen(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o", "enter", "down", "enter", "tab", "enter", "down", "enter")
        await pilot.click("#section-plugins")
        await pilot.click("#section-general")
        await pilot.press("escape", "alt+o")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == Appearance(
            provider_id="icons", icon_mode="codicons"
        )
        assert app.query_one("#icon-provider", Select).value == "icons"
        assert app.query_one("#icon-mode", Select).value == "codicons"
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"


@pytest.mark.anyio
async def test_another_session_refreshes_general_after_plugin_section_was_open(tmp_path: Path) -> None:
    first, manager = make_app(tmp_path)
    manager.enable("icons")
    second = DesktopApp(first.context, first.views)
    async with first.run_test(size=(80, 24)) as first_pilot:
        await first_pilot.press("alt+o")
        await first_pilot.click("#section-plugins")
        async with second.run_test(size=(80, 24)) as second_pilot:
            await second_pilot.press("alt+o", "enter", "down", "enter")
        await first_pilot.click("#section-general")
        assert first.query_one("#icon-provider", Select).value == "icons"


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_plugin_controls_and_feedback_fit_the_desktop(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    app, _manager = make_app(tmp_path)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        table = app.query_one("#plugin-list")
        actions = app.query_one("#plugin-actions")
        feedback = app.query_one("#plugin-feedback")
        assert table.region.height >= 3
        assert table.region.bottom <= actions.region.y
        assert actions.region.bottom <= feedback.region.y
        assert feedback.region.bottom <= app.screen.region.bottom - 2
        for button in actions.query(Button):
            assert button.region.width >= 8
            assert button.region.right <= app.screen.region.right
        shortcuts = text_of(app.query_one("#settings-intro", Static))
        assert all(hint in shortcuts for hint in ("i Install", "e Enable", "d Disable", "u Uninstall"))


@pytest.mark.anyio
async def test_native_tool_pane_uses_the_same_section_navigation(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        assert app.focused is app.query_one("#icon-provider", Select)
        await pilot.press("shift+tab", "enter")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-plugins"
        assert app.focused is app.query_one("#plugin-list", DataTable)
