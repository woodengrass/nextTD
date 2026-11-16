from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from textual.content import Content
from textual.widgets import Button, DataTable
from typer.testing import CliRunner

from tdnext import cli, launcher
from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.errors import InstallationConsentRequired, PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.session import SessionSnapshot, SessionView, current_session, session_token
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.monitor import dependencies as monitor_dependencies
from tdnext.plugins.settings.operations import apply_operation
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView

BUILTINS = Path(__file__).parents[1] / "src" / "tdnext" / "plugins"


class ConstructorMustNotRun:
    def __init__(self) -> None:
        raise RuntimeError("incompatible plugin was constructed")


class RecordingHost:
    def run(self, context: PluginContext, _views: list[tuple[PluginManifest, ViewPlugin]]) -> None:
        assert context.session is not None
        (context.directory / "snapshot.json").write_text(context.session.model_dump_json(), encoding="utf-8")


class FixtureInstaller:
    @staticmethod
    def check(manager: PluginManager) -> str | None:
        return None if (manager.local_bin / "installed").exists() else "Install fixture tool"

    @staticmethod
    def install(manager: PluginManager) -> Path:
        manager.local_bin.mkdir(parents=True, exist_ok=True)
        marker = manager.local_bin / "installed"
        marker.write_text("installed", encoding="utf-8")
        return marker


def make_manager(tmp_path: Path) -> PluginManager:
    return PluginManager(discover((BUILTINS,)), StateStore(tmp_path / "plugins.json"), tmp_path / "bin")


def test_cli_consent_identifies_verified_download_and_denial_has_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    monkeypatch.setattr(cli, "manager", lambda: manager)
    monkeypatch.setattr(manager, "executable", lambda _name: None)

    def forbidden(*_args: object) -> Path:
        pytest.fail("installer ran without consent")

    monkeypatch.setattr("tdnext.plugins.monitor.dependencies.install_release", forbidden)
    result = CliRunner().invoke(cli.cli, ["install", "monitor"], input="n\n")
    assert "https://github.com/aristocratos/btop/releases/download/v1.4.8/" in result.output
    assert str(tmp_path / "bin" / "btop") in result.output
    assert "af589cf82cc9ea9c29ce050b9239a13bef2d1492256d476156e1054a42ce89ad" in result.output
    assert result.exit_code != 0
    assert not manager.local_bin.exists()
    assert not manager.store.path.exists()


def test_consent_for_one_destination_does_not_approve_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    monkeypatch.setattr(manager, "executable", lambda _name: None)
    def forbidden(*_args: object) -> Path:
        pytest.fail("installer ran with stale consent")

    monkeypatch.setattr("tdnext.plugins.monitor.dependencies.install_release", forbidden)
    with pytest.raises(InstallationConsentRequired) as original:
        manager.install("monitor")
    manager.local_bin = tmp_path / "other-bin"
    with pytest.raises(InstallationConsentRequired):
        manager.install("monitor", approved=original.value.actions)
    assert not manager.local_bin.exists()
    assert not manager.store.path.exists()


@pytest.mark.parametrize("field", ["url", "sha256", "member"])
def test_changed_release_requires_new_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    manager = make_manager(tmp_path)
    monkeypatch.setattr(manager, "executable", lambda _name: None)
    with pytest.raises(InstallationConsentRequired) as original:
        manager.install("monitor")
    release = monitor_dependencies.BTOP_RELEASE
    monkeypatch.setattr(monitor_dependencies, "BTOP_RELEASE", replace(release, **{field: "changed"}))

    def forbidden(*_args: object) -> Path:
        pytest.fail("installer ran with consent for a different release")

    monkeypatch.setattr(monitor_dependencies, "install_release", forbidden)
    with pytest.raises(InstallationConsentRequired):
        manager.install("monitor", approved=original.value.actions)
    assert not manager.local_bin.exists()
    assert not manager.store.path.exists()


@pytest.mark.anyio
async def test_settings_displays_install_details_and_denial_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = make_manager(tmp_path)
    enabled = frozenset({"desktop", "settings"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    before = manager.store.path.read_bytes()
    monkeypatch.setattr(manager, "executable", lambda _name: None)

    def forbidden(*_args: object) -> Path:
        pytest.fail("installer ran without consent")

    monkeypatch.setattr(monitor_dependencies, "install_release", forbidden)
    app = DesktopApp(PluginContext(tmp_path, manager), [(manager.manifests["settings"], SettingsPlugin())])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.click("#plugin-install")
        await app.workers.wait_for_complete()
        content = list(app.screen.query("#plugin-confirmation-message Static"))[-1].render()
        assert isinstance(content, Content)
        assert monitor_dependencies.BTOP_RELEASE.url in content.plain
        assert monitor_dependencies.BTOP_RELEASE.sha256 in content.plain
        assert str(manager.local_bin / "btop") in content.plain
        await pilot.click("#cancel")
    assert manager.store.path.read_bytes() == before
    assert not manager.local_bin.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["other", "icons"])
async def test_settings_protects_running_host_after_external_disable(tmp_path: Path, target: str) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {**manager.manifests, "other": manager.manifests["desktop"].model_copy(
        update={"id": "other", "requires_plugins": ("icons",)}
    )}
    manager.store.write(PluginState(
        installed=frozenset({"other", "icons", "settings"}), enabled=frozenset({"settings"})
    ))
    session = SessionSnapshot(views=(), protected_plugins=frozenset({"other", "icons"}))
    context = PluginContext(tmp_path, manager, session)
    app = DesktopApp(context, [(manager.manifests["settings"], SettingsPlugin())])
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index(target))
        view.refresh_selection(manager.store.read())
        assert view.query_one("#plugin-uninstall", Button).disabled


