from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.containers import VerticalScroll
from textual.widgets import Select
from textual.widgets._select import SelectOverlay

from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.files.editor_preferences import EditorPreferencesStore
from tdnext.plugins.settings.plugin import SettingsPlugin


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_editor_options_occupy_clean_rows_without_obscuring_feedback(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane(SettingsPlugin(), desktop.context)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        await pilot.press("tab", "tab", "tab")
        feedback = app.query_one("#settings-feedback-scroll", VerticalScroll)
        before = app.screen._compositor.render_strips()
        feedback_before = [
            before[y].crop(feedback.region.x, feedback.region.right).text
            for y in range(feedback.region.y, feedback.region.bottom)
        ]

        await pilot.press("enter")
        selector = app.query_one("#editor-choice", Select)
        overlay = selector.query_one(SelectOverlay)
        frame = app.query_one("#editor-choice-frame")
        general = app.query_one("#settings-general", VerticalScroll)
        rows = app.screen._compositor.render_strips()
        for offset, option in enumerate(("Environment", "Neovim (nvim)", "Vi (vi)")):
            row = rows[overlay.region.y + offset].crop(frame.region.x, frame.region.right).text
            assert row.strip(" │") == option
        assert general.content_region.contains_region(frame.region)
        assert frame.content_region.contains_region(overlay.region)
        assert app.query_one("#editor-help").region.y >= frame.region.bottom
        assert [
            rows[y].crop(feedback.region.x, feedback.region.right).text
            for y in range(feedback.region.y, feedback.region.bottom)
        ] == feedback_before
        await pilot.press("escape")
        assert app.focused is selector
        assert frame.region.height == 3
        assert app.query_one("#editor-help").display


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24), (120, 36)])
async def test_mouse_can_select_bottom_editor_option_after_expansion(tmp_path: Path, size: tuple[int, int]) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.press("tab", "tab", "tab")
        frame = app.query_one("#editor-choice-frame")
        selector = app.query_one("#editor-choice", Select)
        await pilot.click(frame, offset=(0, 1))
        assert selector.expanded
        await pilot.click(selector.query_one(SelectOverlay), offset=(1, 2))
        assert not selector.expanded
        assert app.focused is selector
        assert EditorPreferencesStore(manager.store.path.with_name("editor.json")).read().editor == "vi"


@pytest.mark.anyio
async def test_open_editor_menu_stays_visible_when_terminal_narrows(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("tab", "tab", "tab", "enter")
        selector = app.query_one("#editor-choice", Select)
        await pilot.resize_terminal(35, 24)
        frame = app.query_one("#editor-choice-frame")
        general = app.query_one("#settings-general", VerticalScroll)
        assert selector.expanded
        assert general.content_region.contains_region(frame.region)
        await pilot.press("down", "enter")
        assert selector.value == "nvim"
