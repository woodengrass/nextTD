"""Validate the Textual desktop's view interface at its boundaries."""

from collections.abc import Sequence

from tdnext.api import ViewPlugin
from tdnext.core.errors import PluginError
from tdnext.core.models import PluginManifest


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
