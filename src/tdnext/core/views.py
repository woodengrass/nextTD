"""Resolve enabled view plugins for content panes."""

from .appearance import AppearanceStore
from .discovery import instantiate
from .errors import PluginError
from .graph import dependency_order
from .manager import PluginManager
from .models import PluginManifest
from .session import SessionSnapshot


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
