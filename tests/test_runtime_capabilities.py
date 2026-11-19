from pathlib import Path

import pytest

from tdnext import launcher
from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.session import SessionSnapshot, SessionView
from tdnext.core.state import StateStore
from tdnext.plugins.settings.operations import OperationResult, apply_operation


class RecordingHost:
    def run(self, context: PluginContext, _views: list[tuple[PluginManifest, object]]) -> None:
        assert context.session is not None
        (context.directory / "snapshot.json").write_text(context.session.model_dump_json())


@pytest.mark.parametrize("with_session", [True, False])
@pytest.mark.parametrize("target", ["preferences", "foundation"])
def test_replacement_settings_and_dependencies_remain_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_session: bool, target: str
) -> None:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    manifests["desktop"] = manifests["desktop"].model_copy(update={"entrypoint": f"{__name__}:RecordingHost"})
    manifests["foundation"] = manifests["icons"].model_copy(update={"id": "foundation"})
    manifests["preferences"] = manifests.pop("settings").model_copy(update={
        "id": "preferences", "requires_plugins": ("foundation",),
    })
    manifests = {
        plugin_id: manifest.model_copy(update={"source_dir": None})
        for plugin_id, manifest in manifests.items()
    }
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    enabled = frozenset({"desktop", "preferences", "foundation"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    launcher.launch(tmp_path)
    session = SessionSnapshot.model_validate_json((tmp_path / "snapshot.json").read_text()) if with_session else None
    manager.disable("foundation")
    before = manager.store.read()
    called: list[str] = []

    def uninstall_locally(managed: PluginManager, plugin_id: str) -> OperationResult:
        called.append(plugin_id)
        return OperationResult("uninstall", plugin_id, ())

    result = apply_operation(
        manager, "uninstall", target, session=session, view_id="preferences",
        uninstall_runner=uninstall_locally,
    )
    assert result == OperationResult("uninstall", target, ())
    assert called == [target]
    assert manager.store.read() == before


@pytest.mark.parametrize("view_id", ["preferences", "configuration"])
@pytest.mark.parametrize("snapshot_mode", ["none", "current"])
def test_only_running_view_and_its_dependencies_are_protected(
    tmp_path: Path, view_id: str, snapshot_mode: str
) -> None:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    active = manifests["settings"].model_copy(update={
        "id": view_id, "capabilities": ("desktop.view", "example.manage"), "requires_plugins": ("foundation",),
    })
    other = manifests["settings"].model_copy(update={"id": "other-settings"})
    foundation = manifests["icons"].model_copy(update={"id": "foundation"})
    manifests.update({view_id: active, other.id: other, foundation.id: foundation})
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    enabled = frozenset({"desktop", view_id, other.id, foundation.id})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    session = None
    if snapshot_mode != "none":
        session = SessionSnapshot(
            views=(SessionView(manifest=active, icon="[]"),),
            protected_plugins=frozenset({"desktop"}),
            view_dependencies={view_id: frozenset({view_id, "foundation"})},
            participants=(active, foundation),
        )

    called: list[str] = []

    def uninstall_locally(managed: PluginManager, plugin_id: str) -> OperationResult:
        called.append(plugin_id)
        return OperationResult("uninstall", plugin_id, ())

    for target in (view_id, "foundation", "desktop"):
        result = apply_operation(
            manager, "uninstall", target, session=session, view_id=view_id,
            uninstall_runner=uninstall_locally,
        )
        assert result == OperationResult("uninstall", target, ())
    assert called == [view_id, "foundation", "desktop"]
    apply_operation(manager, "disable", other.id, session=session, view_id=view_id)
    assert other.id not in manager.store.read().enabled
    assert {view_id, "foundation", "desktop"} <= manager.store.read().enabled


def test_view_can_declare_additional_desktop_capability(tmp_path: Path) -> None:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifest = discover(roots)["files"]
    extended = PluginManifest.model_validate({
        **manifest.model_dump(), "capabilities": (*manifest.capabilities, "desktop.extra")
    })
    manager = PluginManager({extended.id: extended}, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")

    assert manager.manifests[extended.id].role == "desktop.view"
