from dataclasses import dataclass
from typing import Literal, assert_never

from tdnext.core.errors import PluginError
from tdnext.core.graph import dependants
from tdnext.core.manager import PluginManager

Operation = Literal["install", "enable", "disable", "uninstall"]


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation: Operation
    target: str
    affected: tuple[str, ...]


def apply_operation(manager: PluginManager, operation: Operation, target: str) -> OperationResult:
    protected = frozenset({"desktop", "settings"})
    if operation in ("disable", "uninstall"):
        affected = dependants(manager.manifests, target, manager.store.read().enabled)
        if target in protected or affected & protected:
            raise PluginError("The running Desktop and Settings cannot be disabled or uninstalled here.")
    match operation:
        case "install":
            result = manager.install(target)
        case "enable":
            result = manager.enable(target)
        case "disable":
            result = tuple(sorted(manager.disable(target)))
        case "uninstall":
            result = tuple(sorted(manager.uninstall(target)))
        case unreachable:
            assert_never(unreachable)
    return OperationResult(operation, target, result)
