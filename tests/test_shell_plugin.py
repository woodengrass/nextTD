import os
from pathlib import Path

import pytest

from tdnext import launcher
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.shell.launch import exec_shell


def test_shell_executes_the_configured_shell_in_the_requested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace ; $(not-a-command)"
    workspace.mkdir()
    shell = "/tmp/a shell;not a command"
    calls: list[tuple[str, list[str], str]] = []
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setattr(os, "chdir", lambda path: calls.append(("chdir", [], str(path))))

    def record_exec(binary: str, args: list[str], _env: os._Environ[str]) -> None:
        calls.append((binary, args, "exec"))

    monkeypatch.setattr(os, "execvpe", record_exec)
    exec_shell(workspace)
    assert calls == [("chdir", [], str(workspace)), (shell, [shell, "-i"], "exec")]


def test_shell_lifecycle_is_independent_and_depends_on_desktop(tmp_path: Path) -> None:
    root = Path(__file__).parents[1] / "src" / "tdnext" / "plugins"
    manifests = discover((root,))
    store = StateStore(tmp_path / "plugins.json")
    plugins = PluginManager(manifests, store, tmp_path / "bin")
    assert store.read() == PluginState()
    assert plugins.enable("shell") == ("desktop", "shell")
    assert [manifest.id for manifest, _ in launcher.enabled_views(plugins)] == ["shell"]
    assert plugins.disable("shell") == frozenset({"shell"})
    assert store.read() == PluginState(installed=frozenset({"desktop", "shell"}), enabled=frozenset({"desktop"}))
    assert plugins.uninstall("shell") == frozenset()
    assert store.read() == PluginState(installed=frozenset({"desktop"}), enabled=frozenset({"desktop"}))
