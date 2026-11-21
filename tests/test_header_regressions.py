from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.binding import Binding
from textual.widgets import Button, DataTable, Static

from tdnext.api import PluginContext
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


class ShortcutConsumer(Static):
    can_focus = True
    BINDINGS = [
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


@pytest.mark.anyio
async def test_unreserved_shortcut_remains_with_focused_plugin(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    pane = ToolPane(ConsumingPlugin(), app.context)  # type: ignore[arg-type]
    async with pane.run_test(size=(80, 24)) as pilot:
        consumer = pane.query_one(ShortcutConsumer)
        consumer.focus()
        await pilot.press("x")
        assert consumer.consumed == 1


@pytest.mark.anyio
async def test_settings_refreshes_each_time_it_reopens_without_losing_selection(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    first = ToolPane(SettingsPlugin(), desktop.context)
    async with first.run_test(size=(80, 24)) as pilot:
        view = first.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        assert view.selected_plugin() == "monitor"

        manager.enable("monitor")
    second = ToolPane(SettingsPlugin(), desktop.context)
    async with second.run_test(size=(80, 24)) as pilot:
        view = second.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        assert view.selected_plugin() == "monitor"
        assert table.get_row("monitor")[1] == "Enabled"
        assert view.query_one("#plugin-disable", Button).disabled is False

        manager.disable("monitor")
    added = manager.manifests["monitor"].model_copy(update={"id": "aaa", "name": "AAA"})
    manager.manifests = {**manager.manifests, "aaa": added}
    third = ToolPane(SettingsPlugin(), desktop.context)
    async with third.run_test(size=(80, 24)) as pilot:
        view = third.query_one(SettingsView)
        table = view.query_one(DataTable)
        table.move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        assert view.selected_plugin() == "monitor"
        assert table.get_row("monitor")[1] == "Installed"
        assert view.query_one("#plugin-disable", Button).disabled is True
