import os
from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import DataTable

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.errors import PluginError
from tdnext.core.models import PluginState
from tdnext.core.presentation import snapshot_views
from tdnext.launcher import enabled_views
from tdnext.plugins.settings.view import SettingsView


class ProviderInitializationError(RuntimeError):
    pass


class RaisingProvider:
    def __init__(self) -> None:
        raise ProviderInitializationError("provider failed to initialize")


@pytest.mark.parametrize("failure", ["missing", "import", "constructor", "contract"])
def test_unavailable_enabled_provider_keeps_views_and_preferences(tmp_path: Path, failure: str) -> None:
    app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    provider = manifests["icons"]
    if failure == "missing":
        del manifests["icons"]
    else:
        entrypoints = {
            "import": "tdnext_missing_icon_module:Provider",
            "constructor": f"{__name__}:RaisingProvider",
            "contract": "builtins:int",
        }
        manifests["icons"] = provider.model_copy(update={"entrypoint": entrypoints[failure]})
    manager.manifests = manifests
    before = manager.store.read()
    state = PluginState(installed=before.installed | {"icons"}, enabled=before.enabled | {"icons"})
    manager.store.write(state)
    appearance = AppearanceStore(manager.store.path.with_name("appearance.json"))
    appearance.write(Appearance(provider_id="icons", icon_mode="codicons"))
    saved_appearance = appearance.path.read_bytes()
    views = enabled_views(manager)
    assert [manifest.id for manifest, _ in views] == ["files", "settings"]
    assert snapshot_views(manager, views).icon_for("files") == app.context.manager.manifests["files"].icon
    assert manager.store.read() == state
    assert appearance.path.read_bytes() == saved_appearance


def test_missing_provider_remains_fatal_when_a_view_requires_it(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests["files"] = manifests["files"].model_copy(update={"requires_plugins": ("desktop", "icons")})
    del manifests["icons"]
    manager.manifests = manifests
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"icons"}, enabled=state.enabled | {"icons"}))
    AppearanceStore(manager.store.path.with_name("appearance.json")).write(Appearance(provider_id="icons"))
    with pytest.raises(PluginError, match="icons"):
        enabled_views(manager)


def test_broken_provider_remains_fatal_when_a_view_requires_it(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests["files"] = manifests["files"].model_copy(update={"requires_plugins": ("desktop", "icons")})
    manifests["icons"] = manifests["icons"].model_copy(update={"entrypoint": "builtins:int"})
    manager.manifests = manifests
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"icons"}, enabled=state.enabled | {"icons"}))
    with pytest.raises(PluginError, match="icon provider contract"):
        enabled_views(manager)


def test_missing_bundled_provider_is_optional_even_without_an_appearance_selection(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    manager.enable("icons")
    manager.manifests = {key: value for key, value in manager.manifests.items() if key != "icons"}
    assert [manifest.id for manifest, _ in enabled_views(manager)] == ["files", "settings"]
    assert not manager.store.path.with_name("appearance.json").exists()


def test_missing_selected_alternative_provider_remains_optional(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(
        installed=state.installed | {"other-icons"}, enabled=state.enabled | {"other-icons"}
    ))
    appearance = AppearanceStore(manager.store.path.with_name("appearance.json"))
    appearance.write(Appearance(provider_id="other-icons", icon_mode="codicons"))
    views = enabled_views(manager)
    assert [manifest.id for manifest, _ in views] == ["files", "settings"]
    assert snapshot_views(manager, views).icon_for("files") == "▤"
    assert appearance.read().provider_id == "other-icons"


def test_unclassified_missing_enabled_plugin_is_reported(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(
        installed=state.installed | {"unclassified"}, enabled=state.enabled | {"unclassified"}
    ))
    with pytest.raises(PluginError, match="unclassified"):
        enabled_views(manager)


@pytest.mark.parametrize("broken", ["desktop", "files"])
def test_broken_required_host_or_view_is_not_ignored(tmp_path: Path, broken: str) -> None:
    _app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests[broken] = manifests[broken].model_copy(update={"entrypoint": "tdnext_missing_required:Plugin"})
    manager.manifests = manifests
    with pytest.raises(PluginError, match=broken):
        enabled_views(manager)


@pytest.mark.parametrize("operation", ["disable", "uninstall"])
def test_missing_installed_plugin_can_be_removed_without_touching_other_plugins(tmp_path: Path, operation: str) -> None:
    _app, manager = make_app(tmp_path)
    initial = manager.store.read()
    manager.store.write(PluginState(installed=initial.installed | {"missing"}, enabled=initial.enabled | {"missing"}))
    affected = getattr(manager, operation)("missing")
    result = manager.store.read()
    assert affected == frozenset({"missing"})
    assert result.enabled == initial.enabled
    assert result.installed == initial.installed | ({"missing"} if operation == "disable" else set())


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["disable", "uninstall"])
async def test_settings_can_manage_persisted_missing_plugin(tmp_path: Path, operation: str) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    manager.manifests = {key: value for key, value in manager.manifests.items() if key != "icons"}
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("icons"))
        await pilot.pause()
        await pilot.click(f"#plugin-{operation}")
        if operation == "uninstall":
            await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        assert "icons" not in manager.store.read().enabled


def test_required_executable_is_installed_before_entrypoint_import_and_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, manager = make_app(tmp_path)
    binary = manager.local_bin / "tdnext-fixture-binary"
    module = tmp_path / "requires_binary_at_import.py"
    module.write_text(
        "import os\nfrom pathlib import Path\n"
        "if not Path(os.environ['TDNEXT_TEST_BINARY']).is_file(): raise RuntimeError('missing at import')\n"
        "from tdnext.plugins.files.plugin import FilesPlugin\n"
        "class RequiredPlugin(FilesPlugin):\n"
        "    def __init__(self):\n"
        "        if not Path(os.environ['TDNEXT_TEST_BINARY']).is_file(): raise RuntimeError('missing at init')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("TDNEXT_TEST_BINARY", str(binary))
    manifest = manager.manifests["files"].model_copy(update={
        "id": "requires-binary", "entrypoint": "requires_binary_at_import:RequiredPlugin",
        "requires_plugins": (), "requires_executables": (binary.name,),
    })
    manager.manifests = {**manager.manifests, manifest.id: manifest}

    def install(name: str, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    monkeypatch.setattr("tdnext.core.manager.install_executable", install)
    manager.enable(manifest.id)
    assert manifest.id in manager.store.read().enabled
    assert os.access(binary, os.X_OK)


def test_failed_constructor_after_executable_install_does_not_commit_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, manager = make_app(tmp_path)
    binary = "tdnext-fixture-binary"
    manifest = manager.manifests["icons"].model_copy(update={
        "entrypoint": f"{__name__}:RaisingProvider", "requires_executables": (binary,),
    })
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    before = manager.store.read()

    def install(name: str, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    monkeypatch.setattr("tdnext.core.manager.install_executable", install)
    with pytest.raises(PluginError, match="provider failed to initialize"):
        manager.enable(manifest.id)
    assert manager.store.read() == before
