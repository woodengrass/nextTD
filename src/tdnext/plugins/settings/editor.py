"""File editor preference controls for General settings."""

from typing import Final, Literal, assert_never

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import DescendantFocus, Resize
from textual.message import Message
from textual.widgets import Select, Static

from tdnext.api import PluginContext
from tdnext.core.editor_preferences import EditorChoice, EditorPreferences, EditorPreferencesStore
from tdnext.core.errors import PluginError

from .appearance import AppearanceField

EDITOR_OPTIONS: Final[list[tuple[str, EditorChoice]]] = [
    ("Environment", "environment"), ("Neovim (nvim)", "nvim"), ("Vi (vi)", "vi"),
]
EditorValue = EditorChoice | Literal["unavailable"]
UNAVAILABLE_OPTIONS: Final[list[tuple[str, EditorValue]]] = [("Unavailable", "unavailable")]
EDITOR_HELP: Final = (
    "Environment uses $EDITOR, or vi if unset. Changes apply on the next file open. "
    "Editors are not installed automatically; unavailable commands are reported when opened."
)


class EditorSelect(Select[EditorValue]):
    def watch_expanded(self, expanded: bool) -> None:
        frame = self.parent
        if isinstance(frame, AppearanceField):
            frame.set_class(expanded, "editor-expanded")
            if expanded:
                self.reveal_frame()

    def on_resize(self, _event: Resize) -> None:
        if self.expanded:
            self.reveal_frame()

    def reveal_frame(self) -> None:
        frame = self.parent
        if isinstance(frame, AppearanceField):
            self.call_after_refresh(frame.scroll_visible, animate=False, immediate=True)


class EditorControls(Vertical):
    DEFAULT_CSS = """
    EditorControls { height: auto; }
    EditorControls #editor-help { height: auto; color: $text-muted; margin-top: 1; }
    EditorControls #editor-choice-frame.editor-expanded { height: auto; }
    EditorControls #editor-choice.-expanded { height: auto; }
    EditorControls #editor-choice > SelectOverlay { overlay: none; }
    """

    class Feedback(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.store = EditorPreferencesStore(context.manager.store.path.with_name("editor.json"))
        self.preferences: EditorPreferences | PluginError = self.read_preference()

    def read_preference(self) -> EditorPreferences | PluginError:
        try:
            return self.store.read()
        except PluginError as exc:
            return exc

    def compose(self) -> ComposeResult:
        match self.preferences:
            case EditorPreferences(editor=editor):
                options, value, disabled = EDITOR_OPTIONS, editor, False
            case PluginError():
                options, value, disabled = UNAVAILABLE_OPTIONS, "unavailable", True
            case unreachable:
                assert_never(unreachable)
        with AppearanceField(id="editor-choice-frame"):
            yield Static("Editor:")
            yield EditorSelect(
                options, value=value, allow_blank=False, disabled=disabled, type_to_search=False,
                compact=True, id="editor-choice",
            )
        yield Static(EDITOR_HELP, id="editor-help", markup=False)

    def refresh_preference(self) -> None:
        previous = self.preferences
        self.preferences = self.read_preference()
        self.sync_selector()
        match self.preferences:
            case PluginError() as exc:
                self.report_read_error(exc)
            case EditorPreferences():
                if isinstance(previous, PluginError):
                    self.post_message(self.Feedback(
                        "Editor preferences restored. Changes apply on the next file open."
                    ))
            case unreachable:
                assert_never(unreachable)

    def refresh_if_idle(self) -> None:
        selector = self.query_one("#editor-choice", EditorSelect)
        if selector.expanded:
            return
        match self.preferences:
            case EditorPreferences(editor=editor):
                if selector.value != editor:
                    return
            case PluginError():
                pass
            case unreachable:
                assert_never(unreachable)
        self.refresh_preference()

    def sync_selector(self) -> None:
        selector = self.query_one("#editor-choice", EditorSelect)
        help_text = self.query_one("#editor-help", Static)
        with selector.prevent(Select.Changed):
            match self.preferences:
                case EditorPreferences(editor=editor):
                    if selector.disabled:
                        selector.set_options(EDITOR_OPTIONS)
                        selector.disabled = False
                    if selector.value != editor:
                        selector.value = editor
                    help_text.update(EDITOR_HELP)
                case PluginError():
                    selector.expanded = False
                    if not selector.disabled:
                        selector.disabled = True
                        selector.set_options(UNAVAILABLE_OPTIONS)
                    help_text.update("Cannot read editor preferences. Repair editor.json and return to General.")
                case unreachable:
                    assert_never(unreachable)

    def report_read_error(self, exc: PluginError) -> None:
        self.post_message(self.Feedback(
            f"Cannot read editor.json. Repair it; reopen General. Path: {self.store.path}\n{exc}"
        ))

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if event.widget.id == "editor-choice":
            self.query_one("#editor-choice-frame").scroll_visible(animate=False)

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        match event.value:
            case "environment":
                updated = EditorPreferences(editor="environment")
            case "nvim":
                updated = EditorPreferences(editor="nvim")
            case "vi":
                updated = EditorPreferences(editor="vi")
            case _:
                return
        if updated == self.preferences:
            return
        try:
            self.preferences = self.store.read()
        except PluginError as exc:
            self.preferences = exc
            self.sync_selector()
            self.report_read_error(exc)
            return
        try:
            if updated != self.preferences:
                self.store.write(updated)
        except PluginError as exc:
            self.sync_selector()
            self.post_message(self.Feedback(f"Editor not saved: {exc}"))
        else:
            self.preferences = updated
            self.sync_selector()
            self.post_message(self.Feedback("Editor saved. Applies on the next file open."))
