import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tdnext import launcher, pane
from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.desktop import panes as desktop_panes
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.plugin import DesktopPlugin
from tdnext.plugins.files.plugin import FilesPlugin
from tdnext.plugins.monitor.plugin import MonitorPlugin


class SubstituteDesktop:
    def run(self, context: PluginContext, views: list[tuple[PluginManifest, ViewPlugin]]) -> None:
        (context.directory / "host-result").write_text(
            f"{context.directory}\n" + "\n".join(manifest.id for manifest, _ in views), encoding="utf-8"
        )


@pytest.mark.parametrize("size", [(120, 36), (80, 24)], ids=["native-sized", "fallback-sized"])
def test_configured_desktop_controls_launch_at_any_terminal_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    manifests = discover((Path(__file__).parents[1] / "src" / "tdnext" / "plugins",))
    manifests["desktop"] = manifests["desktop"].model_copy(update={"entrypoint": f"{__name__}:SubstituteDesktop"})
    store = StateStore(state / "plugins.json")
    store.write(PluginState(installed=frozenset({"desktop", "files"}), enabled=frozenset({"desktop", "files"})))
    plugins = PluginManager(manifests, store, state / "bin")
    monkeypatch.setattr(launcher, "manager", lambda: plugins)
    monkeypatch.setattr(plugins, "executable", lambda name: "/fake/zellij" if name == "zellij" else None)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size(size))
    launched: list[list[str]] = []

    def record_native_launch(command: list[str], **_kwargs: dict[str, str] | bool) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", record_native_launch)
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.delenv("ZELLIJ", raising=False)

    launcher.launch(workspace)

    assert (workspace / "host-result").read_text(encoding="utf-8") == f"{workspace}\nfiles"
    assert launched == []


def default_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginContext:
    state = tmp_path / "state"
    manifests = discover((Path(__file__).parents[1] / "src" / "tdnext" / "plugins",))
    manifests["monitor"] = manifests["monitor"].model_copy(update={"requires_executables": ()})
    store = StateStore(state / "plugins.json")
    ids = frozenset({"desktop", "files", "monitor"})
    store.write(PluginState(installed=ids, enabled=ids))
    plugins = PluginManager(manifests, store, state / "bin")
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    return PluginContext(tmp_path, plugins)


def test_bundled_host_builds_native_layout_with_view_rail_and_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [
        (context.manager.manifests["files"], FilesPlugin()),
        (context.manager.manifests["monitor"], MonitorPlugin()),
    ]
    launched: list[tuple[list[str], dict[str, str]]] = []
    fallback: list[bool] = []

    def run_zellij(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        launched.append((command, env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(context.manager, "executable", lambda name: "/fake/zellij")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(DesktopApp, "run", lambda self: fallback.append(True))
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, views)

    assert fallback == []
    assert len(launched) == 1
    command, env = launched[0]
    layout = command[command.index("--layout-string") + 1]
    assert 'args "-m" "tdnext.plugins.desktop.panes" "sidebar" "monitor"' in layout
    assert 'args "-m" "tdnext.pane" "view" "monitor"' in layout
    assert 'args "-m" "tdnext.plugins.desktop.panes" "footer" "monitor"' in layout
    assert Path(command[command.index("--config") + 1]).name == "zellij.kdl"
    assert env["TDNEXT_DIRECTORY"] == str(tmp_path)
    assert env["TDNEXT_ZELLIJ"] == "/fake/zellij"


@pytest.mark.parametrize("failure", ["exit", "oserror"])
def test_bundled_host_falls_back_when_native_launch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [(context.manager.manifests["files"], FilesPlugin())]
    fallback: list[Path] = []

    def run_zellij(command: list[str], **_kwargs: dict[str, str] | bool) -> subprocess.CompletedProcess[str]:
        if failure == "oserror":
            raise OSError("cannot start")
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(context.manager, "executable", lambda name: "/fake/zellij")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(DesktopApp, "run", lambda self: fallback.append(self.context.directory))
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, views)

    assert fallback == [tmp_path]


@pytest.mark.parametrize("condition", ["narrow", "short", "nested", "missing"])
def test_bundled_host_uses_fullscreen_when_native_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [(context.manager.manifests["files"], FilesPlugin())]
    fallback: list[Path] = []
    launched: list[list[str]] = []
    sizes = {"narrow": (103, 36), "short": (120, 27)}
    size = sizes.get(condition, (120, 36))

    def run_zellij(command: list[str], **_kwargs: dict[str, str] | bool) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(context.manager, "executable", lambda name: None if condition == "missing" else "/fake/zellij")
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size(size))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(DesktopApp, "run", lambda self: fallback.append(self.context.directory))
    monkeypatch.delenv("ZELLIJ", raising=False)
    if condition == "nested":
        monkeypatch.setenv("ZELLIJ", "1")

    DesktopPlugin().run(context, views)

    assert fallback == [tmp_path]
    assert launched == []


def test_native_view_command_executes_real_monitor_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = default_context(tmp_path, monkeypatch)
    calls: list[tuple[str, list[str], dict[str, str]]] = []

    def exec_btop(binary: str, args: list[str], env: dict[str, str]) -> None:
        calls.append((binary, args, env))

    monkeypatch.setattr(pane, "manager", lambda: context.manager)
    monkeypatch.setattr(context.manager, "executable", lambda name: "/fake/btop")
    monkeypatch.setattr(os, "execve", exec_btop)
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))

    result = CliRunner().invoke(pane.cli, ["view", "monitor"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    binary, args, env = calls[0]
    assert (binary, args) == ("/fake/btop", ["/fake/btop"])
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "state" / "btop" / "config")


def test_bundled_native_footer_shows_active_view_shortcuts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = default_context(tmp_path, monkeypatch)
    shortcuts: list[str] = []
    monkeypatch.setattr(desktop_panes, "manager", lambda: context.manager)
    monkeypatch.setattr(desktop_panes.FooterPane, "run", lambda self: shortcuts.append(self.shortcuts))

    result = CliRunner().invoke(desktop_panes.cli, ["footer", "monitor"])

    assert result.exit_code == 0, result.output
    assert shortcuts == [MonitorPlugin.shortcuts]
