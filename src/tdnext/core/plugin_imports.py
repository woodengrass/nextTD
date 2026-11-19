import importlib
import os
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_file_location
from types import ModuleType
from typing import Final

from pydantic import BaseModel, ConfigDict

from .errors import PluginError
from .plugin_code import InstalledCode

RUNTIME_ENV: Final = "TDNEXT_PLUGIN_CODE"


class ImportRoutes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    packages: dict[str, InstalledCode | None]


class ManagedImports(MetaPathFinder):
    def __init__(self) -> None:
        self.routes: dict[str, InstalledCode | None] = {}

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        for package, code in self.routes.items():
            if fullname != package and not fullname.startswith(package + "."):
                continue
            if code is None or not code.path.exists():
                raise ModuleNotFoundError(f"Managed plugin code is not installed: {package}")
            relative = fullname.removeprefix(package).lstrip(".").replace(".", "/")
            location = code.path / relative if relative else code.path
            if location.is_dir():
                init = location / "__init__.py"
                if init.is_file():
                    return spec_from_file_location(fullname, init, submodule_search_locations=[str(location)])
                spec = ModuleSpec(fullname, loader=None, is_package=True)
                spec.submodule_search_locations = [str(location)]
                return spec
            module = location.with_suffix(".py") if relative else location
            if module.is_file():
                return spec_from_file_location(fullname, module)
            raise ModuleNotFoundError(f"Module {fullname} is absent from installed plugin code")
        # No route claims this package (e.g. synthetic test entrypoints): fall
        # through to the normal import instead of blocking it. Explicitly
        # deactivated packages (route set to None on uninstall) still raise above.
        return None


def prune_dead_routes(keep: set[str]) -> None:
    """Drop routes no living manager claims.

    Explicitly deactivated (None) or deleted (missing path) entries for other
    packages are stale cross-manager pollution, e.g. previous tmp dirs, rather
    than authority over this process. Live slots are always kept.
    """
    finder = next((item for item in sys.meta_path if isinstance(item, ManagedImports)), None)
    if finder is None:
        return
    for package, code in tuple(finder.routes.items()):
        if package not in keep and (code is None or not code.path.exists()):
            del finder.routes[package]


def activate(routes: ImportRoutes) -> None:
    finder = next((item for item in sys.meta_path if isinstance(item, ManagedImports)), None)
    if finder is None:
        finder = ManagedImports()
        sys.meta_path.insert(0, finder)
    for package, code in routes.packages.items():
        if package in finder.routes and finder.routes[package] == code:
            continue
        # Invalidate the whole owned package, including a cached source parent and its children.
        for name in tuple(sys.modules):
            if name == package or name.startswith(package + "."):
                sys.modules.pop(name, None)
                parent, _, child = name.rpartition(".")
                parent_module = sys.modules.get(parent)
                if parent_module is not None and hasattr(parent_module, child):
                    delattr(parent_module, child)
        finder.routes[package] = code
    importlib.invalidate_caches()
    # Deactivated (None) routes guard this process only; children resolve
    # via live mappings or the source tree, so never propagate the blocks.
    live: dict[str, InstalledCode | None] = {
        package: code for package, code in finder.routes.items() if code is not None
    }
    os.environ[RUNTIME_ENV] = ImportRoutes(packages=live).model_dump_json()


def snapshot_routes() -> tuple[ImportRoutes, str | None, bool]:
    finder = next((item for item in sys.meta_path if isinstance(item, ManagedImports)), None)
    return ImportRoutes(packages=dict(finder.routes) if finder else {}), os.environ.get(RUNTIME_ENV), finder is not None


def restore_routes(snapshot: tuple[ImportRoutes, str | None, bool]) -> None:
    routes, environment, had_finder = snapshot
    finder = next((item for item in sys.meta_path if isinstance(item, ManagedImports)), None)
    current = finder.routes if finder is not None else {}
    removed = {package: None for package in current if package not in routes.packages}
    activate(ImportRoutes(packages={**removed, **routes.packages}))
    finder = next(item for item in sys.meta_path if isinstance(item, ManagedImports))
    finder.routes.clear()
    finder.routes.update(routes.packages)
    if not had_finder:
        sys.meta_path.remove(finder)
    if environment is None:
        os.environ.pop(RUNTIME_ENV, None)
    else:
        os.environ[RUNTIME_ENV] = environment


def activate_code(code: InstalledCode) -> None:
    if not code.path.exists():
        raise PluginError(f"Managed plugin code is missing: {code.path}; explicitly reinstall the plugin")
    activate(ImportRoutes(packages={code.package: code}))


def bootstrap() -> None:
    raw = os.environ.get(RUNTIME_ENV)
    if raw is not None:
        activate(ImportRoutes.model_validate_json(raw))
