import shutil
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from .errors import PluginError
from .graph import dependants, dependency_order
from .models import PluginManifest, PluginState
from .plugin_imports import ImportRoutes, activate, restore_routes, snapshot_routes
from .removal import RemovalRequest

if TYPE_CHECKING:
    from .manager import PluginManager


def _manageable(manifest: PluginManifest) -> bool:
    """Real discovered manifests take the managed path; synthetic doubles stay state-only.

    Any manifest with a source_dir came from discovery and must install owned
    managed code (owned_source raises a useful error when the layout is not
    owned). In-memory doubles carry source_dir=None and keep the state-only
    path.
    """
    return manifest.source_dir is not None


def install(
    manager: "PluginManager", plugin_id: str, approved: tuple[tuple[str, str], ...], *, enable: bool,
) -> tuple[str, ...]:
    codes = manager.code_store
    with codes.locked():
        manager.refresh_installed()
        order = manager.enable_order(plugin_id) if enable else dependency_order(manager.manifests, plugin_id)
        state = manager.store.read()
        for name in order:
            if codes.read(name) is not None and name not in state.installed:
                raise PluginError(f"Uncommitted code at {codes.slot(name)}; move it aside before reinstalling")
        manageable = [name for name in order if codes.read(name) is None and _manageable(manager.manifests[name])]
        if not manageable:
            manager.check_dependencies(order, fetch=True, approved=approved)
            manager.store.write(PluginState(
                installed=state.installed | set(order), enabled=state.enabled | set(order) if enable else state.enabled,
            ))
            return order
        # Managed-copy-first: every real plugin is staged before validation so
        # consent and installers always observe the managed copies.
        codes.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".install-", dir=codes.root) as temporary, ExitStack() as rollback:
            pending = dict(manager.manifests)
            staged = []
            for name in manageable:
                directory = Path(temporary) / name
                directory.mkdir()
                pending[name] = codes.stage(manager.manifests[name], directory)
                staged.append(name)
            originals = manager.manifests
            routes = snapshot_routes()
            rollback.callback(restore_routes, routes)
            manager.manifests = pending
            try:
                manager.check_dependencies(order, fetch=True, approved=approved)
            finally:
                manager.manifests = originals
            restore_routes(routes)
            packages: dict[str, str] = {}
            for name, manifest in pending.items():
                if manifest.code is None:
                    continue
                package = manifest.code.package
                for other, owner in packages.items():
                    if package == other or package.startswith(other + ".") or other.startswith(package + "."):
                        raise PluginError(f"Plugins {name} and {owner} claim overlapping Python packages: {package}")
                packages[package] = name
            for name in staged:
                pending[name] = codes.publish(pending[name])
                rollback.callback(shutil.rmtree, codes.slot(name))
            activate(ImportRoutes(packages={
                code.package: code for manifest in pending.values() if (code := manifest.code) is not None
            }))
            manager.store.write(PluginState(
                installed=state.installed | set(order), enabled=state.enabled | set(order) if enable else state.enabled,
            ))
            rollback.pop_all()
            manager.manifests = pending
        return order


def removal_request(manager: "PluginManager", plugin_id: str) -> RemovalRequest:
    state = manager.store.read()
    if plugin_id not in manager.manifests and plugin_id not in state.installed:
        raise PluginError(f"Plugin not found: {plugin_id}")
    return RemovalRequest(plugin_id, dependants(manager.manifests, plugin_id, state.enabled))


@dataclass(frozen=True, slots=True)
class PreparedRemoval:
    state: PluginState
    receipt: PluginManifest | None


def prepare_removal(manager: "PluginManager", plugin_id: str) -> tuple[RemovalRequest, PreparedRemoval]:
    """Validate that the removal can commit; raises before any session is touched."""
    manager.refresh_installed()
    request = removal_request(manager, plugin_id)
    state = manager.store.read()
    receipt = manager.code_store.read(plugin_id)
    return request, PreparedRemoval(state, receipt)


def commit_removal(
    manager: "PluginManager", request: RemovalRequest, target: PreparedRemoval,
) -> frozenset[str]:
    """Remove code and installed state. The caller holds the code-store lock."""
    codes = manager.code_store
    state, receipt = target.state, target.receipt
    if receipt is None:
        manager.store.write(PluginState(
            installed=state.installed - {request.plugin_id}, enabled=state.enabled - request.affected,
        ))
        return request.affected
    slot = codes.slot(request.plugin_id)
    with TemporaryDirectory(prefix=".remove-", dir=codes.root) as temporary:
        backup = Path(temporary) / "backup"
        shutil.copytree(slot, backup)
        committed = False
        try:
            shutil.rmtree(slot)
            manager.store.write(PluginState(
                installed=state.installed - {request.plugin_id}, enabled=state.enabled - request.affected,
            ))
            committed = True
        finally:
            if not committed:
                if slot.exists():
                    slot.rename(Path(temporary) / "partial")
                backup.rename(slot)
    if receipt.code is not None:
        activate(ImportRoutes(packages={receipt.code.package: None}))
    if request.plugin_id in manager.sources:
        manager.manifests[request.plugin_id] = manager.sources[request.plugin_id]
    else:
        manager.manifests.pop(request.plugin_id, None)
    manager.refresh_installed()
    return request.affected


def uninstall(manager: "PluginManager", plugin_id: str) -> frozenset[str]:
    codes = manager.code_store
    with codes.locked():
        request, target = prepare_removal(manager, plugin_id)
        with manager.removal_guard(request) as blockers:
            if blockers:
                raise PluginError("; ".join(blockers))
            return commit_removal(manager, request, target)
