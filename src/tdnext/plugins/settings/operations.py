from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, assert_never

from tdnext.core.errors import PluginError
from tdnext.core.graph import dependants
from tdnext.core.manager import PluginManager
from tdnext.core.session import SessionSnapshot
from tdnext.core.uninstall_helper import launch_uninstall_helper

Operation = Literal["install", "enable", "disable", "uninstall"]


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation: Operation
    target: str
    affected: tuple[str, ...]


def protected_plugin_ids(
    manager: PluginManager, session: SessionSnapshot | None, view_id: str | None
) -> frozenset[str]:
    state = manager.store.read()
    hosts = state.enabled
    protected = frozenset(
        name for name in hosts if name in manager.manifests and manager.manifests[name].role == "desktop.host"
    )
    if view_id is not None:
        protected |= frozenset({view_id})
    else:
        protected |= state.installed
    snapshot_manifests = ()
    if session is not None:
        protected |= session.protected_plugins
        if view_id is not None:
            protected |= session.view_dependencies.get(view_id, frozenset())
        snapshot_manifests = (*session.participants, *(view.manifest for view in session.views))
    while True:
        expanded = protected | frozenset(
            dependency for name in protected if name in manager.manifests
            for dependency in manager.manifests[name].requires_plugins
        ) | frozenset(
            dependency for manifest in snapshot_manifests if manifest.id in protected
            for dependency in manifest.requires_plugins
        )
        if expanded == protected:
            return protected
        protected = expanded


def _uninstall_via_helper(manager: PluginManager, target: str) -> OperationResult:
    """Close TDNext sessions in a detached helper, then uninstall.

    Production runner. Tests inject a stub through ``uninstall_runner``:
    spawning the real helper in tests would act on live processes.
    """
    affected = dependants(manager.manifests, target, manager.store.read().enabled)
    launch_uninstall_helper(manager.store.path.parent, target)
    return OperationResult("uninstall", target, tuple(sorted(affected)))


def apply_operation(
    manager: PluginManager, operation: Operation, target: str, approved: tuple[tuple[str, str], ...] = (),
    session: SessionSnapshot | None = None, *, view_id: str | None,
    uninstall_runner: Callable[[PluginManager, str], OperationResult] | None = None,
) -> OperationResult:
    protected = protected_plugin_ids(manager, session, view_id)
    if operation == "disable":
        affected = dependants(manager.manifests, target, manager.store.read().enabled)
        if target in protected or affected & protected:
            raise PluginError("The running Desktop and Settings cannot be disabled here.")
    match operation:
        case "install":
            result = manager.install(target, approved=approved)
        case "enable":
            result = manager.enable(target, approved=approved)
        case "disable":
            result = tuple(sorted(manager.disable(target)))
        case "uninstall":
            return (uninstall_runner or _uninstall_via_helper)(manager, target)
        case unreachable:
            assert_never(unreachable)
    return OperationResult(operation, target, result)
