"""Validate the shared Textual view interface at its boundaries."""

from collections.abc import Sequence

from textual.widget import Widget

from .api import ViewPlugin
from .context import PluginContext
from .core.discovery import instantiate
from .core.errors import PluginError
from .core.models import PluginManifest
from .terminal_contract import TerminalViewPlugin


class LazyTextualView:
    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self.shortcuts = manifest.shortcuts or ""
        self._plugin: object | None = None

    def resolve(self) -> ViewPlugin:
        if self._plugin is None:
            self._plugin = instantiate(self.manifest)
        return textual_views([(self.manifest, self._plugin)])[0][1]

    def make_view(self, context: PluginContext) -> Widget:
        return self.resolve().make_view(context)

    def run_pane(self, context: PluginContext) -> None:
        if self._plugin is None:
            self._plugin = instantiate(self.manifest)
        if not isinstance(self._plugin, TerminalViewPlugin) or not callable(self._plugin.run_pane):
            raise PluginError(f"View {self.manifest.id} does not implement the terminal view plugin contract")
        self._plugin.run_pane(context)

    @property
    def fallback_shortcuts(self) -> str:
        plugin = self.resolve()
        return getattr(plugin, "fallback_shortcuts", plugin.shortcuts)

    @property
    def fallback_focus_hint(self) -> str | None:
        return getattr(self.resolve(), "fallback_focus_hint", None)


def textual_views(views: Sequence[tuple[PluginManifest, object]]) -> list[tuple[PluginManifest, ViewPlugin]]:
    result: list[tuple[PluginManifest, ViewPlugin]] = []
    for manifest, plugin in views:
        if (not isinstance(plugin, ViewPlugin) or not isinstance(plugin.shortcuts, str)
                or not callable(plugin.make_view) or not callable(plugin.run_pane)):
            raise PluginError(
                f"View {manifest.id} does not implement the Textual view plugin contract: "
                "provide string shortcuts and callable make_view and run_pane"
            )
        result.append((manifest, plugin))
    return result
