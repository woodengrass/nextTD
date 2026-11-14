from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ConfirmPluginChange(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmPluginChange { align: center middle; }
    ConfirmPluginChange > Vertical { width: 64; max-width: 90%; height: auto; padding: 1 2; background: $surface; }
    ConfirmPluginChange #plugin-confirmation-message { height: auto; overflow-x: hidden; }
    ConfirmPluginChange #plugin-confirmation-message:focus { background: $boost; }
    ConfirmPluginChange Horizontal { height: 3; align-horizontal: right; margin-top: 1; }
    ConfirmPluginChange Horizontal Button { border: solid $foreground-muted !important; }
    ConfirmPluginChange.narrow Horizontal { layout: vertical; height: 6; }
    ConfirmPluginChange.narrow Horizontal Button { width: 1fr; min-width: 0; }
    """

    def __init__(self, title: str, details: str, action: str) -> None:
        super().__init__()
        self.title_text = title
        self.details = details
        self.confirm_action = action

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll(id="plugin-confirmation-message"):
                yield Label(self.title_text)
                yield Static(self.details, markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_action, id="confirm-plugin-change", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_resize(self, event: Resize) -> None:
        narrow = event.size.width < 40
        self.set_class(narrow, "narrow")
        self.query_one("#plugin-confirmation-message", VerticalScroll).styles.max_height = max(
            1, event.size.height - (9 if narrow else 6)
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-plugin-change")

    def action_cancel(self) -> None:
        self.dismiss(False)
