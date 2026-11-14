"""Appearance preferences, stored independently from plugin lifecycle state."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tdnext.api import IconMode

from .errors import PluginError
from .state import atomic_write


class Appearance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]*$")
    icon_mode: IconMode = "compatibility"


class AppearanceStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Appearance:
        try:
            return Appearance.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Appearance()
        except (OSError, UnicodeError, ValidationError) as exc:
            raise PluginError(f"Cannot read appearance {self.path}: {exc}") from exc

    def write(self, appearance: Appearance) -> None:
        try:
            atomic_write(self.path, appearance.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            raise PluginError(f"Cannot save appearance {self.path}: {exc}") from exc
