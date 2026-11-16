"""Contract between the desktop host and a trusted view plugin."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from textual.message import Message
from textual.widget import Widget

from .core.manager import PluginManager
from .core.models import PluginManifest
from .core.session import SessionSnapshot

IconMode = Literal["compatibility", "codicons"]
PLUGIN_API_VERSION = 1


class DependencyHook(Protocol):
    @staticmethod
    def check(manager: PluginManager) -> str | None: ...

    @staticmethod
    def install(manager: PluginManager) -> Path: ...


@dataclass(frozen=True, slots=True)
class PluginContext:
    directory: Path
    manager: PluginManager
    session: SessionSnapshot | None = None


class CloseView(Message):
    pass


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
