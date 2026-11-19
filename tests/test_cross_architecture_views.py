import base64
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from typer.testing import CliRunner

from tdnext import launcher, pane
from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.session import SessionSnapshot, SessionView, current_session, session_token
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.plugin import DesktopPlugin
from tdnext.plugins.files.plugin import FilesPlugin


@runtime_checkable
class OtherViewProtocol(Protocol):
    def render_other(self) -> str: ...


class OtherView:
    def render_other(self) -> str:
        return "other architecture rendered"


class OtherHost:
    def run(self, context: PluginContext, views: list[tuple[PluginManifest, object]]) -> None:
        for manifest, view in views:
            if not isinstance(view, OtherViewProtocol):
                raise PluginError(f"{manifest.id} lacks the other view interface")
            (context.directory / "rendered.txt").write_text(view.render_other(), encoding="utf-8")


class ForbiddenConstructor:
    def __init__(self) -> None:
        raise AssertionError("incompatible view was constructed")


def make_manager(tmp_path: Path, view_entrypoint: str, interface: str = "example.other.v1") -> PluginManager:
    host = PluginManifest(
        id="other-host", name="Other host", version="1", description="Other host",
        entrypoint=f"{__name__}:OtherHost", manifest_version=1, api_version=1,
        capabilities=("desktop.host",),
        host_interfaces=("example.other.v1",),
    )
    view = PluginManifest(
        id="other-view", name="Other view", version="1", description="Other view",
        entrypoint=view_entrypoint, manifest_version=1, api_version=1,
        capabilities=("desktop.view",), view_interfaces=(interface,),
    )
    return PluginManager({host.id: host, view.id: view}, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")


def test_non_textual_host_enables_and_launches_its_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path, f"{__name__}:OtherView")
    manager.enable("other-view")
    monkeypatch.setattr(launcher, "manager", lambda: manager)

    launcher.launch(tmp_path)

    assert manager.store.read().enabled == frozenset({"other-host", "other-view"})
    assert (tmp_path / "rendered.txt").read_text(encoding="utf-8") == "other architecture rendered"


def test_incompatible_view_is_rejected_before_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path, f"{__name__}:ForbiddenConstructor", "example.incompatible.v1")
    manager.enable("other-host")
    before = manager.store.read()
    with pytest.raises(PluginError, match="incompatible"):
        manager.enable("other-view")
    assert manager.store.read() == before

    enabled = frozenset({"other-host", "other-view"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    with pytest.raises(PluginError, match="incompatible"):
        launcher.launch(tmp_path)


@pytest.mark.parametrize("broken", [OtherView(), type("BrokenView", (), {
    "shortcuts": "", "make_view": None, "run_pane": lambda self, context: None,
})()])
def test_textual_host_reports_invalid_view_contract(tmp_path: Path, broken: object) -> None:
    manager = make_manager(tmp_path, f"{__name__}:OtherView")
    manifest = manager.manifests["other-view"]
    with pytest.raises(PluginError, match="other-view.*Textual view plugin contract"):
        DesktopPlugin().run(PluginContext(tmp_path, manager), [(manifest, broken)])


def test_launcher_reports_textual_host_contract_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path, f"{__name__}:OtherView", "tdnext.textual-view.v1")
    manager.manifests = {
        **manager.manifests,
        "other-host": manager.manifests["other-host"].model_copy(update={
            "entrypoint": "tdnext.plugins.desktop.plugin:DesktopPlugin",
            "host_interfaces": ("tdnext.textual-view.v1",),
        }),
    }
    manager.enable("other-view")
    monkeypatch.setattr(launcher, "manager", lambda: manager)

    with pytest.raises(PluginError, match="other-view.*Textual view plugin contract"):
        launcher.launch(tmp_path)


def test_snapshot_runs_native_view_pane_without_current_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    installer = PluginManager(discover(roots), StateStore(tmp_path / "install-state.json"), tmp_path / "install-bin")
    installer.install("files")
    manifest = installer.manifests["files"]
    snapshot = SessionSnapshot(
        views=(SessionView(manifest=manifest, icon="[]"),),
        protected_plugins=frozenset(),
        view_dependencies={},
        participants=(manifest,),
    )
    token = session_token(snapshot)
    manager = PluginManager({}, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    monkeypatch.setattr(pane, "manager", lambda: manager)
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))

    from importlib import import_module

    module_name, _, class_name = manifest.entrypoint.partition(":")
    managed_plugin = getattr(import_module(module_name), class_name)

    def run_pane(_plugin: object, context: PluginContext) -> None:
        assert context.session is not None
        (context.directory / "pane.txt").write_text(context.session.icon_for("files") or "", encoding="utf-8")

    monkeypatch.setattr(managed_plugin, "run_pane", run_pane)
    result = CliRunner().invoke(pane.cli, ["view", "files", "--snapshot", token])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "pane.txt").read_text(encoding="utf-8") == "[]"


def test_serialized_snapshot_without_managed_code_is_rejected() -> None:
    manifest = {
        "id": "files", "name": "Files", "version": "1", "description": "files",
        "entrypoint": "tdnext.plugins.files.plugin:FilesPlugin",
        "manifest_version": 1, "api_version": 1, "capabilities": ["desktop.view"],
    }
    view_snapshot = SessionSnapshot.model_validate({
        "views": [{"manifest": manifest, "icon": "[]"}],
        "protected_plugins": [],
        "view_dependencies": {},
        "participants": [],
    })
    participant_snapshot = SessionSnapshot.model_validate({
        "views": [],
        "protected_plugins": [],
        "view_dependencies": {},
        "participants": [manifest],
    })
    for snapshot in (view_snapshot, participant_snapshot):
        with pytest.raises(PluginError, match="no managed code"):
            current_session(session_token(snapshot))


def test_old_snapshot_is_rejected_by_native_view_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_manifest = {
        "id": "files", "name": "Files", "version": "1", "description": "files",
        "entrypoint": "tdnext.plugins.files.plugin:FilesPlugin", "capabilities": ["desktop.view"],
    }
    raw = json.dumps({"views": [{"manifest": old_manifest, "icon": "[]"}]}).encode()
    token = base64.urlsafe_b64encode(raw).decode()
    manager = PluginManager({}, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    monkeypatch.setattr(pane, "manager", lambda: manager)
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))

    def forbidden_run_pane(_plugin: FilesPlugin, _context: PluginContext) -> None:
        pytest.fail("old snapshot must not dispatch to the view")

    monkeypatch.setattr(FilesPlugin, "run_pane", forbidden_run_pane)
    result = CliRunner().invoke(pane.cli, ["view", "files", "--snapshot", token])
    assert result.exit_code != 0
    assert isinstance(result.exception, PluginError)
    assert "Invalid desktop session snapshot" in str(result.exception)
    assert not (tmp_path / "pane.txt").exists()
