"""Labeled input and explicit confirmation screens for file operations."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static


class NameDialog(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    NameDialog { align: center middle; }
    NameDialog > Vertical { width: 60; max-width: 90%; height: auto; padding: 1 2; background: $surface; }
    NameDialog Input { margin: 1 0; }
    NameDialog Horizontal { height: auto; align-horizontal: right; }
    """

    def __init__(self, title: str, value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial_value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text)
            yield Input(value=self.initial_value, id="name-input")
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one(Input).value if event.button.id == "submit" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDelete(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmDelete { align: center middle; }
    ConfirmDelete > Vertical { width: 64; max-width: 90%; height: auto; padding: 1 2; background: $surface; }
    ConfirmDelete Horizontal { height: auto; align-horizontal: right; margin-top: 1; }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self.target_name = name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Delete permanently?")
            yield Static(f"{self.target_name}\nDirectories and their contents will be removed.", markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="delete", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "delete")

    def action_cancel(self) -> None:
        self.dismiss(False)


class FileHelp(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close"), ("question_mark", "close", "Close")]
    DEFAULT_CSS = """
    FileHelp { align: center middle; }
    FileHelp > Vertical { width: 68; max-width: 90%; height: auto; padding: 1 2; background: $surface; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("File shortcuts")
            yield Static(
                "Arrows/j/k: select   Enter/l: open   Backspace/h: parent\n"
                "v: preview / list   b: folders   Tab: desktop focus   g: go to path\n"
                "n: new folder   f: new file   r: rename   e: edit\n"
                "y: copy   x: cut   p: paste   d: delete permanently\n"
                ".: hidden files   Ctrl+r: refresh\n\nEscape: close",
                markup=False,
            )

    def action_close(self) -> None:
        self.dismiss(None)
