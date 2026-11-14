"""Transactional installation and enable/disable of local plugins."""

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import assert_never

from .discovery import instantiate
from .errors import PluginError
from .executables import install_executable
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

    def check_dependencies(self, names: tuple[str, ...], *, fetch: bool = False) -> None:
        # Local import avoids a circular import (tdnext.api imports PluginManager).
        from ..api import DesktopHost, IconProvider, ViewPlugin
        from .icons import check_provider

        for name in names:
            manifest = self.manifests[name]
            for executable in manifest.requires_executables:
                binary = self.executable(executable)
                if binary is None:
                    if not fetch:
                        raise PluginError(f"{name} requires missing executable '{executable}'")
                    binary = str(install_executable(executable, self.local_bin))
                if executable == "btop":
                    try:
                        result = subprocess.run(
                            [binary, "--version"],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=10,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise PluginError(f"Cannot run btop at {binary}: {exc}") from exc
                    if result.returncode or "btop" not in result.stdout.lower():
                        raise PluginError(f"Executable at {binary} is not a working btop")
            plugin = instantiate(manifest)
            match manifest.role:
                case "desktop.host":
                    if not isinstance(plugin, DesktopHost) or not callable(plugin.run):
                        raise PluginError(f"{name} does not implement the desktop host contract")
                case "desktop.view":
                    if not isinstance(plugin, ViewPlugin):
                        raise PluginError(f"{name} does not implement the view plugin contract")
                case "icons.provider":
                    if not isinstance(plugin, IconProvider):
                        raise PluginError(f"{name} does not implement the icon provider contract")
                    check_provider(plugin, name)
                case unreachable:
                    assert_never(unreachable)

    def install(self, plugin_id: str) -> tuple[str, ...]:
        order = dependency_order(self.manifests, plugin_id)
        self.check_dependencies(order, fetch=True)
        state = self.store.read()
        self.store.write(PluginState(installed=state.installed | set(order), enabled=state.enabled))
        return order

    def enable(self, plugin_id: str) -> tuple[str, ...]:
        order = dependency_order(self.manifests, plugin_id)
        self.check_dependencies(order, fetch=True)
        state = self.store.read()
        self.store.write(PluginState(installed=state.installed | set(order), enabled=state.enabled | set(order)))
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
