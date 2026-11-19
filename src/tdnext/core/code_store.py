import fcntl
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from .errors import PluginError
from .models import PluginManifest
from .plugin_code import InstalledCode
from .plugin_sources import owned_source
from .state import StateStore, atomic_write


class CodeStore:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.root = state.path.parent / "plugin-code"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.state.path.parent.mkdir(parents=True, exist_ok=True)
        with self.state.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def slot(self, plugin_id: str) -> Path:
        if not plugin_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in plugin_id):
            raise PluginError(f"Invalid plugin id: {plugin_id}")
        path = self.root / plugin_id
        if self.root.is_symlink() or path.is_symlink():
            raise PluginError(f"Managed code location must not be a symlink: {path}")
        return path

    def read(self, plugin_id: str) -> PluginManifest | None:
        slot = self.slot(plugin_id)
        receipt = slot / "manifest.json"
        try:
            manifest = PluginManifest.model_validate_json(receipt.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if slot.exists():
                raise PluginError(f"Unrecognized code directory {slot}; move it aside before installing") from None
            return None
        except (OSError, ValueError, ValidationError) as exc:
            raise PluginError(f"Cannot read managed plugin receipt {receipt}: {exc}") from exc
        code = manifest.code
        expected = slot / ("code" if code is not None and code.is_package else "code.py")
        if manifest.id != plugin_id or code is None or code.path != expected.resolve() or code.path.is_symlink():
            raise PluginError(f"Invalid ownership receipt: {receipt}")
        return manifest

    def stage(self, manifest: PluginManifest, directory: Path) -> PluginManifest:
        source = owned_source(manifest)
        destination = directory / ("code" if source.is_package else "code.py")
        entries = source.path.rglob("*") if source.is_package else (source.path,)
        for entry in entries:
            if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                raise PluginError(f"Local plugin source must contain regular files/directories: {entry}")
        if source.is_package:
            shutil.copytree(source.path, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source.path, destination)
        code = InstalledCode(
            package=source.package, path=destination.resolve(), generation=uuid4().hex, is_package=source.is_package,
        )
        return manifest.model_copy(update={"code": code})

    def publish(self, manifest: PluginManifest) -> PluginManifest:
        code = manifest.code
        if code is None:
            raise PluginError(f"Cannot publish unstaged plugin {manifest.id}")
        slot = self.slot(manifest.id)
        published = manifest.model_copy(update={"code": code.model_copy(update={
            "path": (slot / code.path.name).resolve(),
        })})
        atomic_write(code.path.parent / "manifest.json", published.model_dump_json())
        code.path.parent.rename(slot)
        return published
