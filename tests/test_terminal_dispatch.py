from pathlib import Path
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from tdnext import launcher, pane
from tdnext.api import ViewPlugin
from tdnext.context import PluginContext
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.settings.plugin import SettingsPane
from tdnext.plugins.settings.view import SettingsView


class Target:
    def run_pane(self, context: PluginContext) -> None:
        (context.directory / "ran").write_text(context.view_id or "")


class Forbidden:
    def __init__(self) -> None:
        raise AssertionError("unrelated view was constructed")


class Host:
    def run(self, context: PluginContext, views: list[tuple[PluginManifest, object]]) -> None:
        plugin = views[0][1]
        assert isinstance(plugin, ViewPlugin)
        (context.directory / "shortcuts").write_text(plugin.shortcuts)


def manifest(plugin_id: str, entrypoint: str, role: str) -> PluginManifest:
    return PluginManifest(
        id=plugin_id, name=plugin_id, version="1", description=plugin_id, entrypoint=entrypoint,
        manifest_version=1, api_version=1, capabilities=(role,), shortcuts="hint",
        host_interfaces=("tdnext.textual-view.v1",) if role == "desktop.host" else (),
        view_interfaces=("tdnext.terminal-view.v1",) if role == "desktop.view" else (),
    )


def test_pane_runs_only_selected_terminal_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = manifest("target", f"{__name__}:Target", "desktop.view")
    other = manifest("other", f"{__name__}:Forbidden", "desktop.view")
    plugins = PluginManager({m.id: m for m in (target, other)}, StateStore(tmp_path / "state"), tmp_path / "bin")
    plugins.store.write(PluginState(installed=frozenset({"target", "other"}), enabled=frozenset({"target", "other"})))
    monkeypatch.setattr(pane, "manager", lambda: plugins)
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))

    result = CliRunner().invoke(pane.cli, ["view", "target"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "ran").read_text() == "target"


def test_launch_passes_shortcuts_without_constructing_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = manifest("host", f"{__name__}:Host", "desktop.host")
    view = manifest("other", f"{__name__}:Forbidden", "desktop.view")
    plugins = PluginManager({m.id: m for m in (host, view)}, StateStore(tmp_path / "state"), tmp_path / "bin")
    plugins.store.write(PluginState(installed=frozenset({"host", "other"}), enabled=frozenset({"host", "other"})))
    monkeypatch.setattr(launcher, "manager", lambda: plugins)

    launcher.launch(tmp_path)

    assert (tmp_path / "shortcuts").read_text() == "hint"


@pytest.mark.anyio
async def test_settings_back_keeps_pane_alive_and_focus_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugins = PluginManager({}, StateStore(tmp_path / "state"), tmp_path / "bin")
    app = SettingsPane(PluginContext(tmp_path, plugins))
    calls: list[str] = []
    monkeypatch.setenv("TDNEXT_ZELLIJ", "tdnext")
    monkeypatch.setattr("tdnext.plugins.settings.plugin.subprocess.run", lambda *_a, **_k: CompletedProcess([], 0))
    monkeypatch.setattr(SettingsView, "on_native_activation", lambda _self: calls.append("focus"))

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("escape")
        assert app.is_running
        from textual.events import AppFocus

        app.on_app_focus(AppFocus())
        assert calls == ["focus"]