@pytest.mark.parametrize("target", ["other", "icons"])
def test_launch_snapshot_protects_running_host_after_external_disable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {**manager.manifests, "other": manager.manifests["desktop"].model_copy(update={
        "id": "other", "entrypoint": f"{__name__}:RecordingHost", "requires_plugins": ("icons",),
    })}
    enabled = frozenset({"other", "icons", "settings"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    launcher.launch(tmp_path)
    snapshot = SessionSnapshot.model_validate_json((tmp_path / "snapshot.json").read_text(encoding="utf-8"))
    restored = current_session(session_token(snapshot))
    manager.disable("icons")
    before = manager.store.read()
    with pytest.raises(PluginError, match="cannot be disabled"):
        apply_operation(manager, "uninstall", target, session=restored)
    assert manager.store.read() == before


def test_incompatible_view_rejected_before_any_plugin_construction(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {
        **manager.manifests,
        "desktop": manager.manifests["desktop"].model_copy(update={
            "entrypoint": f"{__name__}:ConstructorMustNotRun",
        }),
        "files": manager.manifests["files"].model_copy(update={"view_interfaces": ("other.v1",)}),
    }
    manager.store.write(PluginState(
        installed=frozenset({"desktop", "files"}), enabled=frozenset({"desktop", "files"})
    ))
    with pytest.raises(PluginError, match="Enabled view files is incompatible"):
        launcher.enabled_views(manager)


@pytest.mark.parametrize("operation", ["disable", "uninstall"])
def test_host_dependency_cascades_to_views_and_their_dependants(
    tmp_path: Path, operation: Literal["disable", "uninstall"]
) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {
        **manager.manifests,
        "other": manager.manifests["desktop"].model_copy(update={"id": "other", "requires_plugins": ("icons",)}),
        "shell": manager.manifests["shell"].model_copy(update={"requires_plugins": ("files",)}),
    }
    enabled = frozenset({"icons", "other", "files", "shell"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    affected = getattr(manager, operation)("icons")
    assert affected == enabled
    assert manager.store.read().enabled == frozenset()


@pytest.mark.parametrize("operation", ["disable", "uninstall"])
def test_settings_protects_alternative_host_dependency(
    tmp_path: Path, operation: Literal["disable", "uninstall"]
) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {**manager.manifests, "other": manager.manifests["desktop"].model_copy(
        update={"id": "other", "requires_plugins": ("icons",)}
    )}
    enabled = frozenset({"icons", "other", "settings"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    with pytest.raises(PluginError, match="cannot be disabled"):
        apply_operation(manager, operation, "icons")
    assert manager.store.read().enabled == enabled


def test_missing_manual_dependency_blocks_all_installations(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {
        **manager.manifests,
        "icons": manager.manifests["icons"].model_copy(update={"dependency_hook": f"{__name__}:FixtureInstaller"}),
        "files": manager.manifests["files"].model_copy(update={
            "requires_plugins": ("icons",), "requires_executables": ("tdnext-missing-invariant-tool",),
        }),
    }
    with pytest.raises(PluginError, match="tdnext-missing-invariant-tool"):
        manager.install("files", approved=(("icons", "Install fixture tool"),))
    assert not manager.local_bin.exists()
    assert not manager.store.path.exists()


def test_partial_consent_never_starts_installation(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    hook = f"{__name__}:FixtureInstaller"
    manager.manifests = {
        **manager.manifests,
        "icons": manager.manifests["icons"].model_copy(update={"dependency_hook": hook}),
        "files": manager.manifests["files"].model_copy(update={
            "requires_plugins": ("icons",), "dependency_hook": hook,
        }),
    }
    with pytest.raises(InstallationConsentRequired) as error:
        manager.install("files", approved=(("icons", "Install fixture tool"),))
    assert {plugin_id for plugin_id, _ in error.value.actions} == {"icons", "files"}
    assert not manager.local_bin.exists()
    assert not manager.store.path.exists()


def test_disabling_an_already_disabled_host_keeps_active_views(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {**manager.manifests, "other": manager.manifests["desktop"].model_copy(update={"id": "other"})}
    manager.store.write(PluginState(
        installed=frozenset({"desktop", "other", "files"}), enabled=frozenset({"other", "files"})
    ))
    assert manager.disable("desktop") == frozenset()
    assert manager.store.read().enabled == frozenset({"other", "files"})


def test_removing_missing_host_does_not_leave_enabled_views(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    enabled = frozenset({"missing-host", "files"})
    manager.store.write(PluginState(installed=enabled, enabled=enabled))
    assert manager.disable("missing-host") == enabled
    assert manager.store.read().enabled == frozenset()


def test_view_without_declared_interface_still_requires_a_host(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.manifests = {"files": manager.manifests["files"].model_copy(update={"view_interfaces": ()})}
    with pytest.raises(PluginError, match="host"):
        manager.enable("files")
    assert not manager.store.path.exists()


def test_snapshot_views_do_not_depend_on_current_host_or_state(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    snapshot = SessionSnapshot(views=(SessionView(manifest=manager.manifests["files"], icon="[]"),))
    manager.manifests = {}
    assert [manifest.id for manifest, _ in launcher.enabled_views(manager, snapshot)] == ["files"]
