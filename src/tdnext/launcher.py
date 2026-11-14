"""Launch the configured desktop host with the enabled views."""

import os
from pathlib import Path

from .api import DesktopHost, PluginContext, ViewPlugin
from .core.appearance import AppearanceStore
from .core.discovery import load_entrypoint
from .core.errors import PluginError
from .core.graph import dependency_order
from .core.manager import PluginManager
from .core.models import PluginManifest
from .core.presentation import snapshot_views
from .core.session import SESSION_ENV, SessionSnapshot
from .runtime import manager


def enabled_views(
    plugins: PluginManager, session: SessionSnapshot | None = None
) -> list[tuple[PluginManifest, ViewPlugin]]:
    if session is not None:
        views: list[tuple[PluginManifest, ViewPlugin]] = []
        for view in session.views:
            plugin = load_entrypoint(view.manifest)()
            if not isinstance(plugin, ViewPlugin):
                raise PluginError(f"{view.manifest.id} does not implement the view plugin contract")
            views.append((view.manifest, plugin))
        return views
    state = plugins.store.read()
    views = []
    selected_provider = AppearanceStore(plugins.store.path.with_name("appearance.json")).read().provider_id
    for plugin_id in sorted(state.enabled):
        manifest = plugins.manifests.get(plugin_id)
        if manifest is None:
            if plugin_id == selected_provider or plugin_id == "icons":
                continue
            raise PluginError(f"Required plugin not found: {plugin_id}")
        if manifest.role == "icons.provider":
            continue
        order = dependency_order(plugins.manifests, plugin_id)
        if not set(order) <= state.enabled:
            raise PluginError(f"Enabled plugin {plugin_id} has a disabled dependency")
        plugins.check_dependencies(order)
        if manifest.role != "desktop.view":
            continue
        plugin = load_entrypoint(manifest)()
        if not isinstance(plugin, ViewPlugin):
            raise PluginError(f"{plugin_id} does not implement the view plugin contract")
        views.append((manifest, plugin))
    return views


def launch(directory: Path) -> None:
    if not directory.is_dir():
        raise PluginError(f"Not a directory: {directory}")
    os.environ.pop(SESSION_ENV, None)
    plugins = manager()
    if not plugins.store.path.exists():
        plugins.enable("files")
        plugins.enable("settings")
    state = plugins.store.read()
    hosts = [plugins.manifests[plugin_id] for plugin_id in sorted(state.enabled)
             if plugin_id in plugins.manifests and plugins.manifests[plugin_id].role == "desktop.host"]
    if not hosts:
        raise PluginError("Desktop is disabled. Run: tdnext enable desktop")
    if len(hosts) != 1:
        raise PluginError("Exactly one desktop host must be enabled")
    views = enabled_views(plugins)
    if not views:
        raise PluginError("No enabled views. Run: tdnext enable files")
    context = PluginContext(directory, plugins, snapshot_views(plugins, views))
    desktop = load_entrypoint(hosts[0])()
    if not isinstance(desktop, DesktopHost):
        raise PluginError("desktop does not implement the desktop host contract")
    desktop.run(context, views)
