import importlib
import os
import sys
from collections.abc import Iterator

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _covered_by(name: str, packages: set[str]) -> bool:
    return (
        name == "tdnext"
        or name.startswith("tdnext.")
        or any(name == package or name.startswith(package + ".") for package in packages)
    )


@pytest.fixture(autouse=True)
def _isolate_managed_import_state() -> Iterator[None]:
    """Keep managed plugin imports process-local to each test.

    Enabling a real plugin stages a managed copy under that test's tmp dir,
    routes its package through ``ManagedImports`` and caches the copy in
    ``sys.modules``. Without cleanup the next test resolves the same package
    from the previous tmp dir, so a monkeypatch on the source-tree class
    misses the imported copy. Snapshot the routing, the cached modules (and
    their parent attributes) and ``TDNEXT_PLUGIN_CODE``, then restore them so
    production managed-import behavior still runs normally inside each test.
    """
    from tdnext.core import plugin_imports

    finder = next((item for item in sys.meta_path if isinstance(item, plugin_imports.ManagedImports)), None)
    saved_routes = dict(finder.routes) if finder is not None else None
    owned = set(saved_routes) if saved_routes is not None else set()

    saved_modules = {name: module for name, module in sys.modules.items() if _covered_by(name, owned)}
    saved_attrs: dict[str, tuple[bool, object]] = {}
    for name in saved_modules:
        parent, sep, child = name.rpartition(".")
        if not sep:
            continue
        parent_module = sys.modules.get(parent)
        if parent_module is None:
            continue
        try:
            saved_attrs[name] = (True, getattr(parent_module, child))
        except AttributeError:
            saved_attrs[name] = (False, None)

    saved_code = os.environ.get(plugin_imports.RUNTIME_ENV)
    yield

    current = next((item for item in sys.meta_path if isinstance(item, plugin_imports.ManagedImports)), None)
    live = set(current.routes) if current is not None else set()
    if saved_routes is None:
        if current is not None:
            sys.meta_path.remove(current)
    elif current is None:
        current = plugin_imports.ManagedImports()
        current.routes.update(saved_routes)
        sys.meta_path.insert(0, current)
    else:
        current.routes.clear()
        current.routes.update(saved_routes)
    covered = owned | live

    for name in [name for name in sys.modules if name not in saved_modules and _covered_by(name, covered)]:
        removed = sys.modules.pop(name)
        parent, sep, child = name.rpartition(".")
        if sep:
            parent_module = sys.modules.get(parent)
            if parent_module is not None and getattr(parent_module, child, None) is removed:
                delattr(parent_module, child)
    for name, module in saved_modules.items():
        if sys.modules.get(name) is not module:
            sys.modules[name] = module
    for name, (had_attr, value) in saved_attrs.items():
        parent, _, child = name.rpartition(".")
        parent_module = sys.modules.get(parent)
        if parent_module is None:
            continue
        if had_attr:
            if getattr(parent_module, child, None) is not value:
                setattr(parent_module, child, value)
        elif hasattr(parent_module, child):
            delattr(parent_module, child)
    if saved_code is None:
        os.environ.pop(plugin_imports.RUNTIME_ENV, None)
    else:
        os.environ[plugin_imports.RUNTIME_ENV] = saved_code
    importlib.invalidate_caches()
