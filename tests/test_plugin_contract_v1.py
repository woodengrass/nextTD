import base64
import json
from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import DataTable
from typer.testing import CliRunner

from tdnext import cli, launcher
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.session import current_session
from tdnext.core.state import StateStore
from tdnext.plugins.settings.view import SettingsView

BUILTINS = Path(__file__).parents[1] / "src" / "tdnext" / "plugins"


def break_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace curl with a failing script: installs run their staged copy's
    real installer, so a guard that fails the test must sit at the download
    itself rather than on the live source module the copy was staged from."""
    curl = tmp_path / "failing-bin" / "curl"
    curl.parent.mkdir()
    curl.write_text("#!/bin/sh\necho mocked curl failure >&2\nexit 1\n", encoding="utf-8")
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", str(curl.parent))


class FixtureBtop:
    """Test-local tool hook for a synthetic monitor double: fakes the btop
    installation hermetically instead of patching the real hook class, which a
    staged managed copy would not observe."""

    @staticmethod
    def check(manager: PluginManager) -> str | None:
        return None if manager.executable("btop") else "Install fixture btop"

    @staticmethod
    def install(manager: PluginManager) -> Path:
        manager.local_bin.mkdir(parents=True, exist_ok=True)
        path = manager.local_bin / "btop"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path


class FixtureMonitor:
    """Test-local view plugin: the double's entrypoint must not live under a
    stageable top-level package, since a staged install from an earlier test
    can leave that package name pointing at a deleted temp dir in sys.modules."""

    def __init__(self) -> None:
        pass


def synthetic_monitor(manager: PluginManager) -> None:
    """Swap the real monitor for a synthetic double with a hermetic hook."""
    manifests = dict(manager.manifests)
    manifests["monitor"] = manifests["monitor"].model_copy(update={
        "entrypoint": f"{__name__}:FixtureMonitor",
        "dependency_hook": f"{__name__}:FixtureBtop", "source_dir": None,
    })
    manager.manifests = manifests


def test_duplicate_manifests_quarantine_every_conflicting_entry(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "plugin.toml").write_text((BUILTINS / "files" / "plugin.toml").read_text())
    manifests = discover((BUILTINS, tmp_path))
    assert "files" not in manifests
    assert "files" in manifests.errors
    assert "desktop" in manifests


def test_enabled_conflicting_plugin_stops_launch_and_is_reported_by_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = tmp_path / "duplicate"
    duplicate.mkdir()
    (duplicate / "plugin.toml").write_text((BUILTINS / "files" / "plugin.toml").read_text())
    manager = PluginManager(discover((BUILTINS, tmp_path)), StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    manager.enable("desktop")
    manager.store.write(PluginState(
        installed=frozenset({"desktop", "files"}), enabled=frozenset({"desktop", "files"})
    ))
    monkeypatch.setattr(cli, "manager", lambda: manager)
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    assert "Duplicate plugin id files" in CliRunner().invoke(cli.cli, ["list"]).output
    with pytest.raises(PluginError, match="Duplicate plugin id files"):
        launcher.launch(tmp_path)


def test_ambiguous_host_requires_selection_without_writing_state(tmp_path: Path) -> None:
    manifests = discover((BUILTINS,))
    manifests["other"] = manifests["desktop"].model_copy(update={"id": "other"})
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    with pytest.raises(PluginError, match="compatible host"):
        manager.enable("files")
    assert not manager.store.path.exists()


def test_enabled_incompatible_host_does_not_enable_view(tmp_path: Path) -> None:
    manifests = discover((BUILTINS,))
    manifests["desktop"] = manifests["desktop"].model_copy(update={"host_interfaces": ("other.v1",)})
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    manager.enable("desktop")
    before = manager.store.read()
    with pytest.raises(PluginError, match="incompatible"):
        manager.enable("files")
    assert manager.store.read() == before


def test_enabled_compatible_host_is_used_when_other_hosts_exist(tmp_path: Path) -> None:
    manifests = discover((BUILTINS,))
    manifests["other"] = manifests["desktop"].model_copy(update={"id": "other"})
    manager = PluginManager(manifests, StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    manager.enable("other")
    manager.enable("files")
    assert manager.store.read().enabled == frozenset({"other", "files"})


def test_old_native_snapshot_without_new_manifest_fields_is_rejected() -> None:
    old_manifest = {
        "id": "files", "name": "Files", "version": "1", "description": "files",
        "entrypoint": "tdnext.plugins.files.plugin:FilesPlugin", "capabilities": ["desktop.view"],
    }
    raw = json.dumps({"views": [{"manifest": old_manifest, "icon": "[]"}]}).encode()
    token = base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(PluginError, match="Invalid desktop session snapshot"):
        current_session(token)


def test_cli_refusal_never_installs_or_changes_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = PluginManager(discover((BUILTINS,)), StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    monkeypatch.setattr(cli, "manager", lambda: manager)
    monkeypatch.setattr(manager, "executable", lambda _name: None)
    break_downloads(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.cli, ["install", "monitor"], input="n\n")
    assert result.exit_code != 0
    assert not manager.store.path.exists()
    assert not manager.local_bin.exists()


def test_cli_consent_installs_and_enables_monitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = PluginManager(discover((BUILTINS,)), StateStore(tmp_path / "plugins.json"), tmp_path / "bin")
    monkeypatch.setattr(cli, "manager", lambda: manager)
    monkeypatch.setattr(manager, "executable", lambda name: str(manager.local_bin / name)
                        if (manager.local_bin / name).exists() else None)
    synthetic_monitor(manager)
    manager.enable("desktop")
    result = CliRunner().invoke(cli.cli, ["enable", "monitor"], input="y\n")
    assert result.exit_code == 0
    assert manager.store.read().enabled == frozenset({"desktop", "monitor"})
    assert (manager.local_bin / "btop").is_file()


@pytest.mark.anyio
async def test_settings_refusal_never_installs_or_changes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = make_app(tmp_path)
    before = manager.store.read()
    manager.manifests = {**manager.manifests, "monitor": manager.manifests["monitor"].model_copy(
        update={"dependency_hook": "tdnext.plugins.monitor.dependencies:MonitorDependencies"}
    )}
    monkeypatch.setattr(manager, "executable", lambda _name: None)
    break_downloads(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.click("#plugin-install")
        await app.workers.wait_for_complete()
        await pilot.click("#cancel")
        assert manager.store.read() == before
        assert not manager.local_bin.exists()


@pytest.mark.anyio
async def test_settings_consent_installs_monitor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, manager = make_app(tmp_path)
    synthetic_monitor(manager)
    monkeypatch.setattr(manager, "executable", lambda name: str(manager.local_bin / name)
                        if (manager.local_bin / name).exists() else None)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.click("#plugin-install")
        await app.workers.wait_for_complete()
        await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        assert "monitor" in manager.store.read().installed
        assert (manager.local_bin / "btop").is_file()
