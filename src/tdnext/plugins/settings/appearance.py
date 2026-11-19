"""Keyboard- and mouse-operable appearance selectors for Settings."""

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.events import DescendantFocus, Resize
from textual.message import Message
from textual.widgets import Select, Static

from tdnext.api import PluginContext
from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.errors import PluginError
from tdnext.settings_ui import AppearanceField


class AppearanceControls(Horizontal):
    DEFAULT_CSS = """
    AppearanceControls { height: 3; }
    AppearanceControls.stacked { layout: vertical; height: 6; }
    AppearanceControls #icon-mode-frame { width: 26; }
    AppearanceControls.stacked #icon-mode-frame { width: 1fr; }
    """

    class Feedback(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context
        self.store = AppearanceStore(context.manager.store.path.with_name("appearance.json"))
        self.appearance = self.store.read()
        self._options: list[tuple[str, str]] = []

    def provider_options(self) -> list[tuple[str, str]]:
        manager = self.context.manager
        enabled = manager.store.read().enabled
        options = [("None (text)", "")]
        options.extend(
            (manifest.name, manifest.id)
            for manifest in sorted(manager.manifests.values(), key=lambda item: item.name)
            if manifest.role == "icons.provider" and manifest.id in enabled
        )
        selected = self.appearance.provider_id
        if selected and all(value != selected for _, value in options):
            options.append((f"{selected} (unavailable)", selected))
        return options

    def compose(self) -> ComposeResult:
        self._options = self.provider_options()
        with AppearanceField(id="icon-provider-frame"):
            yield Static("Provider:")
            yield Select(
                self._options, value=self.appearance.provider_id or "", allow_blank=False,
                type_to_search=False, compact=True, id="icon-provider",
            )
        with AppearanceField(id="icon-mode-frame"):
            yield Static("Style:")
            yield Select(
                [("Compatibility", "compatibility"), ("Codicons", "codicons")],
                value=self.appearance.icon_mode, allow_blank=False, type_to_search=False, compact=True, id="icon-mode",
            )

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 50, "stacked")

    def refresh_providers(self) -> None:
        self.appearance = self.store.read()
        self.sync_selectors()

    def sync_selectors(self) -> None:
        selector = self.query_one("#icon-provider", Select)
        options = self.provider_options()
        with selector.prevent(Select.Changed):
            if options != self._options:
                selector.set_options(options)
                self._options = options
            if selector.value != (self.appearance.provider_id or ""):
                selector.value = self.appearance.provider_id or ""
        mode = self.query_one("#icon-mode", Select)
        with mode.prevent(Select.Changed):
            if mode.value != self.appearance.icon_mode:
                mode.value = self.appearance.icon_mode

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        if not isinstance(event.widget, Select) or event.widget.expanded:
            return
        current = (
            (self.appearance.provider_id or "") if event.widget.id == "icon-provider" else self.appearance.icon_mode
        )
        if event.widget.value == current:
            self.refresh_providers()

    def on_select_changed(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str):
            return
        event.stop()
        if (event.select.id == "icon-provider" and event.value == (self.appearance.provider_id or "")) or (
            event.select.id == "icon-mode" and event.value == self.appearance.icon_mode
        ):
            return
        try:
            self.appearance = self.store.read()
            if event.select.id == "icon-provider":
                updated = Appearance(provider_id=event.value or None, icon_mode=self.appearance.icon_mode)
            elif event.select.id == "icon-mode" and event.value in ("compatibility", "codicons"):
                updated = Appearance(provider_id=self.appearance.provider_id, icon_mode=event.value)
            else:
                return
            if updated == self.appearance:
                self.sync_selectors()
                return
            self.store.write(updated)
        except PluginError as exc:
            self.sync_selectors()
            self.post_message(self.Feedback(f"Appearance not saved: {exc}"))
        else:
            self.appearance = updated
            self.sync_selectors()
            self.post_message(self.Feedback("Icon appearance saved. Restart desktop to apply."))
