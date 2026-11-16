from collections.abc import Sequence

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Static

from tdnext.api import ViewPlugin
from tdnext.core.models import PluginManifest


class HeaderBar(Horizontal):
    DEFAULT_CSS = """
    HeaderBar { height: 1; background: $surface; }
    HeaderBar #desktop-label { width: 1fr; height: 1; padding: 0 1; text-style: bold; }
    HeaderBar Button { height: 1; min-width: 0; width: auto; border: none; padding: 0 1; }
    """

    class OpenView(Message):
        def __init__(self, plugin_id: str) -> None:
            super().__init__()
            self.plugin_id = plugin_id

    def __init__(self, views: Sequence[tuple[PluginManifest, ViewPlugin]]) -> None:
        super().__init__()
        self.views = views

    def compose(self) -> ComposeResult:
        yield Static("TERMINAL DESKTOP", id="desktop-label")
        for index, (manifest, _) in enumerate(self.views):
            suffix = " [Alt+o]" if index == 0 else ""
            yield Button(f"{manifest.name}{suffix}", id=f"header-{manifest.id}", compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        for manifest, _ in self.views:
            if event.button.id == f"header-{manifest.id}":
                self.post_message(self.OpenView(manifest.id))
                return
