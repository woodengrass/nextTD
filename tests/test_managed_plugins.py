import importlib
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tdnext.core.discovery import discover
from tdnext.core.errors import InstallationConsentRequired, PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.plugin_code import InstalledCode
from tdnext.core.plugin_imports import RUNTIME_ENV, ImportRoutes, ManagedImports, activate
from tdnext.core.removal import RemovalRequest
from tdnext.core.state import StateStore


def local_manager(tmp_path: Path) -> PluginManager:
    source = tmp_path / "sources" / "sample"
    source.mkdir(parents=True)
    (source / "plugin.toml").write_text(
        'id = "sample"\nname = "Sample"\nversion = "1"\ndescription = "Local test plugin"\n'
        'manifest_version = 1\napi_version = 1\ncapabilities = ["desktop.host"]\n'
        'host_interfaces = ["sample.view.v1"]\nentrypoint = "sample_plugin:Plugin"\n',
        encoding="utf-8",
    )
    package = source / "sample_plugin"
    package.mkdir()
    (package / "__init__.py").write_text(
        'from pathlib import Path\nclass Plugin:\n'
        '    origin = Path(__file__)\n'
        '    def run(self, context, views): pass\n',
        encoding="utf-8",
    )
    state = tmp_path / "state"
    return PluginManager(discover((tmp_path / "sources",)), StateStore(state / "plugins.json"), state / "bin")


@contextmanager
def allow_removal(request: RemovalRequest) -> Iterator[tuple[str, ...]]:
    yield ()


def guarded_manager(tmp_path: Path) -> PluginManager:
    manager = local_manager(tmp_path)
    manager.removal_guard = allow_removal
    return manager


def test_install_runs_a_copy_and_retains_local_source(tmp_path: Path) -> None:
    manager = local_manager(tmp_path)
    manager.install("sample")

    plugin = manager.check_dependencies(("sample",))["sample"]
    origin = plugin.origin  # type: ignore[attr-defined]
    assert isinstance(origin, Path)
    assert origin.is_relative_to(manager.store.path.parent / "plugin-code")
    assert origin.is_file()
    assert (tmp_path / "sources/sample/sample_plugin/__init__.py").is_file()


def test_install_immediately_routes_lazy_imports_and_children_to_published_code(tmp_path: Path) -> None:
    manager = local_manager(tmp_path)
    source = tmp_path / "sources/sample/sample_plugin"
    (source / "lazy.py").write_text("from pathlib import Path\norigin = str(Path(__file__))\n", encoding="utf-8")

    manager.install("sample")

    expected = manager.code_store.slot("sample") / "code" / "lazy.py"
    assert importlib.import_module("sample_plugin.lazy").origin == str(expected)
    child = subprocess.run(
        [sys.executable, "-c", "import tdnext, sample_plugin.lazy; print(sample_plugin.lazy.origin)"],
        capture_output=True, text=True, check=True,
    )
    assert child.stdout.strip() == str(expected)


@pytest.mark.parametrize("failure", ["consent", "dependency", "commit"])
@pytest.mark.parametrize("previous_route", [False, True])
def test_failed_install_restores_import_routes_environment_and_invalidates_staged_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, previous_route: bool,
) -> None:
    manager = local_manager(tmp_path)
    source = tmp_path / "sources/sample"
    with (source / "plugin.toml").open("a", encoding="utf-8") as manifest:
        manifest.write('dependency_hook = "sample_plugin.hook:Dependencies"\n')
    check = {"consent": "return 'install tool'", "dependency": "raise RuntimeError('dependency failed')",
             "commit": "return None"}[failure]
    (source / "sample_plugin/hook.py").write_text(
        f"class Dependencies:\n    @staticmethod\n    def check(manager):\n        {check}\n", encoding="utf-8",
    )
    manager = PluginManager(discover((tmp_path / "sources",)), manager.store, manager.local_bin)
    old = tmp_path / "old"
    old.mkdir()
    (old / "__init__.py").write_text("value = 'old'\n", encoding="utf-8")
    (old / "lazy.py").write_text("value = 'old lazy'\n", encoding="utf-8")
    old_code = InstalledCode(package="sample_plugin", path=old, generation="a" * 32, is_package=True)
    unrelated = InstalledCode(package="unrelated_plugin", path=old, generation="b" * 32, is_package=True)
    routes = {"unrelated_plugin": unrelated, "unrelated_dead": None}
    if previous_route:
        routes["sample_plugin"] = old_code
    activate(ImportRoutes(packages=routes))
    finder = next(item for item in sys.meta_path if isinstance(item, ManagedImports))
    before = dict(finder.routes)
    monkeypatch.setenv(RUNTIME_ENV, "original environment")
    if failure == "commit":
        def fail_write(state: PluginState) -> None:
            raise OSError("commit failed")

        monkeypatch.setattr(manager.store, "write", fail_write)

    error = {"consent": InstallationConsentRequired, "dependency": RuntimeError, "commit": OSError}[failure]
    with pytest.raises(error):
        manager.install("sample")

    assert finder.routes == before
    assert os.environ[RUNTIME_ENV] == "original environment"
    assert "sample_plugin.hook" not in sys.modules
    assert not manager.code_store.slot("sample").exists()
    if previous_route:
        assert importlib.import_module("sample_plugin.lazy").value == "old lazy"


