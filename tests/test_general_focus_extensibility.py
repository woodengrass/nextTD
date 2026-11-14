from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Select

from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("host", "size"), [("pane", (35, 24)), ("pane", (120, 36)), ("desktop", (80, 24))]
)
async def test_added_general_controls_are_reached_once_in_each_direction(
    tmp_path: Path, host: str, size: tuple[int, int]
) -> None:
    desktop, manager = make_app(tmp_path)
    app = desktop if host == "desktop" else ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        if host == "desktop":
            await pilot.press("alt+o")
        general = app.query_one("#settings-general", VerticalScroll)
        first = Button("First additional setting", id="extra-first")
        second = Button("Second additional setting", id="extra-second")
        group = Vertical(first, second)
        group.styles.height = "auto"
        await general.mount(group)
        await pilot.pause()

        mode = app.query_one("#icon-mode", Select)
        feedback = app.query_one("#settings-feedback-scroll", VerticalScroll)
        editor = app.query_one("#editor-choice", Select)
        chain = app.screen.focus_chain
        external = chain[(chain.index(feedback) + 1) % len(chain)]
        for expected in (mode, feedback, editor, first, second, external):
            await pilot.press("tab")
            assert app.focused is expected
            if expected in (first, second):
                assert general.content_region.contains_region(expected.region)
        for expected in (second, first, editor, feedback, mode):
            await pilot.press("shift+tab")
            assert app.focused is expected
        if size == (35, 24):
            assert general.max_scroll_y > 0
        assert not manager.store.path.with_name("appearance.json").exists()
        assert not manager.store.path.with_name("editor.json").exists()


@pytest.mark.anyio
async def test_general_traversal_skips_disabled_and_hidden_added_controls(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(35, 24)) as pilot:
        general = app.query_one("#settings-general", VerticalScroll)
        disabled = Button("Disabled", disabled=True)
        hidden = Button("Hidden")
        hidden.display = False
        available = Button("Available")
        group = Vertical(disabled, hidden, available)
        group.styles.height = "auto"
        await general.mount(group)
        await pilot.press("tab", "tab", "tab", "tab")
        assert app.focused is available
        await pilot.press("shift+tab")
        assert app.focused is app.query_one("#editor-choice", Select)


@pytest.mark.anyio
async def test_open_selector_keeps_its_escape_and_tab_behavior(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        general = app.query_one("#settings-general", VerticalScroll)
        extra = Button("Additional setting")
        await general.mount(extra)
        editor = app.query_one("#editor-choice", Select)
        await pilot.press("tab", "tab", "tab", "enter")
        assert editor.expanded
        await pilot.press("escape")
        assert app.focused is editor
        assert not editor.expanded
        await pilot.press("enter", "tab")
        assert not editor.expanded
        assert app.focused is extra
        assert not manager.store.path.with_name("editor.json").exists()
