from dataclasses import dataclass
from pathlib import Path

from .errors import PluginError
from .models import PluginManifest


@dataclass(frozen=True, slots=True)
class LocalSource:
    package: str
    path: Path
    is_package: bool


def owned_source(manifest: PluginManifest) -> LocalSource:
    module = manifest.entrypoint.partition(":")[0]
    builtin = Path(__file__).resolve().parents[1] / "plugins"
    parts = module.split(".")
    source = manifest.source_dir
    if module.startswith("tdnext.plugins.") and len(parts) >= 3:
        candidate = builtin / parts[2]
        if source is None or source.resolve() == candidate.resolve():
            source = candidate
            package = ".".join(parts[:3])
            owned = LocalSource(package, candidate, True)
        else:
            raise ownership_error(manifest)
    elif source is not None and parts[0] != "tdnext":
        package = parts[0]
        candidate = source / package
        if candidate.is_dir():
            owned = LocalSource(package, candidate, True)
        elif candidate.with_suffix(".py").is_file():
            owned = LocalSource(package, candidate.with_suffix(".py"), False)
        else:
            raise ownership_error(manifest)
    else:
        raise ownership_error(manifest)
    if not owned.path.exists() or owned.path.is_symlink():
        raise ownership_error(manifest)
    if manifest.dependency_hook is not None:
        hook = manifest.dependency_hook.partition(":")[0]
        if hook != owned.package and not hook.startswith(owned.package + "."):
            raise ownership_error(manifest)
    return owned


def ownership_error(manifest: PluginManifest) -> PluginError:
    return PluginError(
        f"Cannot load {manifest.id}: local source ownership is unknown. Move its owned Python package/module "
        f"beside plugin.toml, use that package in entrypoint and dependency_hook, then run "
        f"tdnext install {manifest.id}. External import paths are never copied or removed."
    )
