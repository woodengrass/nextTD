from textual.app import App, ComposeResult
from textual.events import AppFocus
from textual.widget import Widget

from tdnext.api import ActivatableView, CloseView, PluginContext, ViewPlugin
from tdnext.core.errors import PluginError

from .navigation import back_from_header


class ToolPane(App[None]):
    BINDINGS = [("escape", "close_view", "Back")]

    def __init__(self, plugin_id: str, plugin: ViewPlugin, context: PluginContext) -> None:
        super().__init__()
        self.plugin_id = plugin_id
        self.plugin = plugin
        self.context = PluginContext(context.directory, context.manager, context.session, plugin_id)
        self.view: Widget | None = None

    def compose(self) -> ComposeResult:
        self.view = self.plugin.make_view(self.context)
        yield self.view

    def on_app_focus(self, _event: AppFocus) -> None:
        if self.view is not None and self.view.is_mounted and isinstance(self.view, ActivatableView):
            self.view.on_native_activation()

    def on_close_view(self, _event: CloseView) -> None:
        self.action_close_view()

    def action_close_view(self) -> None:
        try:
            back_from_header()
        except PluginError as exc:
            self.notify(str(exc), severity="error")
