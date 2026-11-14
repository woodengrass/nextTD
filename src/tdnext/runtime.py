"""Construct the isolated runtime from bundled and user-local manifests."""

from pathlib import Path

from .core.discovery import discover
from .core.local_env import resolve_state_dir
from .core.manager import PluginManager
from .core.state import StateStore


def state_dir() -> Path:
    return resolve_state_dir()


def manager() -> PluginManager:
    state = state_dir()
    builtin = Path(__file__).parent / "plugins"
    return PluginManager(
        discover((builtin, state / "plugins")), StateStore(state / "plugins.json"), state / "bin"
    )
