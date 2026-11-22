"""Persistent editor preference, stored independently from plugin lifecycle state."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tdnext.core.errors import PluginError
from tdnext.core.state import atomic_write

EditorChoice = Literal["environment", "nvim", "vi"]


class EditorPreferences(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    editor: EditorChoice = Field(default="environment")


class EditorPreferencesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> EditorPreferences:
        try:
            return EditorPreferences.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return EditorPreferences()
        except (OSError, UnicodeError, ValidationError) as exc:
            raise PluginError(f"Cannot read editor preferences {self.path}: {exc}") from exc

    def write(self, preferences: EditorPreferences) -> None:
        try:
            atomic_write(self.path, preferences.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            raise PluginError(f"Cannot save editor preferences {self.path}: {exc}") from exc
