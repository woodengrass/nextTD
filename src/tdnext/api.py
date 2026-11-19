"""Contract between the desktop host and a trusted view plugin."""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget

from .context import PluginContext as PluginContext
from .core.manager import PluginManager
from .core.models import PluginManifest
from .terminal_contract import TERMINAL_VIEW_INTERFACE as TERMINAL_VIEW_INTERFACE
from .terminal_contract import TerminalViewPlugin as TerminalViewPlugin

IconMode = Literal["compatibility", "codicons"]
PLUGIN_API_VERSION = 1


class DependencyHook(Protocol):
    @staticmethod
    def check(manager: PluginManager) -> str | None: ...

    @staticmethod
    def install(manager: PluginManager) -> Path: ...


class CloseView(Message):
    pass


class SettingsControls(Vertical):
    """Optional plugin-owned controls hosted in Settings General."""

    class Feedback(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def refresh_settings(self) -> None:
        """Refresh persisted values when the settings view is activated."""

    def refresh_if_idle(self) -> None:
        """Refresh persisted values without interrupting an active edit."""


@runtime_checkable
class SettingsContributor(Protocol):
    def make_settings_controls(self, context: PluginContext) -> SettingsControls: ...


@runtime_checkable
class ViewPlugin(Protocol):
    shortcuts: str

    def make_view(self, context: PluginContext) -> Widget: ...

    def run_pane(self, context: PluginContext) -> None: ...


@runtime_checkable
class IconProvider(Protocol):
    """Resolve a semantic icon name in either explicitly selected display mode."""

    def resolve(self, key: str, mode: IconMode) -> str: ...


@runtime_checkable
class ActivatableView(Protocol):
    """A view that refreshes when its native terminal pane regains focus."""

    def on_native_activation(self) -> None: ...


@runtime_checkable
class DesktopHost(Protocol):
    """Own the desktop shell, including its choice of native or fullscreen presentation."""

    def run(self, context: PluginContext, views: Sequence[tuple[PluginManifest, object]]) -> None: ...
