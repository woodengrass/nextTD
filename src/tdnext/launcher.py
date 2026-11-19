"""Launch the configured desktop host with the enabled views."""

import os
from pathlib import Path

from .api import DesktopHost, PluginContext
from .core.appearance import AppearanceStore
from .core.code_store import CodeStore
from .core.discovery import instantiate
from .core.errors import PluginError
from .core.graph import dependency_order
from .core.manager import PluginManager
from .core.models import PluginManifest
from .core.presentation import snapshot_views
from .core.process_name import set_process_name
from .core.session import SESSION_ENV, SessionSnapshot
from .core.state import StateStore
from .runtime import manager, state_dir
from .terminal_contract import compatible_views


def enabled_views(
    plugins: PluginManager, session: SessionSnapshot | None = None, *, instances: dict[str, object] | None = None,
    lazy_views: bool = False,
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
            if len(hosts) != 1 or not compatible_views(manifest.view_interfaces, hosts[0].host_interfaces):
                raise PluginError(f"Enabled view {plugin_id} is incompatible with the enabled host")
    checked = instances if instances is not None else {}
    for plugin_id in sorted(state.enabled):
        manifest = plugins.manifests.get(plugin_id)
        if manifest is None:
            errors = plugins.errors
            if plugin_id in errors:
                raise PluginError(errors[plugin_id])
            if plugin_id == selected_provider:
                continue
            raise PluginError(
                f"Required plugin not found: {plugin_id}; restore its manifest or run "
                f"tdnext disable {plugin_id} or tdnext uninstall {plugin_id}"
            )
        if manifest.role == "icons.provider":
            continue
        order = dependency_order(plugins.manifests, plugin_id)
        if not set(order) <= state.enabled:
            raise PluginError(f"Enabled plugin {plugin_id} has a disabled dependency")
        unchecked = tuple(name for name in order if name not in checked)
        checked.update(plugins.check_dependencies(unchecked, lazy_views=lazy_views))
        if manifest.role != "desktop.view":
            continue
        plugin = checked[plugin_id]
        views.append((manifest, plugin))
    return views


def launch(directory: Path) -> None:
    if not directory.is_dir():
        raise PluginError(f"Not a directory: {directory}")
    # Fallback runs in this process; name it tdnext (including `python -m`)
    # so uninstall-by-name closes it. The native server binary is already
    # a real file named tdnext.
    set_process_name()
    state = state_dir()
    codes = CodeStore(StateStore(state / "plugins.json"))
    with codes.locked():
        desktop, context, views = _prepare_launch(directory)
    desktop.run(context, views)


def _prepare_launch(directory: Path) -> tuple[DesktopHost, PluginContext, list[tuple[PluginManifest, object]]]:
    os.environ.pop(SESSION_ENV, None)
    plugins = manager()
    # 啟動只使用已啟用的插件；允許空桌面。See explicit-plugin-startup.adr.md
    state = plugins.store.read()
    errors = plugins.errors
    for plugin_id in sorted(state.enabled & errors.keys()):
        raise PluginError(errors[plugin_id])
    hosts = [plugins.manifests[plugin_id] for plugin_id in sorted(state.enabled)
             if plugin_id in plugins.manifests and plugins.manifests[plugin_id].role == "desktop.host"]
    if not hosts:
        raise PluginError("Desktop is disabled. Run: tdnext enable desktop")
    if len(hosts) != 1:
        raise PluginError("Exactly one desktop host must be enabled")
    instances: dict[str, object] = {}
    views = enabled_views(
        plugins, instances=instances,
        lazy_views=bool({"tdnext.textual-view.v1", "tdnext.terminal-view.v1"} & set(hosts[0].host_interfaces)),
    )
    session = snapshot_views(plugins, views, instances)
    context = PluginContext(directory, plugins, session)
    desktop = instances[hosts[0].id]
    if not isinstance(desktop, DesktopHost):
        raise PluginError("desktop does not implement the desktop host contract")
    return desktop, context, views
