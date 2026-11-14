from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import Button, ContentSwitcher

from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.plugin import SettingsPlugin


@pytest.mark.anyio
@pytest.mark.parametrize(("host", "size"), [("pane", (35, 24)), ("desktop", (80, 24)), ("desktop", (120, 36))])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_back_frame_is_fixed_and_its_edge_returns_to_the_workspace(
    tmp_path: Path, host: str, size: tuple[int, int], theme: str
) -> None:
    desktop, _manager = make_app(tmp_path)
    app = desktop if host == "desktop" else ToolPane("settings", SettingsPlugin(), desktop.context)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        if host == "desktop":
            await pilot.press("alt+o")
        await pilot.pause()
        back = app.query_one("#settings-back", Button)
        border = back.styles.border
        assert all(side[0] == "solid" for side in border)
        await pilot.press("shift+tab", "shift+tab", "shift+tab")
        assert app.focused is back
        assert back.styles.border == border
        await pilot.hover(back, offset=(0, 1))
        assert back.styles.border == border
        if host == "desktop":
            await pilot.click(back, offset=(0, 1))
            assert app.query_one(ContentSwitcher).current == "view-files"
