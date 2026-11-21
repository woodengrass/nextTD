from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.events import AppFocus
from textual.widgets import Select

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin


@pytest.mark.anyio
async def test_native_focus_refreshes_saved_appearance_without_changing_focus(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    manager.enable("icons")
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        focus = app.focused
        appearance = Appearance(provider_id="icons", icon_mode="codicons")
        AppearanceStore(manager.store.path.with_name("appearance.json")).write(appearance)
        app.post_message(AppFocus())
        await pilot.pause()
        assert app.query_one("#icon-provider", Select).value == "icons"
        assert app.query_one("#icon-mode", Select).value == "codicons"
        assert app.focused is focus


@pytest.mark.anyio
async def test_unchanged_native_focus_does_not_rebuild_provider_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        selector = app.query_one("#icon-provider", Select)
        calls: list[bool] = []
        original = selector.set_options

        def record(options):
            calls.append(True)
            original(options)

        monkeypatch.setattr(selector, "set_options", record)
        app.post_message(AppFocus())
        await pilot.pause()
        assert calls == []
