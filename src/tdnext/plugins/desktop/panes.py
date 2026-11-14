"""Native Zellij rail and contextual footer, generated from enabled plugins."""

import os
import subprocess
from pathlib import Path

import typer
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.events import MouseDown, MouseUp
from textual.widgets import Button, Label, ListItem, ListView, Static

from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.errors import PluginError
from tdnext.core.local_env import bootstrap as _bootstrap_local_path
from tdnext.core.models import PluginManifest
from tdnext.core.session import SESSION_ENV, SessionSnapshot, current_session
from tdnext.launcher import enabled_views
from tdnext.runtime import manager

from . import navigation
from .header import HeaderBar
from .sidebar import sidebar_label
from .tool import ToolPane

cli = typer.Typer(pretty_exceptions_enable=False)


class SidebarPane(App[None]):
    CSS = """
    Screen { background: $surface; padding: 1 1; }
    #rail-title { height: 2; text-style: bold; }
    #rail-label { height: 2; color: $text-muted; }
    ListView { height: 1fr; background: $surface; }
    ListItem { height: 1; }
    #rail-help { height: 3; color: $text-muted; }
    """

    def __init__(
        self, views: list[tuple[PluginManifest, ViewPlugin]], active: str, session: SessionSnapshot | None = None
    ) -> None:
        super().__init__()
        self.views = views
        self.active = active
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("WORKSPACE", id="rail-title")
            yield Static("VIEWS", id="rail-label")
            yield ListView(
                *(ListItem(Label(
                    f"{'>' if item.id == self.active else ' '} {sidebar_label(item, self.session)}", markup=False
                ))
                  for item, _ in self.views)
            )
            yield Static("Alt+1..9 switch\nCtrl+q quit", id="rail-help")

    def on_mount(self) -> None:
        menu = self.query_one(ListView)
        menu.index = next((i for i, (item, _) in enumerate(self.views) if item.id == self.active), 0)

    def on_list_view_selected(self, _event: ListView.Selected) -> None:
        index = self.query_one(ListView).index
        if index is None:
            return
        try:
            result = subprocess.run(
                [os.environ["TDNEXT_ZELLIJ"], "action", "go-to-tab", str(index + 1)],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if result.returncode:
                self.notify(f"Cannot switch view: {result.stderr.strip()}", severity="error")
        except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
            self.notify(f"Cannot switch view: {exc}", severity="error")


class FooterPane(App[None]):
    CSS = """
    Screen { background: $surface; }
    Static { height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    def __init__(
        self, active: tuple[PluginManifest, ViewPlugin], views: list[tuple[PluginManifest, ViewPlugin]]
    ) -> None:
        super().__init__()
        self.manifest, plugin = active
        self.shortcuts = plugin.shortcuts
        self.views = views

    def compose(self) -> ComposeResult:
        global_hints = ["Ctrl+q quit", "Ctrl+p panes"]
        if any(manifest.placement == "header" for manifest, _ in self.views):
            global_hints.append("Alt+o header")
        sidebar_count = min(9, sum(manifest.placement == "sidebar" for manifest, _ in self.views))
        if sidebar_count == 1:
            global_hints.append("Alt+1 view")
        elif sidebar_count > 1:
            global_hints.append(f"Alt+1..{sidebar_count} views")
        yield Static(f"Tool ({self.manifest.name}): {self.shortcuts}", markup=False)
        yield Static(f"Global: {' '.join(global_hints)}", markup=False)


class HeaderPane(App[None]):
    def __init__(self, views: list[tuple[PluginManifest, ViewPlugin]]) -> None:
        super().__init__()
        self.views = views
        self.mouse_down = False

    def compose(self) -> ComposeResult:
        yield HeaderBar(self.views)

    def on_mount(self) -> None:
        self.set_focus(None)

    def on_mouse_down(self, event: MouseDown) -> None:
        self.mouse_down = event.button == 1

    def on_mouse_up(self, event: MouseUp) -> None:
        # Zellij consumes the first press when focusing a pane, but forwards its release.
        if event.button == 1:
            widget, _region = self.screen.get_widget_at(event.screen_x, event.screen_y)
            if isinstance(widget, Button) and isinstance(widget.parent, HeaderBar) and not self.mouse_down:
                widget.press()
            elif not isinstance(widget, Button):
                try:
                    navigation.restore_header_focus()
                except PluginError as exc:
                    self.notify(str(exc), severity="error")
        self.mouse_down = False

    def on_header_bar_open_view(self, event: HeaderBar.OpenView) -> None:
        try:
            navigation.open_header(event.plugin_id)
        except PluginError as exc:
            self.notify(str(exc), severity="error")


@cli.command()
def sidebar(active: str, snapshot: str | None = None) -> None:
    session = current_session(snapshot)
    views = [view for view in enabled_views(manager(), session) if view[0].placement == "sidebar"]
    os.environ.pop(SESSION_ENV, None)
    SidebarPane(views, active, session).run()


@cli.command()
def header(active: str, snapshot: str | None = None) -> None:
    views = [view for view in enabled_views(manager(), current_session(snapshot)) if view[0].placement == "header"]
    os.environ.pop(SESSION_ENV, None)
    HeaderPane(views).run()


@cli.command("open-header")
def open_header_shortcut(snapshot: str | None = None) -> None:
    views = enabled_views(manager(), current_session(snapshot))
    os.environ.pop(SESSION_ENV, None)
    for manifest, _ in views:
        if manifest.placement == "header":
            navigation.open_header(manifest.id)
            return


@cli.command()
def tool(active: str, snapshot: str | None = None) -> None:
    plugins = manager()
    session = current_session(snapshot)
    views = enabled_views(plugins, session)
    os.environ.pop(SESSION_ENV, None)
    for manifest, plugin in views:
        if manifest.id == active and manifest.placement == "header":
            context = PluginContext(Path(os.environ["TDNEXT_DIRECTORY"]), plugins, session)
            ToolPane(active, plugin, context).run()
            return
    raise PluginError(f"Header view not enabled: {active}")


@cli.command()
def footer(active: str, snapshot: str | None = None) -> None:
    views = enabled_views(manager(), current_session(snapshot))
    os.environ.pop(SESSION_ENV, None)
    for manifest, plugin in views:
        if manifest.id == active:
            FooterPane((manifest, plugin), views).run()
            return
    raise PluginError(f"View not enabled: {active}")


def main() -> None:
    _bootstrap_local_path()
    try:
        cli(standalone_mode=False)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
