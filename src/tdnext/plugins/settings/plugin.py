from textual.app import App, ComposeResult
from textual.widget import Widget

from tdnext.api import CloseView, PluginContext

from .view import SettingsView


class SettingsPane(App[None]):
    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        yield SettingsView(self.context)

    def on_close_view(self, _event: CloseView) -> None:
        self.exit()


class SettingsPlugin:
    shortcuts = "Esc Back   Tab Sections and controls"

    def make_view(self, context: PluginContext) -> Widget:
        return SettingsView(context)

    def run_pane(self, context: PluginContext) -> None:
        SettingsPane(context).run()
