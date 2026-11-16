"""Launch the configured desktop host with the enabled views."""

import os
from pathlib import Path

from .api import DesktopHost, PluginContext
from .core.appearance import AppearanceStore
from .core.discovery import instantiate
from .core.errors import PluginError
from .core.graph import dependency_order
from .core.manager import PluginManager
from .core.models import PluginManifest
from .core.presentation import snapshot_views
from .core.session import SESSION_ENV, SessionSnapshot
from .runtime import manager


def enabled_views(
    plugins: PluginManager, session: SessionSnapshot | None = None, *, instances: dict[str, object] | None = None
) -> list[tuple[PluginManifest, object]]:
    if session is not None:
        views: list[tuple[PluginManifest, object]] = []
        for view in session.views:
            plugin = instantiate(view.manifest)
            views.append((view.manifest, plugin))
        return views
    state = plugins.store.read()
    views = []
    selected_provider = AppearanceStore(plugins.store.path.with_name("appearance.json")).read().provider_id
    hosts = [plugins.manifests[name] for name in state.enabled
             if name in plugins.manifests and plugins.manifests[name].role == "desktop.host"]
    for plugin_id in sorted(state.enabled):
        manifest = plugins.manifests.get(plugin_id)
        if manifest is not None and manifest.role == "desktop.view":
            if len(hosts) != 1 or not set(manifest.view_interfaces) <= set(hosts[0].host_interfaces):
                raise PluginError(f"Enabled view {plugin_id} is incompatible with the enabled host")
    checked = instances if instances is not None else {}
    for plugin_id in sorted(state.enabled):
        manifest = plugins.manifests.get(plugin_id)
        if manifest is None:
            errors = getattr(plugins.manifests, "errors", {})
            if plugin_id in errors:
                raise PluginError(errors[plugin_id])
            if plugin_id == selected_provider or plugin_id == "icons":
                continue
            raise PluginError(f"Required plugin not found: {plugin_id}")
        if manifest.role == "icons.provider":
            continue
        order = dependency_order(plugins.manifests, plugin_id)
        if not set(order) <= state.enabled:
            raise PluginError(f"Enabled plugin {plugin_id} has a disabled dependency")
        unchecked = tuple(name for name in order if name not in checked)
        checked.update(plugins.check_dependencies(unchecked))
        if manifest.role != "desktop.view":
            continue
        plugin = checked[plugin_id]
        views.append((manifest, plugin))
    return views


def launch(directory: Path) -> None:
    if not directory.is_dir():
        raise PluginError(f"Not a directory: {directory}")
    os.environ.pop(SESSION_ENV, None)
    plugins = manager()
    # 啟動只使用已啟用的插件；允許空桌面。See explicit-plugin-startup.adr.md
    state = plugins.store.read()
    errors = getattr(plugins.manifests, "errors", {})
    for plugin_id in sorted(state.enabled & errors.keys()):
        raise PluginError(errors[plugin_id])
    hosts = [plugins.manifests[plugin_id] for plugin_id in sorted(state.enabled)
             if plugin_id in plugins.manifests and plugins.manifests[plugin_id].role == "desktop.host"]
    if not hosts:
        raise PluginError("Desktop is disabled. Run: tdnext enable desktop")
    if len(hosts) != 1:
        raise PluginError("Exactly one desktop host must be enabled")
    instances: dict[str, object] = {}
    views = enabled_views(plugins, instances=instances)
    snapshot = snapshot_views(plugins, views, instances)
    protected = set(dependency_order(plugins.manifests, hosts[0].id))
    if any(manifest.id == "settings" for manifest, _ in views):
        protected.update(dependency_order(plugins.manifests, "settings"))
    session = SessionSnapshot(views=snapshot.views, protected_plugins=frozenset(protected))
    context = PluginContext(directory, plugins, session)
    desktop = instances[hosts[0].id]
    if not isinstance(desktop, DesktopHost):
        raise PluginError("desktop does not implement the desktop host contract")
    desktop.run(context, views)
