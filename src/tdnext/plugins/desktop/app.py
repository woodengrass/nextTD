"""Textual desktop used when a native pane layout is unavailable."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import DescendantFocus
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, ContentSwitcher, Label, ListItem, ListView, Static

from tdnext.api import CloseView, PluginContext, ViewPlugin
from tdnext.core.models import PluginManifest

from .header import HeaderBar
from .sidebar import compact_icon, sidebar_label


@runtime_checkable
class FocusBoundary(Protocol):
    """Optional focus routing when a header view meets desktop controls."""

    def focus_previous_from_outside(self) -> bool: ...


class DesktopApp(App[None]):
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True), ("ctrl+t", "toggle_theme", "Theme"),
        Binding("alt+o", "open_header", "Open header action", priority=True),
        Binding("f6", "focus_sidebar", "Focus sidebar", priority=True),
        Binding("f7", "toggle_sidebar", "Toggle sidebar", priority=True),
        Binding("escape", "close_header", "Back"),
    ]
    CSS = """
    Screen { background: $background; }
    #desktop-body { height: 1fr; }
    #rail { width: 22; height: 1fr; background: $surface; padding: 1 1; }
    #rail-title { height: 2; text-style: bold; }
    #rail-hint { height: 2; color: $text-muted; }
    #rail-toggle { width: 100%; min-width: 0; height: 1; min-height: 1; border: none; padding: 0; }
    #destinations { height: 1fr; background: $surface; }
    #destinations ListItem { height: 3; align-vertical: middle; }
    #workspace { width: 1fr; height: 1fr; }
    ContentSwitcher { width: 1fr; height: 1fr; }
    .plugin-view { width: 1fr; height: 1fr; }
    #shortcuts {
        height: 2; padding: 0 1; background: $surface; color: $text;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    def __init__(self, context: PluginContext, views: Sequence[tuple[PluginManifest, ViewPlugin]]) -> None:
        super().__init__()
        self.context = context
        self.views = views
        self.sidebar_views = [(manifest, plugin) for manifest, plugin in views if manifest.placement == "sidebar"]
        self.header_views = [(manifest, plugin) for manifest, plugin in views if manifest.placement == "header"]
        first_view = self.sidebar_views or self.header_views
        self.active_view = first_view[0][0].id if first_view else None
        self.return_view: str | None = None
        self.return_focus: Widget | None = None
        self.last_workspace_focus: Widget | None = None
        self.sidebar_expanded = True

    def compose(self) -> ComposeResult:
        yield HeaderBar(self.header_views)
        with Horizontal(id="desktop-body"):
            with Vertical(id="rail"):
                yield Static("WORKSPACE", id="rail-title")
                yield Button("< Collapse (F7)", id="rail-toggle")
                yield Static("VIEWS", id="rail-hint")
                yield ListView(
                    *(ListItem(Label(sidebar_label(manifest, self.context.session), markup=False))
                      for manifest, _ in self.sidebar_views),
                    id="destinations",
                )
                yield Static("F6: sidebar\nCtrl+q: quit", id="rail-help", markup=False)
            with Vertical(id="workspace"):
                if self.active_view is None:
                    yield Static("No views enabled. Enable a view and restart the desktop.", id="empty-workspace")
                else:
                    with ContentSwitcher(initial=f"view-{self.active_view}"):
                        for manifest, plugin in self.views:
                            yield Container(
                                plugin.make_view(self.context), id=f"view-{manifest.id}", classes="plugin-view"
                            )
        yield Static(id="shortcuts", markup=False)

    def on_mount(self) -> None:
        toggle = self.query_one("#rail-toggle", Button)
        toggle.can_focus = False
        toggle.tooltip = "Collapse sidebar (F7)"
        menu = self.query_one("#destinations", ListView)
        for item, (manifest, _) in zip(menu.children, self.sidebar_views, strict=True):
            item.tooltip = manifest.name
        if self.active_view is None:
            self.query_one("#shortcuts", Static).update(
                "No active tool\nGlobal: Ctrl+q quit   F7 sidebar   Ctrl+t theme"
            )
        else:
            self.show_view(self.active_view)

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if self.active_view is None:
            return
        view = self.query_one(f"#view-{self.active_view}")
        rail = self.query_one("#rail")
        if self.return_view is None and (view in event.widget.ancestors or rail in event.widget.ancestors):
            self.last_workspace_focus = event.widget

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view is not self.query_one("#destinations", ListView):
            return
        index = self.query_one("#destinations", ListView).index
        if index is None:
            return
        self.return_view = None
        self.show_view(self.sidebar_views[index][0].id)

    def show_view(self, plugin_id: str, *, restore_focus: Widget | None = None) -> None:
        self.active_view = plugin_id
        self.update_sidebar_labels()
        self.query_one(ContentSwitcher).current = f"view-{plugin_id}"
        manifest, plugin = next(view for view in self.views if view[0].id == plugin_id)
        tool_hints = getattr(plugin, "fallback_shortcuts", plugin.shortcuts)
        focus_hint = getattr(plugin, "fallback_focus_hint", None)
        global_hints = ["Ctrl+q quit", "F7 sidebar"]
        if focus_hint is None:
            global_hints.extend(["Ctrl+t theme", "Tab focus"])
        else:
            global_hints.append(focus_hint)
        if self.header_views:
            global_hints.append("Alt+o header")
        self.query_one("#shortcuts", Static).update(
            f"Tool ({manifest.name}): {tool_hints}\nGlobal: {'   '.join(global_hints)}"
        )
        if restore_focus is not None and restore_focus.is_mounted:
            restore_focus.focus()
            return
        for widget in self.query_one(f"#view-{plugin_id}").query("*"):
            if widget.can_focus:
                widget.focus()
                break

    def on_header_bar_open_view(self, event: HeaderBar.OpenView) -> None:
        self.open_header(event.plugin_id)

    def open_header(self, plugin_id: str) -> None:
        if self.active_view == plugin_id:
            return
        if self.return_view is None:
            self.return_view = self.active_view
            focused = self.focused
            self.return_focus = self.last_workspace_focus if focused is None or isinstance(
                focused.parent, HeaderBar
            ) else focused
        self.show_view(plugin_id)

    def on_close_view(self, _event: CloseView) -> None:
        self.action_close_header()

    def action_close_header(self) -> None:
        if self.return_view is not None:
            self.show_view(self.return_view, restore_focus=self.return_focus)
            self.return_view = None
            self.return_focus = None

    def action_open_header(self) -> None:
        if self.header_views and not isinstance(self.screen, ModalScreen):
            self.open_header(self.header_views[0][0].id)

    def action_toggle_theme(self) -> None:
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"

    def action_focus_previous(self) -> None:
        if self.return_view is not None and not isinstance(self.screen, ModalScreen):
            view = self.query_one(f"#view-{self.active_view}").children[0]
            if isinstance(view, FocusBoundary) and view.focus_previous_from_outside():
                return
        super().action_focus_previous()

    def action_focus_sidebar(self) -> None:
        if self.sidebar_views and not isinstance(self.screen, ModalScreen):
            self.query_one("#destinations", ListView).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button is self.query_one("#rail-toggle", Button):
            self.action_toggle_sidebar()

    def action_toggle_sidebar(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.sidebar_expanded = not self.sidebar_expanded
        self.query_one("#rail").styles.width = 22 if self.sidebar_expanded else 9
        for selector in ("#rail-title", "#rail-hint", "#rail-help"):
            self.query_one(selector).styles.visibility = "visible" if self.sidebar_expanded else "hidden"
        toggle = self.query_one("#rail-toggle", Button)
        toggle.label = "< Collapse (F7)" if self.sidebar_expanded else ">"
        toggle.tooltip = "Collapse sidebar (F7)" if self.sidebar_expanded else "Expand sidebar (F7)"
        self.update_sidebar_labels()

    def update_sidebar_labels(self) -> None:
        menu = self.query_one("#destinations", ListView)
        for index, (item, (manifest, _)) in enumerate(zip(menu.children, self.sidebar_views, strict=True)):
            label = sidebar_label(manifest, self.context.session) if self.sidebar_expanded else compact_icon(
                manifest, self.context.session, index
            )
            item.query_one(Label).update(f"{'>' if manifest.id == self.active_view else ' '} {label}")
