from pathlib import Path

import pytest
from textual.widgets import Static
from typer.testing import CliRunner

from tdnext import launcher, pane
from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.plugin_code import InstalledCode
from tdnext.core.session import SessionSnapshot, current_session, session_token
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.tool import ToolPane


class IdentityView:
    shortcuts = ""
    seen: list[str | None] = []

    def make_view(self, context: PluginContext) -> Static:
        self.seen.append(context.view_id)
        return Static("fixture")

    def run_pane(self, context: PluginContext) -> None:
        self.seen.append(context.view_id)


class RecordingHost:
    def run(self, context: PluginContext, _views: list[tuple[PluginManifest, object]]) -> None:
        assert context.session is not None
        (context.directory / "session.json").write_text(context.session.model_dump_json())


def manager_at(path: Path) -> PluginManager:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    return PluginManager(discover(roots), StateStore(path / "plugins.json"), path / "bin")


def test_launch_snapshot_includes_enabled_host_service_and_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = manager_at(tmp_path)
    manager.manifests = {**manager.manifests, "desktop": manager.manifests["desktop"].model_copy(
        update={"entrypoint": f"{__name__}:RecordingHost"}
    )}
    stub = InstalledCode(package="snapshot_stub", path=Path(__file__), generation="a" * 32, is_package=False)
    manager.manifests = {
        plugin_id: manifest.model_copy(update={"source_dir": None, "code": stub})
        for plugin_id, manifest in manager.manifests.items()
    }
    enabled = frozenset({"desktop", "icons", "files"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    monkeypatch.setattr(launcher, "manager", lambda: manager)

    launcher.launch(tmp_path)

    snapshot = SessionSnapshot.model_validate_json((tmp_path / "session.json").read_text())
    assert {manifest.id for manifest in snapshot.participants} == enabled
    assert current_session(session_token(snapshot)) == snapshot


def test_missing_unselected_plugin_is_reported_without_changing_state(tmp_path: Path) -> None:
    manager = manager_at(tmp_path)
    manager.manifests = {key: value for key, value in manager.manifests.items() if key != "icons"}
    manager.manifests = {
        plugin_id: manifest.model_copy(update={"source_dir": None})
        for plugin_id, manifest in manager.manifests.items()
    }
    enabled = frozenset({"desktop", "icons"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))

    with pytest.raises(PluginError, match="tdnext disable icons"):
        launcher.enabled_views(manager)
    assert manager.store.read().enabled == enabled


@pytest.mark.anyio
async def test_fallback_and_header_pane_scope_view_identity(tmp_path: Path) -> None:
    manager = manager_at(tmp_path)
    manifest = manager.manifests["files"].model_copy(update={"id": "custom-view"})
    plugin = IdentityView()
    plugin.seen.clear()
    context = PluginContext(tmp_path, manager)
    async with DesktopApp(context, [(manifest, plugin)]).run_test():
        assert plugin.seen == ["custom-view"]
    async with ToolPane("custom-view", plugin, context).run_test():
        assert plugin.seen == ["custom-view", "custom-view"]
    assert context.view_id is None


def test_native_view_pane_scopes_view_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = manager_at(tmp_path)
    manager.install("files")
    manifest = manager.manifests["files"].model_copy(update={
        "id": "custom-view", "entrypoint": f"{__name__}:IdentityView",
    })
    snapshot = SessionSnapshot.model_validate({
        "views": [{"manifest": manifest, "icon": "[]"}],
        "protected_plugins": [],
        "view_dependencies": {},
        "participants": [manifest],
    })
    IdentityView.seen.clear()
    monkeypatch.setattr(pane, "manager", lambda: manager)
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))

    result = CliRunner().invoke(pane.cli, ["view", "custom-view", "--snapshot", session_token(snapshot)])

    assert result.exit_code == 0, result.output
    assert IdentityView.seen == ["custom-view"]
