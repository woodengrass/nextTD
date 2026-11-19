"""Transactional installation and enable/disable of local plugins."""

import os
import shutil
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import assert_never

from .code_store import CodeStore
from .discovery import instantiate
from .errors import InstallationConsentRequired, PluginError
from .graph import dependants, dependency_order
from .models import PluginManifest, PluginState
from .plugin_imports import ImportRoutes, activate, activate_code, prune_dead_routes
from .removal import RemovalGuard, unavailable_guard
from .state import StateStore


class PluginManager:
    def __init__(
        self, manifests: Mapping[str, PluginManifest], store: StateStore, local_bin: Path,
        *, removal_guard: RemovalGuard = unavailable_guard,
    ) -> None:
        self.manifests: dict[str, PluginManifest] = dict(manifests)
        self.errors: dict[str, str] = dict(getattr(manifests, "errors", {}))
        self.sources = manifests
        self.store = store
        self.local_bin = local_bin
        self.code_store = CodeStore(store)
        self.removal_guard = removal_guard
        self.refresh_installed()

    def refresh_installed(self) -> None:
        for name in list(self.store.read().installed):
            if name not in self.manifests:
                continue
            receipt = self.code_store.read(name)
            if receipt is not None:
                current = self.manifests[name]
                source_dir = current.source_dir
                if source_dir is None and name in self.sources:
                    source_dir = self.sources[name].source_dir
                self.manifests[name] = receipt.model_copy(update={"source_dir": source_dir})

    def executable(self, name: str) -> str | None:
        local = self.local_bin / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
        return shutil.which(name)

    def check_dependencies(
        self, names: tuple[str, ...], *, fetch: bool = False, approved: tuple[tuple[str, str], ...] = (),
        existing: Mapping[str, object] | None = None, lazy_views: bool = False,
    ) -> dict[str, object]:
        # Local import avoids a circular import (tdnext.api imports PluginManager).
        from ..api import DesktopHost, IconProvider
        from .icons import check_provider

        activate(ImportRoutes(packages={
            manifest.code.package: manifest.code for manifest in self.manifests.values() if manifest.code is not None
        }))
        prune_dead_routes({
            manifest.code.package for manifest in self.manifests.values() if manifest.code is not None
        })
        hooks = {}
        actions = []
        for name in names:
            manifest = self.manifests[name]
            if existing is not None and name in existing:
                continue
            action = None
            if manifest.dependency_hook is not None:
                if manifest.code is not None:
                    activate_code(manifest.code)
                elif manifest.source_dir is not None:
                    raise PluginError(f"Plugin {name} has no managed code; run tdnext install {name}")
                module, _, symbol = manifest.dependency_hook.partition(":")
                try:
                    hook = getattr(import_module(module), symbol)
                    action = hook.check(self)
                except (ImportError, AttributeError) as exc:
                    raise PluginError(f"Cannot load dependency hook for {name}: {exc}") from exc
                hooks[name] = hook
                if action is not None:
                    actions.append((name, action))
            if action is None:
                for executable in manifest.requires_executables:
                    if self.executable(executable) is None:
                        raise PluginError(f"{name} requires missing executable '{executable}'; install it on PATH")
        if actions:
            # 使用者確認具體安裝項目後才執行。See ../plugin-compatibility-and-consent.adr.md
            if not fetch:
                raise PluginError("Missing dependencies: " + "; ".join(text for _, text in actions))
            if tuple(actions) != approved:
                raise InstallationConsentRequired(tuple(actions))
            for name, _ in actions:
                hooks[name].install(self)
                if hooks[name].check(self) is not None:
                    raise PluginError(f"Dependency installation for {name} did not resolve its requirements")
        instances: dict[str, object] = {}
        for name in names:
            manifest = self.manifests[name]
            if existing is not None and name in existing:
                instances[name] = existing[name]
                continue
            for executable in manifest.requires_executables:
                binary = self.executable(executable)
                if binary is None:
                    raise PluginError(f"{name} requires missing executable '{executable}'; install it on PATH")
            if lazy_views and manifest.role == "desktop.view" and manifest.shortcuts is not None:
                from ..textual_contract import LazyTextualView

                instances[name] = LazyTextualView(manifest)
                continue
            plugin = instantiate(manifest)
            instances[name] = plugin
            match manifest.role:
                case "desktop.host":
                    if not isinstance(plugin, DesktopHost) or not callable(plugin.run):
                        raise PluginError(f"{name} does not implement the desktop host contract")
                case "desktop.view":
                    pass
                case "icons.provider":
                    if not isinstance(plugin, IconProvider):
                        raise PluginError(f"{name} does not implement the icon provider contract")
                    check_provider(plugin, name)
                case unreachable:
                    assert_never(unreachable)
        return instances

    def install(self, plugin_id: str, *, approved: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
        from .lifecycle import install

        return install(self, plugin_id, approved, enable=False)

    def enable(self, plugin_id: str, *, approved: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
        from .lifecycle import install

        return install(self, plugin_id, approved, enable=True)

    def enable_order(self, plugin_id: str) -> tuple[str, ...]:
        from ..terminal_contract import compatible_views

        order = dependency_order(self.manifests, plugin_id)
        state = self.store.read()
        enabled_hosts = [self.manifests[name] for name in state.enabled
                         if name in self.manifests and self.manifests[name].role == "desktop.host"]
        requested_hosts = [self.manifests[name] for name in order if self.manifests[name].role == "desktop.host"]
        hosts = {host.id: host for host in (*enabled_hosts, *requested_hosts)}
        if len(hosts) > 1:
            raise PluginError("Exactly one desktop host may be enabled; disable the current host first")
        views = [self.manifests[name] for name in (*state.enabled, *order)
                 if name in self.manifests and self.manifests[name].role == "desktop.view"]
        if views and not hosts:
            candidates = [host for host in self.manifests.values() if host.role == "desktop.host"
                          and all(compatible_views(view.view_interfaces, host.host_interfaces) for view in views)]
            if len(candidates) != 1:
                names = ", ".join(host.id for host in candidates) or "none"
                raise PluginError(f"Select a compatible host explicitly; candidates: {names}")
            host_order = dependency_order(self.manifests, candidates[0].id)
            order = tuple(dict.fromkeys((*host_order, *order)))
            hosts.update({name: self.manifests[name] for name in host_order
                          if self.manifests[name].role == "desktop.host"})
            if len(hosts) != 1:
                raise PluginError("Selected host dependencies require multiple hosts; fix the plugin dependencies")
            views.extend(self.manifests[name] for name in host_order
                         if self.manifests[name].role == "desktop.view" and self.manifests[name].view_interfaces)
        if hosts:
            host = next(iter(hosts.values()))
            incompatible = [
                view.id for view in views if not compatible_views(view.view_interfaces, host.host_interfaces)
            ]
            if incompatible:
                raise PluginError(f"Host {host.id} is incompatible with views: {', '.join(incompatible)}")
        return order

    def disable(self, plugin_id: str) -> frozenset[str]:
        with self.code_store.locked():
            self.refresh_installed()
            state = self.store.read()
            if plugin_id not in self.manifests and plugin_id not in state.installed:
                raise PluginError(f"Plugin not found: {plugin_id}")
            affected = dependants(self.manifests, plugin_id, state.enabled)
            self.store.write(PluginState(installed=state.installed, enabled=state.enabled - affected))
            return affected

    def uninstall(self, plugin_id: str) -> frozenset[str]:
        from .lifecycle import uninstall

        return uninstall(self, plugin_id)
