"""Discover local TOML manifests and load their declared entry points."""

from importlib import import_module
from pathlib import Path
from tomllib import TOMLDecodeError, loads

from pydantic import ValidationError

from .errors import PluginError
from .models import PluginManifest


def discover(roots: tuple[Path, ...]) -> dict[str, PluginManifest]:
    manifests: dict[str, PluginManifest] = {}
    for root in roots:
        for path in sorted(root.glob("*/plugin.toml")):
            try:
                manifest = PluginManifest.model_validate(loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, TOMLDecodeError, ValidationError) as exc:
                raise PluginError(f"Invalid plugin manifest {path}: {exc}") from exc
            if manifest.id in manifests:
                raise PluginError(f"Duplicate plugin id: {manifest.id} ({path})")
            manifests[manifest.id] = manifest
    return manifests


def load_entrypoint(manifest: PluginManifest):
    """Import the declared local plugin class; plugins are trusted code."""
    module_name, _, class_name = manifest.entrypoint.partition(":")
    try:
        plugin_class = getattr(import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise PluginError(f"Cannot load {manifest.id} ({manifest.entrypoint}): {exc}") from exc
    if not callable(plugin_class):
        raise PluginError(f"Plugin entry point is not callable: {manifest.entrypoint}")
    return plugin_class


def instantiate(manifest: PluginManifest):
    try:
        return load_entrypoint(manifest)()
    except PluginError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"Cannot load {manifest.id} ({manifest.entrypoint}): {exc}") from exc
