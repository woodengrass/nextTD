from pathlib import Path

import pytest
from pydantic import ValidationError
from test_settings_ui import make_app
from textual.content import Content
from textual.widgets import Label, ListView

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
        assert displayed == ["▤    Files", "▥    Monitor", ">_   Shell"]
    native = SidebarPane(views, "shell")
    async with native.run_test(size=(22, 20)):
        displayed = labels(native.query_one(ListView))
        assert displayed == ["  ▤    Files", "  ▥    Monitor", "> >_   Shell"]
