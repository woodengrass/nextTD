from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import ListView, Static

from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.panes import FooterPane
from tdnext.plugins.files.plugin import FilesPlugin


def visible_rows(widget: Static) -> list[str]:
    return [widget.render_line(row).text.rstrip() for row in range(widget.size.height)]


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)], ids=["80x24", "wide"])
async def test_fallback_footer_tracks_active_view_and_preserves_global_row(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    async with app.run_test(size=size) as pilot:
        footer = app.query_one("#shortcuts", Static)
        tool, desktop = visible_rows(footer)
        assert all(hint in tool for hint in ("Tool (Files):", "Enter open", "Backspace up", "? help"))
        assert all(key in desktop for key in ("Global:", "Ctrl+q", "Ctrl+t", "Tab", "Alt+o"))
        assert footer.region.bottom == size[1]
        if size[0] == 80:
            assert tool.endswith("…")
        else:
            assert "? help" in tool

        theme = app.theme
        await pilot.press("ctrl+t")
        assert app.theme != theme
        rail = app.query_one("#destinations", ListView)
        rail.focus()
        rail.index = 1
        await pilot.press("enter")
        tool, desktop_after = visible_rows(footer)
        assert "Tool (Monitor):" in tool and "Enter open btop" in tool and "q return inside btop" in tool
        assert desktop_after == desktop

        await pilot.press("alt+o")
        tool, desktop_after = visible_rows(footer)
        assert "Tool (Settings):" in tool and "Esc Back" in tool
        assert "i Install" not in tool
        assert desktop_after == desktop
        await pilot.click("#section-plugins")
        hints = "\n".join(visible_rows(app.query_one("#settings-intro", Static)))
        assert all(hint in hints for hint in ("i Install", "e Enable", "d Disable", "u Uninstall"))

        await pilot.press("escape")
        assert "Tool (Monitor):" in visible_rows(footer)[0]


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)], ids=["80x24", "wide"])
async def test_fallback_footer_omits_header_shortcut_when_header_disabled(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app, manager = make_app(tmp_path)
    app = DesktopApp(app.context, [(manager.manifests["files"], FilesPlugin())])
    async with app.run_test(size=size) as pilot:
        await pilot.press("alt+o")
        tool, desktop = visible_rows(app.query_one("#shortcuts", Static))
        assert "Tool (Files):" in tool
        assert all(key in desktop for key in ("Global:", "Ctrl+q", "Ctrl+t", "Tab"))
        assert "Alt+o" not in desktop


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(57, 2), (80, 24), (120, 36)], ids=["80-column-pane", "80x24", "wide"])
@pytest.mark.parametrize("header", [False, True], ids=["header-disabled", "header-enabled"])
async def test_native_footer_shows_only_configured_global_shortcuts(
    tmp_path: Path, size: tuple[int, int], header: bool
) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    views = app.views if header else app.sidebar_views
    footer = FooterPane(views[0], views)
    async with footer.run_test(size=size):
        tool_widget, desktop_widget = footer.query(Static)
        tool = visible_rows(tool_widget)[0]
        desktop = visible_rows(desktop_widget)[0]
        assert all(hint in tool for hint in ("Tool (Files):", "Enter open", "Backspace up", "? help"))
        assert all(key in desktop for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+1..2"))
        assert ("Alt+o" in desktop) is header
        assert "Alt+1..9" not in desktop
        assert "Ctrl+t" not in desktop and "Tab focus" not in desktop


@pytest.mark.anyio
@pytest.mark.parametrize(("sidebar_count", "expected"), [(0, None), (1, "Alt+1 view"), (10, "Alt+1..9 views")])
async def test_native_numbered_hints_match_sidebar_bindings(
    tmp_path: Path, sidebar_count: int, expected: str | None
) -> None:
    app, _manager = make_app(tmp_path)
    files, settings = app.views
    views = [(files[0].model_copy(update={"id": f"view-{index}"}), files[1]) for index in range(sidebar_count)]
    views.append(settings)
    footer = FooterPane(settings, views)
    async with footer.run_test(size=(80, 24)):
        tool_widget, desktop_widget = footer.query(Static)
        assert "Tool (Settings):" in visible_rows(tool_widget)[0]
        desktop = visible_rows(desktop_widget)[0]
        assert ("Alt+1" in desktop) is (expected is not None)
        if expected is not None:
            assert expected in desktop
        assert "Alt+1..10" not in desktop