def test_enable_disable_preserves_installed_code(tmp_path: Path) -> None:
    manager = guarded_manager(tmp_path)
    manager.enable("sample")
    assert manager.store.read().enabled == frozenset({"sample"})
    slot = manager.code_store.slot("sample")
    assert slot.is_dir()

    affected = manager.disable("sample")
    assert affected == frozenset({"sample"})
    assert manager.store.read().installed == frozenset({"sample"})
    assert manager.store.read().enabled == frozenset()
    assert slot.is_dir()


def test_uninstall_removes_code_preserves_settings_and_tools(tmp_path: Path) -> None:
    manager = guarded_manager(tmp_path)
    manager.enable("sample")
    settings = manager.store.path.parent / "sample-settings.json"
    settings.write_text('{"keep": true}', encoding="utf-8")
    manager.local_bin.mkdir(parents=True, exist_ok=True)
    tool = manager.local_bin / "external-tool"
    tool.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    tool.chmod(0o755)
    slot = manager.code_store.slot("sample")
    assert slot.is_dir()

    affected = manager.uninstall("sample")
    assert affected == frozenset({"sample"})
    assert manager.store.read() == PluginState()
    assert not slot.exists()
    assert settings.read_text(encoding="utf-8") == '{"keep": true}'
    assert tool.is_file()
    assert (tmp_path / "sources/sample/sample_plugin/__init__.py").is_file()

    manager.install("sample")
    assert manager.store.read().installed == frozenset({"sample"})
    plugin = manager.check_dependencies(("sample",))["sample"]
    reinstall_origin = plugin.origin  # type: ignore[attr-defined]
    assert reinstall_origin.is_relative_to(manager.store.path.parent / "plugin-code")


def test_uninstall_is_idempotent_without_guard_side_effects(tmp_path: Path) -> None:
    manager = guarded_manager(tmp_path)
    manager.install("sample")
    assert manager.uninstall("sample") == frozenset()
    assert manager.uninstall("sample") == frozenset()
    assert manager.store.read() == PluginState()


def test_installed_without_managed_code_requires_install(tmp_path: Path) -> None:
    manager = local_manager(tmp_path)
    manager.store.write(PluginState(installed=frozenset({"sample"}), enabled=frozenset({"sample"})))
    with pytest.raises(PluginError, match="run tdnext install sample"):
        manager.check_dependencies(("sample",))


def test_uninstall_without_managed_code_preserves_settings_and_tool(tmp_path: Path) -> None:
    manager = guarded_manager(tmp_path)
    manager.store.write(PluginState(installed=frozenset({"sample"}), enabled=frozenset({"sample"})))
    settings = manager.store.path.parent / "sample-settings.json"
    settings.write_text('{"keep": true}', encoding="utf-8")
    manager.local_bin.mkdir()
    tool = manager.local_bin / "external-tool"
    tool.write_text("keep", encoding="utf-8")

    assert manager.uninstall("sample") == frozenset({"sample"})
    assert manager.store.read() == PluginState()
    assert not manager.code_store.slot("sample").exists()
    assert settings.read_text(encoding="utf-8") == '{"keep": true}'
    assert tool.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "sources/sample/sample_plugin/__init__.py").is_file()
    manager.install("sample")
    assert manager.code_store.slot("sample").is_dir()


def test_startup_does_not_auto_install(tmp_path: Path) -> None:
    manager = local_manager(tmp_path)
    assert manager.store.read() == PluginState()
    assert not (manager.store.path.parent / "plugin-code").exists()
    assert not manager.store.path.exists()
