"""Files plugin entry point."""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget

from tdnext.api import PluginContext

from .view import FilesView


class FilesPane(App[None]):
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", priority=True)]

    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        yield FilesView(self.context.directory, self.context)


class FilesPlugin:
    shortcuts = (
        "Enter open   Backspace up   ? help   v preview   b folders   g path   n folder   r rename   "
        "y/x/p copy/move   d delete"
    )

    def make_view(self, context: PluginContext) -> Widget:
        return FilesView(context.directory, context)

    def run_pane(self, context: PluginContext) -> None:
        FilesPane(context).run()
