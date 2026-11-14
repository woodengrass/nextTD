"""Read and atomically replace one isolated plugin state file."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import ValidationError

from .errors import PluginError
from .models import PluginState


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> PluginState:
        try:
            return PluginState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return PluginState()
        except (OSError, UnicodeError, ValidationError) as exc:
            raise PluginError(f"Cannot read plugin state {self.path}: {exc}") from exc

    def write(self, state: PluginState) -> None:
        try:
            atomic_write(self.path, state.model_dump_json(indent=2) + "\n")
        except OSError as exc:
            raise PluginError(f"Cannot save plugin state {self.path}: {exc}") from exc
