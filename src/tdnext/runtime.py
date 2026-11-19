"""Construct the isolated runtime from bundled and user-local manifests."""

from pathlib import Path

from .core.discovery import discover
from .core.local_env import resolve_state_dir
from .core.manager import PluginManager
from .core.removal import RemovalGuard
from .core.state import StateStore


def state_dir() -> Path:
    return resolve_state_dir()


def manager_for(state: Path, *, removal_guard: RemovalGuard | None = None) -> PluginManager:
    builtin = Path(__file__).parent / "plugins"
    discovered = discover((builtin, state / "plugins"))
    store = StateStore(state / "plugins.json")
    if removal_guard is None:
        return PluginManager(discovered, store, state / "bin")
    return PluginManager(discovered, store, state / "bin", removal_guard=removal_guard)


def manager() -> PluginManager:
    return manager_for(state_dir())
