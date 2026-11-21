from pathlib import Path

import pytest

from tdnext.core.discovery import discover
from tdnext.core.errors import InstallationConsentRequired, PluginError
from tdnext.core.graph import dependency_order
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.state import StateStore


def manifest(name: str, dependencies: tuple[str, ...] = (), executables: tuple[str, ...] = ()) -> PluginManifest:
    return PluginManifest(
        id=name,
        name=name,
        version="1",
        description=name,
        entrypoint="tdnext.plugins.files.plugin:FilesPlugin",
        capabilities=("desktop.view",),
        view_interfaces=("tdnext.textual-view.v1",),
        requires_plugins=dependencies,
        requires_executables=executables,
    )


def host(name: str) -> PluginManifest:
    return PluginManifest(
        id=name, name=name, version="1", description=name,
        entrypoint=None,
        capabilities=("desktop.host",), host_interfaces=("tdnext.textual-view.v1",),
    )


def test_dependency_order_when_graph_has_shared_dependency() -> None:
    manifests = {
        item.id: item
        for item in (
            manifest("base"),
            manifest("left", ("base",)),
            manifest("right", ("base",)),
            manifest("app", ("left", "right")),
        )
    }
    assert dependency_order(manifests, "app") == ("base", "left", "right", "app")


def test_cycle_when_plugin_dependencies_loop() -> None:
    manifests = {item.id: item for item in (manifest("a", ("b",)), manifest("b", ("a",)))}
    with pytest.raises(PluginError, match="cycle"):
        dependency_order(manifests, "a")


def test_missing_transitive_dependency_is_reported() -> None:
    with pytest.raises(PluginError, match="missing"):
        dependency_order({"app": manifest("app", ("missing",))}, "app")


def test_enable_installs_dependencies_and_persists_state(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (host("base"), manifest("app", ("base",)))}
    store = StateStore(tmp_path / "state.json")
    manager = PluginManager(manifests, store, tmp_path / "bin")
    manager.enable("app")
    assert StateStore(store.path).read() == PluginState(
        installed=frozenset({"base", "app"}), enabled=frozenset({"base", "app"})
    )


def test_install_resolves_dependencies_without_enabling(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (manifest("base"), manifest("app", ("base",)))}
    store = StateStore(tmp_path / "state.json")
    PluginManager(manifests, store, tmp_path / "bin").install("app")
    assert store.read() == PluginState(installed=frozenset({"base", "app"}))


def test_disable_cascades_through_enabled_dependants(tmp_path: Path) -> None:
    manifests = {
        item.id: item
        for item in (
            manifest("base"),
            manifest("mid", ("base",)),
            manifest("app", ("mid",)),
            manifest("other"),
        )
    }
    store = StateStore(tmp_path / "state.json")
    store.write(PluginState(installed=frozenset(manifests), enabled=frozenset(manifests)))
    manager = PluginManager(manifests, store, tmp_path / "bin")
    affected = manager.disable("base")
    assert affected == frozenset({"base", "mid", "app"})
    assert store.read().enabled == frozenset({"other"})


def test_missing_executable_does_not_commit_install(tmp_path: Path) -> None:
    manifests = {"desktop": host("desktop"), "app": manifest("app", executables=("tdnext-test-missing-executable",))}
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(PluginError, match="missing executable"):
        PluginManager(manifests, store, tmp_path / "bin").enable("app")
    assert store.read() == PluginState()


def test_failed_download_does_not_commit_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifests = discover((Path(__file__).parents[1] / "src" / "tdnext" / "plugins",))
    store = StateStore(tmp_path / "state.json")

    def fail(_name: str, _destination: Path, _release: object) -> Path:
        raise PluginError("download failed")

    monkeypatch.setattr("tdnext.plugins.monitor.dependencies.install_release", fail)
    manager = PluginManager(manifests, store, tmp_path / "bin")
    monkeypatch.setattr(manager, "executable", lambda _name: None)
    with pytest.raises(InstallationConsentRequired) as consent:
        manager.install("monitor")
    with pytest.raises(PluginError, match="download failed"):
        manager.install("monitor", approved=consent.value.actions)
    assert store.read() == PluginState()


def test_corrupt_state_is_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PluginError, match="Cannot read plugin state"):
        StateStore(path).read()


def test_enable_with_invalid_entrypoint_does_not_commit_state(tmp_path: Path) -> None:
    manifests = {
        "base": manifest("base"),
        "desktop": host("desktop"),
        "bad": PluginManifest(
            id="bad",
            name="bad",
            version="1",
            description="bad",
            entrypoint="builtins:missing_tdnext_plugin",
        ),
    }
    store = StateStore(tmp_path / "state.json")
    store.write(PluginState(installed=frozenset({"base", "desktop"}), enabled=frozenset({"base", "desktop"})))
    before = store.read()
    with pytest.raises(PluginError, match="Cannot load bad"):
        PluginManager(manifests, store, tmp_path / "bin").enable("bad")
    assert store.read() == before


def test_install_with_invalid_entrypoint_does_not_commit_state(tmp_path: Path) -> None:
    manifests = {
        "bad": PluginManifest(
            id="bad",
            name="bad",
            version="1",
            description="bad",
            entrypoint="builtins:missing_tdnext_plugin",
        ),
    }
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(PluginError, match="Cannot load bad"):
        PluginManager(manifests, store, tmp_path / "bin").install("bad")
    assert store.read() == PluginState()


