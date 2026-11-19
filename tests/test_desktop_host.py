import json
import os
import shutil
import subprocess
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tdnext import launcher, pane
from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest
from tdnext.core.session import SessionSnapshot, SessionView
from tdnext.core.state import StateStore
from tdnext.plugins.desktop import panes as desktop_panes
from tdnext.plugins.desktop import plugin as desktop_plugin
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
    manifests["desktop"] = manifests["desktop"].model_copy(
        update={"entrypoint": f"{__name__}:SubstituteDesktop", "source_dir": None}
    )
    store = StateStore(state / "plugins.json")
    plugins = PluginManager(manifests, store, state / "bin")
    plugins.enable("files")
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
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": (), "dependency_hook": None}
    )
    store = StateStore(state / "plugins.json")
    plugins = PluginManager(manifests, store, state / "bin")
    plugins.enable("files")
    plugins.enable("monitor")
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    return PluginContext(tmp_path, plugins)


def test_bundled_host_passes_shared_chrome_and_single_pane_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [
        (context.manager.manifests["settings"], FilesPlugin()),
        (context.manager.manifests["files"], FilesPlugin()),
        (context.manager.manifests["monitor"], MonitorPlugin()),
    ]
    session = SessionSnapshot(
        views=tuple(SessionView(manifest=manifest, icon="X") for manifest, _ in views),
        protected_plugins=frozenset(), view_dependencies={}, participants=tuple(manifest for manifest, _ in views),
    )
    context = replace(context, session=session)
    launched: list[tuple[list[str], dict[str, str], str, list[dict[str, str]]]] = []
    fallback: list[bool] = []

    def run_zellij(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert "--layout-string" not in command
        layout_path = Path(command[command.index("--new-session-with-layout") + 1])
        assert layout_path.name == "layout.kdl"
        chrome_path = Path(env["TDNEXT_CHROME_FILE"])
        assert chrome_path.parent == Path(command[command.index("--config-dir") + 1])
        chrome = json.loads(chrome_path.read_text(encoding="utf-8"))
        launched.append((command, env, layout_path.read_text(encoding="utf-8"), chrome))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: Path("/fake/tdnext"))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(
        import_module("tdnext.plugins.desktop.app").DesktopApp, "run", lambda self: fallback.append(True)
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, views)

    assert fallback == []
    assert len(launched) == 1
    command, env, layout, chrome = launched[0]
    assert command[0] == "/fake/tdnext"
    assert 'args "-m" "tdnext.pane" "view" "files"' in layout
    assert 'args "-m" "tdnext.pane" "view" "monitor"' in layout
    assert 'args "-m" "tdnext.pane" "view" "settings"' in layout
    assert layout.count("pane ") == 3
    assert layout.count('"--snapshot"') == 3
    assert chrome == [
        {"id": manifest.id, "name": manifest.name, "icon": "X", "shortcuts": plugin.shortcuts,
         "placement": manifest.placement,
         "tab_name": f"tdnext-header:{manifest.id}" if manifest.placement == "header" else manifest.name}
        for manifest, plugin in [*views[1:], views[0]]
    ]
    assert Path(command[command.index("--config") + 1]).name == "zellij.kdl"
    assert env["TDNEXT_DIRECTORY"] == str(tmp_path)
    assert env["TDNEXT_ZELLIJ"] == "/fake/tdnext"


def test_empty_native_desktop_uses_empty_chrome_without_textual_panes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = default_context(tmp_path, monkeypatch)
    captured: list[tuple[str, str]] = []

    def run_zellij(command: list[str], *, env: dict[str, str], check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        layout = Path(command[command.index("--new-session-with-layout") + 1]).read_text(encoding="utf-8")
        chrome = Path(env["TDNEXT_CHROME_FILE"]).read_text(encoding="utf-8")
        captured.append((layout, chrome))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: Path("/fake/tdnext"))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, [])

    layout, chrome = captured[0]
    assert json.loads(chrome) == []
    assert "tdnext.plugins.desktop.panes" not in layout
    assert "signal.pause()" in layout


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

    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: Path("/fake/tdnext"))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(
        import_module("tdnext.plugins.desktop.app").DesktopApp, "run",
        lambda self: fallback.append(self.context.directory),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, views)

    assert fallback == [tmp_path]


@pytest.mark.parametrize("condition", ["narrow", "short", "nested"])
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

    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: Path("/fake/tdnext"))
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size(size))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setattr(
        import_module("tdnext.plugins.desktop.app").DesktopApp, "run",
        lambda self: fallback.append(self.context.directory),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)
    if condition == "nested":
        monkeypatch.setenv("ZELLIJ", "1")

    DesktopPlugin().run(context, views)

    assert fallback == [tmp_path]
    assert launched == []


def test_bundled_host_refuses_start_when_core_missing_or_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tdnext.core.errors import PluginError

    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [(context.manager.manifests["files"], FilesPlugin())]
    fallback: list[Path] = []
    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: None)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(
        import_module("tdnext.plugins.desktop.app").DesktopApp, "run",
        lambda self: fallback.append(self.context.directory),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    with pytest.raises(PluginError, match="tdnext executable missing or unusable"):
        DesktopPlugin().run(context, views)

    assert fallback == []


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
