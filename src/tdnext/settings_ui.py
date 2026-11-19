"""Shared Textual controls for plugin-contributed settings."""

from textual.containers import Horizontal
from textual.events import Click
from textual.widgets import Select


class AppearanceField(Horizontal):
    """The whole labeled frame is a pointer target for its selector."""

    DEFAULT_CSS = """
    AppearanceField { height: 3; width: 1fr; border: solid $foreground-muted; background: $surface; }
    AppearanceField Static { height: 1; width: auto; padding-right: 1; }
    AppearanceField Select { width: 1fr; height: 1; }
    AppearanceField Select:focus > SelectCurrent { background: $boost; text-style: bold; }
    """

    def on_click(self, event: Click) -> None:
        selector = self.query_one(Select)
        if event.widget is not None and (event.widget is selector or selector in event.widget.ancestors):
            return
        if event.button == 1 and not selector.disabled:
            event.stop()
            selector.focus()
            selector.action_show_overlay()
