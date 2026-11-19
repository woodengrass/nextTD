"""Resolve optional icon services into an immutable desktop-session snapshot."""

from collections.abc import Sequence

from tdnext.api import IconProvider

from .appearance import AppearanceStore
from .errors import PluginError
from .graph import dependency_order
from .icons import safe_icon
from .manager import PluginManager
from .models import PluginManifest
from .session import SessionSnapshot, SessionView


def snapshot_views(
    manager: PluginManager, views: Sequence[tuple[PluginManifest, object]], instances: dict[str, object] | None = None
) -> SessionSnapshot:
    appearance = AppearanceStore(manager.store.path.with_name("appearance.json")).read()
    state = manager.store.read()
    provider: IconProvider | None = None
    provider_id = appearance.provider_id
    if provider_id in state.enabled and provider_id in manager.manifests:
        manifest = manager.manifests[provider_id]
        if manifest.role == "icons.provider":
            try:
                order = dependency_order(manager.manifests, provider_id)
                if not set(order) <= state.enabled:
                    raise PluginError(f"Enabled plugin {provider_id} has a disabled dependency")
                candidate = manager.check_dependencies(order, existing=instances)[provider_id]
                if isinstance(candidate, IconProvider) and callable(candidate.resolve):
                    provider = candidate
            except PluginError:
                provider = None

    snapshot: list[SessionView] = []
    for manifest, _plugin in views:
        icon = manifest.icon or "[]"
        if provider is not None and manifest.icon_key is not None:
            try:
                resolved = provider.resolve(manifest.icon_key, appearance.icon_mode)
                if safe_icon(resolved, appearance.icon_mode):
                    icon = resolved
            except Exception:  # noqa: BLE001
                # A trusted extension can fail on an individual key; navigation must survive it.
                icon = manifest.icon or "[]"
        snapshot.append(SessionView(manifest=manifest, icon=icon))
    hosts = [
        manager.manifests[name] for name in sorted(state.enabled)
        if name in manager.manifests and manager.manifests[name].role == "desktop.host"
    ]
    protected = frozenset(dependency_order(manager.manifests, hosts[0].id)) if len(hosts) == 1 else frozenset()
    return SessionSnapshot(
        views=tuple(snapshot),
        protected_plugins=protected,
        view_dependencies={
            manifest.id: frozenset(dependency_order(manager.manifests, manifest.id)) for manifest, _ in views
        },
        participants=tuple(manager.manifests[name] for name in sorted(state.enabled) if name in manager.manifests),
    )
