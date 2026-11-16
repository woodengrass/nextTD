from pathlib import Path

import pytest
from pydantic import ValidationError

from tdnext import launcher
from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.settings.operations import apply_operation


class RecordingDesktop:
    def run(self, context: PluginContext, views: list[tuple[PluginManifest, ViewPlugin]]) -> None:
        (context.directory / "views.txt").write_text(
            ",".join(manifest.id for manifest, _plugin in views), encoding="utf-8"
        )


def make_manager(tmp_path: Path) -> PluginManager:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    manifests["desktop"] = manifests["desktop"].model_copy(update={"entrypoint": f"{__name__}:RecordingDesktop"})
    return PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")


def test_manifest_placement_defaults_to_sidebar_and_validates_header(tmp_path: Path) -> None:
    manifests = make_manager(tmp_path).manifests
    assert manifests["files"].placement == "sidebar"
    assert manifests["settings"].placement == "header"
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({**manifests["files"].model_dump(), "placement": "footer"})


def test_fresh_launch_requires_explicit_host_without_writing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    with pytest.raises(PluginError, match="Desktop is disabled"):
        launcher.launch(tmp_path)
    assert not manager.store.path.exists()
    assert not (tmp_path / "views.txt").exists()


def test_enabled_host_runs_without_views_and_preserves_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path)
    manager.enable("desktop")
    before = manager.store.path.read_bytes()
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    launcher.launch(tmp_path)
    assert (tmp_path / "views.txt").read_text(encoding="utf-8") == ""
    assert manager.store.path.read_bytes() == before


def test_multiple_enabled_hosts_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path)
    other = manager.manifests["desktop"].model_copy(update={"id": "other-host"})
    manager.manifests = {**manager.manifests, other.id: other}
    manager.enable("desktop")
    manager.store.write(PluginState(
        installed=frozenset({"desktop", other.id}), enabled=frozenset({"desktop", other.id})
    ))
    before = manager.store.path.read_bytes()
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    with pytest.raises(PluginError, match="Exactly one desktop host"):
        launcher.launch(tmp_path)
    assert manager.store.path.read_bytes() == before


def test_existing_disabled_settings_remains_disabled_on_repeated_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    before = PluginState(installed=frozenset({"desktop", "files", "settings"}), enabled=frozenset({"desktop", "files"}))
    manager.store.write(before)
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    launcher.launch(tmp_path)
    launcher.launch(tmp_path)
    assert manager.store.read() == before
    assert (tmp_path / "views.txt").read_text(encoding="utf-8") == "files"


def test_settings_lifecycle_is_independent_and_requires_desktop(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    assert manager.enable("settings") == ("desktop", "settings")
    assert manager.disable("settings") == frozenset({"settings"})
    assert manager.store.read() == PluginState(
        installed=frozenset({"desktop", "settings"}), enabled=frozenset({"desktop"})
    )


@pytest.mark.parametrize("target", ["desktop", "settings"])
@pytest.mark.parametrize("operation", ["disable", "uninstall"])
def test_settings_operations_protect_running_components(
    tmp_path: Path, target: str, operation: str
) -> None:
    manager = make_manager(tmp_path)
    manager.enable("settings")
    before = manager.store.read()
    with pytest.raises(PluginError, match="cannot be disabled"):
        if operation == "disable":
            apply_operation(manager, "disable", target)
        else:
            apply_operation(manager, "uninstall", target)
    assert manager.store.read() == before


def test_settings_operation_rejects_cascade_to_running_desktop(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manifests = dict(manager.manifests)
    manifests["base"] = manifests["files"].model_copy(update={"id": "base", "requires_plugins": ()})
    manifests["desktop"] = manifests["desktop"].model_copy(update={"requires_plugins": ("base",)})
    manager.manifests = manifests
    manager.enable("settings")
    before = manager.store.read()
    with pytest.raises(PluginError, match="cannot be disabled"):
        apply_operation(manager, "disable", "base")
    assert manager.store.read() == before
