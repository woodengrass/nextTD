"""Framework-neutral contract for a view running in its own terminal."""

from typing import Protocol, runtime_checkable

from .context import PluginContext

TERMINAL_VIEW_INTERFACE = "tdnext.terminal-view.v1"
TEXTUAL_VIEW_INTERFACE = "tdnext.textual-view.v1"


@runtime_checkable
class TerminalViewPlugin(Protocol):
    def run_pane(self, context: PluginContext) -> None: ...


def compatible_views(required: tuple[str, ...], supported: tuple[str, ...]) -> bool:
    available = set(supported)
    if TEXTUAL_VIEW_INTERFACE in available:
        available.add(TERMINAL_VIEW_INTERFACE)
    if TERMINAL_VIEW_INTERFACE in available:
        available.add(TEXTUAL_VIEW_INTERFACE)
    return set(required) <= available
