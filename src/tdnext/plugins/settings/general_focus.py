"""Preserve the established feedback Tab stop while extending General."""

from typing import Literal

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import DescendantFocus
from textual.widgets import ContentSwitcher, Select

from tdnext.core.errors import PluginError

from .appearance import AppearanceControls
from .editor import EditorControls


class GeneralFocus(Vertical):
    BINDINGS = [
        Binding("tab", "general_next", show=False),
        Binding("shift+tab", "general_previous", show=False),
    ]

    def refresh_general(self) -> None:
        self.query_one(AppearanceControls).refresh_providers()
        self.query_one(EditorControls).refresh_preference()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if not isinstance(event.widget, Select) or event.widget.expanded:
            return
        try:
            self.query_one(EditorControls).refresh_if_idle()
            if event.widget.id == "editor-choice":
                self.query_one(AppearanceControls).refresh_providers()
        except PluginError as exc:
            self.notify(f"Cannot refresh settings: {exc}", severity="error")

    def move_general_focus(self, direction: Literal[-1, 1]) -> bool:
        if self.query_one("#settings-pages", ContentSwitcher).current != "settings-general":
            return False
        focused = self.app.focused
        if focused is None or any(
            isinstance(widget, Select) and widget.expanded for widget in (focused, *focused.ancestors)
        ):
            return False
        chain = self.screen.focus_chain
        mode = self.query_one("#icon-mode", Select)
        feedback = self.query_one("#settings-feedback-scroll", VerticalScroll)
        if focused not in chain or mode not in chain or feedback not in chain:
            return False
        chain.remove(feedback)
        chain.insert(chain.index(mode) + 1, feedback)
        self.screen.set_focus(chain[(chain.index(focused) + direction) % len(chain)])
        return True

    def focus_previous_from_outside(self) -> bool:
        focused = self.app.focused
        if focused is None or self in focused.ancestors:
            return False
        chain = self.screen.focus_chain
        feedback = self.query_one("#settings-feedback-scroll", VerticalScroll)
        if focused in chain and feedback in chain and chain[chain.index(focused) - 1] is feedback:
            return self.move_general_focus(-1)
        return False

    def action_general_next(self) -> None:
        if not self.move_general_focus(1):
            self.screen.focus_next()

    def action_general_previous(self) -> None:
        if not self.move_general_focus(-1):
            self.screen.focus_previous()
