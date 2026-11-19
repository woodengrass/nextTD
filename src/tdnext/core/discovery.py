"""Discover local TOML manifests and load their declared entry points."""

from importlib import import_module
from pathlib import Path
from tomllib import TOMLDecodeError, loads

from pydantic import ValidationError

from .errors import PluginError
from .models import PluginManifest


class DiscoveredPlugins(dict[str, PluginManifest]):
    def __init__(self) -> None:
        super().__init__()
        self.errors: dict[str, str] = {}


def discover(roots: tuple[Path, ...]) -> DiscoveredPlugins:
    manifests = DiscoveredPlugins()
    paths: dict[str, list[Path]] = {}
    for root in roots:
        for path in sorted(root.glob("*/plugin.toml")):
            identity: str | None = None
            try:
                data = loads(path.read_text(encoding="utf-8"))
                if "source_dir" in data or "code" in data:
                    raise ValueError("source_dir and code are runtime metadata, not manifest fields")
                raw_identity = data.get("id")
                if isinstance(raw_identity, str):
                    identity = raw_identity
                    paths.setdefault(identity, []).append(path)
                if not {"manifest_version", "api_version", "capabilities"} <= data.keys():
                    raise ValueError("manifest_version, api_version and capabilities are required")
                if type(data["manifest_version"]) is not int or type(data["api_version"]) is not int:
                    raise ValueError("manifest_version and api_version must be integers")
                manifest = PluginManifest.model_validate(data).model_copy(update={"source_dir": path.parent.resolve()})
                if manifest.role == "desktop.host" and not manifest.host_interfaces:
                    raise ValueError("host_interfaces must declare supported interfaces")
                if manifest.role == "desktop.view" and not manifest.view_interfaces:
                    raise ValueError("view_interfaces must declare required interfaces")
                if manifest.role != "desktop.host" and manifest.host_interfaces:
                    raise ValueError("only hosts may declare host_interfaces")
                if manifest.role != "desktop.view" and manifest.view_interfaces:
                    raise ValueError("only views may declare view_interfaces")
            except (OSError, UnicodeError, TOMLDecodeError, ValidationError, ValueError) as exc:
                key = identity if identity is not None else str(path)
                manifests.errors[key] = f"Invalid plugin manifest {path}: {exc}"
                continue
            manifests[manifest.id] = manifest
    for identity, conflicts in paths.items():
        if len(conflicts) > 1:
            manifests.pop(identity, None)
            manifests.errors[identity] = (
                f"Duplicate plugin id {identity}: {', '.join(map(str, conflicts))}; remove the conflict"
            )
        elif identity in manifests.errors:
            manifests.pop(identity, None)
    return manifests


def load_entrypoint(manifest: PluginManifest):
    """Import the declared local plugin class; plugins are trusted code.

    Manifests with managed code activate that copy first. Real discovered
    manifests (source_dir is set) without managed code fail with an explicit
    install instruction and never fall back to the source tree. In-memory
    doubles (source_dir is None) load from the source tree for tests.
    """
    module_name, _, class_name = manifest.entrypoint.partition(":")
    if manifest.code is not None:
        from .plugin_imports import activate_code

        activate_code(manifest.code)
    elif manifest.source_dir is not None:
        raise PluginError(f"Plugin {manifest.id} has no managed code; run tdnext install {manifest.id}")
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
