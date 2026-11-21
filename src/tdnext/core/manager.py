"""Transactional installation and enable/disable of local plugins."""

import os
import shutil
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import assert_never

from .discovery import instantiate
from .errors import InstallationConsentRequired, PluginError
from .graph import dependants, dependency_order
from .models import PluginManifest, PluginState
from .state import StateStore


class PluginManager:
    def __init__(self, manifests: Mapping[str, PluginManifest], store: StateStore, local_bin: Path) -> None:
        self.manifests = manifests
        self.store = store
        self.local_bin = local_bin

    def executable(self, name: str) -> str | None:
        local = self.local_bin / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
        return shutil.which(name)

    def check_dependencies(
        self, names: tuple[str, ...], *, fetch: bool = False, approved: tuple[tuple[str, str], ...] = (),
        existing: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        # Local import avoids a circular import (tdnext.api imports PluginManager).
        from ..api import IconProvider
        from .icons import check_provider

        hooks = {}
        actions = []
        for name in names:
            manifest = self.manifests[name]
            if existing is not None and name in existing:
                continue
            action = None
            if manifest.dependency_hook is not None:
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
            if manifest.role == "desktop.host":
                continue
            plugin = instantiate(manifest)
            instances[name] = plugin
            match manifest.role:
                case "desktop.host":
                    pass
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
        order = dependency_order(self.manifests, plugin_id)
        self.check_dependencies(order, fetch=True, approved=approved)
        state = self.store.read()
        self.store.write(PluginState(installed=state.installed | set(order), enabled=state.enabled))
        return order

    def enable(self, plugin_id: str, *, approved: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
        order = self.enable_order(plugin_id)
        self.check_dependencies(order, fetch=True, approved=approved)
        state = self.store.read()
        self.store.write(PluginState(installed=state.installed | set(order), enabled=state.enabled | set(order)))
        return order

    def enable_order(self, plugin_id: str) -> tuple[str, ...]:
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
                          and all(set(view.view_interfaces) <= set(host.host_interfaces) for view in views)]
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
            incompatible = [view.id for view in views if not set(view.view_interfaces) <= set(host.host_interfaces)]
            if incompatible:
                raise PluginError(f"Host {host.id} is incompatible with views: {', '.join(incompatible)}")
        return order

    def disable(self, plugin_id: str) -> frozenset[str]:
        state = self.store.read()
        if plugin_id not in self.manifests and plugin_id not in state.installed:
            raise PluginError(f"Plugin not found: {plugin_id}")
        affected = dependants(self.manifests, plugin_id, state.enabled)
        self.store.write(PluginState(installed=state.installed, enabled=state.enabled - affected))
        return affected

    def uninstall(self, plugin_id: str) -> frozenset[str]:
        """Remove target installed/enabled state; disable enabled dependants, keep them installed.

        Only ``plugin_id`` leaves ``installed``. Enabled transitive dependants
        (including the target when enabled) leave ``enabled`` but stay
        installed so reinstalling the target can re-enable them. Shared
        binaries, manifests, and user files are untouched; reinstall is
        ``install``/``enable`` without cleanup.
        """
        state = self.store.read()
        if plugin_id not in self.manifests and plugin_id not in state.installed:
            raise PluginError(f"Plugin not found: {plugin_id}")
        affected = dependants(self.manifests, plugin_id, state.enabled)
        self.store.write(
            PluginState(installed=state.installed - {plugin_id}, enabled=state.enabled - affected)
        )
        return affected
