from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core.manager import PluginManager
    from .core.session import SessionSnapshot


@dataclass(frozen=True, slots=True)
class PluginContext:
    directory: Path
    manager: PluginManager
    session: SessionSnapshot | None = None
    view_id: str | None = None
