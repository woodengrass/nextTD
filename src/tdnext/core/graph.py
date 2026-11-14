"""Dependency ordering and reverse dependency closure."""

from collections.abc import Mapping

from .errors import PluginError
from .models import PluginManifest


def dependency_order(manifests: Mapping[str, PluginManifest], plugin_id: str) -> tuple[str, ...]:
    """Return dependencies before dependants, rejecting missing nodes and cycles."""
    completed: set[str] = set()
    visiting: list[str] = []
    result: list[str] = []

    def visit(current: str) -> None:
        if current in completed:
            return
        if current in visiting:
            raise PluginError(f"Plugin dependency cycle: {' -> '.join((*visiting, current))}")
        if current not in manifests:
            raise PluginError(f"Required plugin not found: {current}")
        visiting.append(current)
        for dependency in manifests[current].requires_plugins:
            visit(dependency)
        visiting.pop()
        completed.add(current)
        result.append(current)

    visit(plugin_id)
    return tuple(result)


def dependants(manifests: Mapping[str, PluginManifest], target: str, enabled: frozenset[str]) -> frozenset[str]:
    """Find all enabled plugins that transitively depend on target."""
    affected = {target}
    while True:
        expanded = affected | {
            name for name in enabled
            if name in manifests and affected.intersection(manifests[name].requires_plugins)
        }
        if expanded == affected:
            return frozenset(expanded & enabled)
        affected = expanded
