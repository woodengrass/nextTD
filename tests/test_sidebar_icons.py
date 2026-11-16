from pathlib import Path

import pytest
from pydantic import ValidationError
from test_settings_ui import make_app
from textual.content import Content
from textual.widgets import ContentSwitcher, Label, ListView

from tdnext.api import ViewPlugin
from tdnext.core.models import PluginManifest
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.panes import SidebarPane
from tdnext.plugins.shell.plugin import ShellPlugin


def test_manifest_icon_is_optional_and_safe(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    base = manager.manifests["files"].model_dump(exclude={"icon"})
    assert PluginManifest.model_validate(base).icon is None
    for invalid in ("", " ", "a\nb", "\x1b[31m", "💻", "\u200b", "abcde"):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({**base, "icon": invalid})
    assert PluginManifest.model_validate({**base, "icon": ">_"}).icon == ">_"


def labels(menu: ListView) -> list[str]:
    result = []
    for label in menu.query(Label):
        content = label.render()
        assert isinstance(content, Content)
        result.append(content.plain)
    return result


@pytest.mark.anyio
async def test_native_and_fallback_sidebars_show_safe_icons_with_labels(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path, monitor=True)
    views: list[tuple[PluginManifest, ViewPlugin]] = [
        *app.sidebar_views, (manager.manifests["shell"], ShellPlugin())
    ]
    fallback = DesktopApp(app.context, views)
    async with fallback.run_test(size=(80, 24)):
        displayed = labels(fallback.query_one("#destinations", ListView))
        assert displayed == ["> ▤    Files", "  ▥    Monitor", "  >_   Shell"]
    native = SidebarPane(views, "shell")
    async with native.run_test(size=(22, 20)):
        displayed = labels(native.query_one(ListView))
        assert displayed == ["  ▤    Files", "  ▥    Monitor", "> >_   Shell"]


@pytest.mark.anyio
async def test_fallback_toggle_reclaims_width_and_keeps_navigation(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    async with app.run_test(size=(80, 24)) as pilot:
        workspace = app.query_one("#workspace")
        original_width = workspace.size.width
        original_view = app.query_one("#view-files").children[0]
        menu = app.query_one("#destinations", ListView)
        positions = [item.region.y for item in menu.children]
        assert all(item.region.height == 3 for item in menu.children)
        await pilot.press("f7")
        assert workspace.size.width > original_width
        assert [item.region.y for item in menu.children] == positions
        assert labels(app.query_one("#destinations", ListView)) == ["> ▤", "  ▥"]
        await pilot.press("f6", "down", "enter")
        assert app.query_one(ContentSwitcher).current == "view-monitor"
        assert labels(app.query_one("#destinations", ListView)) == ["  ▤", "> ▥"]
        await pilot.click("#rail-toggle")
        assert workspace.size.width == original_width
        assert app.query_one("#view-files").children[0] is original_view
        assert labels(app.query_one("#destinations", ListView)) == ["  ▤    Files", "> ▥    Monitor"]


@pytest.mark.anyio
async def test_fallback_missing_icons_are_distinguishable(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    views = [(manifest.model_copy(update={"icon": None}), plugin) for manifest, plugin in app.sidebar_views]
    app = DesktopApp(app.context, views)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("f7")
        menu = app.query_one("#destinations", ListView)
        assert labels(menu) == ["> [1]", "  [2]"]
        assert [item.tooltip for item in menu.children] == ["Files", "Monitor"]


@pytest.mark.anyio
async def test_native_sidebar_renders_icons_after_pane_resize(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path, monitor=True)
    native = SidebarPane(app.sidebar_views, "monitor")
    async with native.run_test(size=(21, 20)) as pilot:
        menu = native.query_one(ListView)
        positions = [item.region.y for item in menu.children]
        assert all(item.region.height == 3 for item in menu.children)
        await pilot.resize_terminal(9, 20)
        assert [item.region.y for item in menu.children] == positions
        assert labels(menu) == ["  ▤", "> ▥"]
        assert [item.tooltip for item in menu.children] == ["Files", "Monitor"]
        await pilot.resize_terminal(21, 20)
        assert labels(menu) == ["  ▤    Files", "> ▥    Monitor"]