def test_native_host_has_no_python_entrypoint(tmp_path: Path) -> None:
    manifests = {
        "desktop": PluginManifest(
            id="desktop",
            name="desktop",
            version="1",
            description="desktop",
            entrypoint=None,
            capabilities=("desktop.host",),
            host_interfaces=("tdnext.textual-view.v1",),
        ),
    }
    store = StateStore(tmp_path / "state.json")
    PluginManager(manifests, store, tmp_path / "bin").enable("desktop")
    assert store.read() == PluginState(
        installed=frozenset({"desktop"}), enabled=frozenset({"desktop"})
    )


def test_native_host_entrypoint_lookup_is_reported(tmp_path: Path) -> None:
    from tdnext.core.discovery import load_entrypoint

    manifests = {
        "desktop": PluginManifest(
            id="desktop",
            name="desktop",
            version="1",
            description="desktop",
            entrypoint=None,
            capabilities=("desktop.host",),
            host_interfaces=("tdnext.textual-view.v1",),
        ),
    }
    store = StateStore(tmp_path / "state.json")
    manager = PluginManager(manifests, store, tmp_path / "bin")
    with pytest.raises(PluginError, match="native host"):
        load_entrypoint(manager.manifests["desktop"])


def test_wrong_monitor_binary_does_not_commit_install(tmp_path: Path) -> None:
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    executable = local_bin / "btop"
    executable.write_text("#!/bin/sh\nprintf 'htop 1.0\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    store = StateStore(tmp_path / "state.json")
    manifests = discover((Path(__file__).parents[1] / "src" / "tdnext" / "plugins",))
    with pytest.raises(PluginError, match="not a working btop"):
        PluginManager(manifests, store, local_bin).install("monitor")
    assert store.read() == PluginState()


def test_uninstall_dependency_cascades_disable_but_keeps_dependants_installed(tmp_path: Path) -> None:
    manifests = {
        item.id: item
        for item in (
            manifest("base"),
            manifest("mid", ("base",)),
            manifest("app", ("mid",)),
        )
    }
    store = StateStore(tmp_path / "state.json")
    store.write(PluginState(installed=frozenset(manifests), enabled=frozenset(manifests)))
    affected = PluginManager(manifests, store, tmp_path / "bin").uninstall("base")
    assert affected == frozenset({"base", "mid", "app"})
    assert store.read() == PluginState(installed=frozenset({"mid", "app"}), enabled=frozenset())


def test_uninstall_leaf_only_removes_target(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (manifest("base"), manifest("app", ("base",)))}
    store = StateStore(tmp_path / "state.json")
    store.write(
        PluginState(installed=frozenset({"base", "app"}), enabled=frozenset({"base", "app"}))
    )
    affected = PluginManager(manifests, store, tmp_path / "bin").uninstall("app")
    assert affected == frozenset({"app"})
    assert store.read() == PluginState(installed=frozenset({"base"}), enabled=frozenset({"base"}))


def test_uninstall_shared_dependency_keeps_sibling_branch_installed(tmp_path: Path) -> None:
    manifests = {
        item.id: item
        for item in (
            manifest("base"),
            manifest("left", ("base",)),
            manifest("right", ("base",)),
            manifest("app", ("left", "right")),
        )
    }
    store = StateStore(tmp_path / "state.json")
    store.write(PluginState(installed=frozenset(manifests), enabled=frozenset(manifests)))
    manager = PluginManager(manifests, store, tmp_path / "bin")
    affected = manager.uninstall("left")
    assert affected == frozenset({"left", "app"})
    assert store.read() == PluginState(
        installed=frozenset({"base", "right", "app"}),
        enabled=frozenset({"base", "right"}),
    )


def test_uninstall_repeated_operation_is_idempotent(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (manifest("base"), manifest("app", ("base",)))}
    store = StateStore(tmp_path / "state.json")
    store.write(
        PluginState(installed=frozenset({"base", "app"}), enabled=frozenset({"base", "app"}))
    )
    manager = PluginManager(manifests, store, tmp_path / "bin")
    first = manager.uninstall("base")
    assert first == frozenset({"base", "app"})
    second = manager.uninstall("base")
    assert second == frozenset()
    assert store.read() == PluginState(installed=frozenset({"app"}), enabled=frozenset())


def test_uninstall_keeps_state_consistent_and_reinstallable(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (host("base"), manifest("app", ("base",)))}
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    shared_binary = local_bin / "shared-tool"
    shared_binary.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    shared_binary.chmod(0o755)
    user_file = tmp_path / "user-data.txt"
    user_file.write_text("keep me", encoding="utf-8")
    store = StateStore(tmp_path / "state.json")
    store.write(
        PluginState(installed=frozenset({"base", "app"}), enabled=frozenset({"base", "app"}))
    )
    manager = PluginManager(manifests, store, tmp_path / "bin")
    manager.uninstall("base")
    state = store.read()
    assert state.enabled <= state.installed
    assert state == PluginState(installed=frozenset({"app"}), enabled=frozenset())
    assert shared_binary.is_file()
    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert set(manifests) == {"base", "app"}
    manager.install("base")
    assert store.read().installed == frozenset({"base", "app"})
    manager.enable("app")
    assert store.read() == PluginState(
        installed=frozenset({"base", "app"}), enabled=frozenset({"base", "app"})
    )


def test_uninstall_unknown_plugin_does_not_commit_state(tmp_path: Path) -> None:
    manifests = {item.id: item for item in (manifest("base"),)}
    store = StateStore(tmp_path / "state.json")
    store.write(PluginState(installed=frozenset({"base"}), enabled=frozenset({"base"})))
    before = store.read()
    with pytest.raises(PluginError, match="Plugin not found"):
        PluginManager(manifests, store, tmp_path / "bin").uninstall("missing")
    assert store.read() == before
