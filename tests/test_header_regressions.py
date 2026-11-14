from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, DataTable, Static

from tdnext.api import PluginContext
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.settings.view import SettingsView


class ShortcutConsumer(Static):
    can_focus = True
    BINDINGS = [
        Binding("alt+o", "consume", "Plugin", priority=True),
        Binding("ctrl+q", "consume", "Plugin", priority=True),
        Binding("x", "consume", "Plugin"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.consumed = 0

    def action_consume(self) -> None:
        self.consumed += 1


class ConsumingPlugin:
    shortcuts = ""

    def make_view(self, context: PluginContext) -> ShortcutConsumer:
        return ShortcutConsumer()

    def run_pane(self, context: PluginContext) -> None:
        raise NotImplementedError


def consuming_app(tmp_path: Path) -> DesktopApp:
    app, manager = make_app(tmp_path)
    return DesktopApp(app.context, [(manager.manifests["files"], ConsumingPlugin()), app.views[-1]])


@pytest.mark.anyio
async def test_header_shortcut_wins_over_focused_plugin_binding(tmp_path: Path) -> None:
    app = consuming_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        consumer = app.query_one(ShortcutConsumer)
        consumer.focus()
        await pilot.press("alt+o")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        assert consumer.consumed == 0


@pytest.mark.anyio
async def test_quit_shortcut_wins_over_focused_plugin_binding(tmp_path: Path) -> None:
    app = consuming_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        consumer = app.query_one(ShortcutConsumer)
        consumer.focus()
        await pilot.press("ctrl+q")
        assert not app.is_running
        assert consumer.consumed == 0


@pytest.mark.anyio
async def test_unreserved_shortcut_remains_with_focused_plugin(tmp_path: Path) -> None:
    app = consuming_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        consumer = app.query_one(ShortcutConsumer)
        consumer.focus()
        await pilot.press("x")
        assert consumer.consumed == 1


@pytest.mark.anyio
async def test_header_shortcut_does_not_navigate_behind_modal(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("n")
        assert isinstance(app.screen, ModalScreen)
        await pilot.press("alt+o")
        assert isinstance(app.screen, ModalScreen)
        await pilot.press("escape")
        assert app.query_one(ContentSwitcher).current == "view-files"


@pytest.mark.anyio
async def test_global_quit_remains_available_in_modal(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("n")
        assert isinstance(app.screen, ModalScreen)
        await pilot.press("ctrl+q")
        assert not app.is_running


@pytest.mark.anyio
async def test_settings_refreshes_each_time_it_reopens_without_losing_selection(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        view = app.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.press("escape")

        manager.enable("monitor")
        await pilot.press("alt+o")
        assert view.selected_plugin() == "monitor"
        assert table.get_row("monitor")[1] == "Enabled"
        assert view.query_one("#plugin-disable", Button).disabled is False
        await pilot.press("escape")

        manager.disable("monitor")
        added = manager.manifests["monitor"].model_copy(update={"id": "aaa", "name": "AAA"})
        manager.manifests = {**manager.manifests, "aaa": added}
        await pilot.press("alt+o")
        assert view.selected_plugin() == "monitor"
        assert table.get_row("monitor")[1] == "Installed"
        assert view.query_one("#plugin-disable", Button).disabled is True
