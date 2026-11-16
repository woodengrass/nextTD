from dataclasses import dataclass
from typing import Literal, assert_never

from tdnext.core.errors import PluginError
from tdnext.core.graph import dependants
from tdnext.core.manager import PluginManager
from tdnext.core.session import SessionSnapshot

Operation = Literal["install", "enable", "disable", "uninstall"]


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation: Operation
    target: str
    affected: tuple[str, ...]


def protected_plugin_ids(manager: PluginManager, session: SessionSnapshot | None) -> frozenset[str]:
    protected = frozenset({"settings"}) | frozenset(
        name for name in manager.store.read().enabled
        if name in manager.manifests and manager.manifests[name].role == "desktop.host"
    )
    if session is not None and session.protected_plugins is not None:
        return protected | session.protected_plugins
    if session is not None:
        protected |= frozenset(
            name for name in manager.store.read().installed
            if name in manager.manifests and manager.manifests[name].role == "desktop.host"
        )
        while True:
            expanded = protected | frozenset(
                dependency for name in protected if name in manager.manifests
                for dependency in manager.manifests[name].requires_plugins
            )
            if expanded == protected:
                break
            protected = expanded
    return protected


def apply_operation(
    manager: PluginManager, operation: Operation, target: str, approved: tuple[tuple[str, str], ...] = (),
    session: SessionSnapshot | None = None,
) -> OperationResult:
    protected = protected_plugin_ids(manager, session)
    if operation in ("disable", "uninstall"):
        affected = dependants(manager.manifests, target, manager.store.read().enabled)
        if target in protected or affected & protected:
            raise PluginError("The running Desktop and Settings cannot be disabled or uninstalled here.")
    match operation:
        case "install":
            result = manager.install(target, approved=approved)
        case "enable":
            result = manager.enable(target, approved=approved)
        case "disable":
            result = tuple(sorted(manager.disable(target)))
        case "uninstall":
            result = tuple(sorted(manager.uninstall(target)))
        case unreachable:
            assert_never(unreachable)
    return OperationResult(operation, target, result)
