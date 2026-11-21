import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tdnext import pane
from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def default_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginContext:
    state = tmp_path / "state"
    manifests = discover((Path(__file__).parents[1] / "src" / "tdnext" / "plugins",))
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": (), "dependency_hook": None}
    )
    store = StateStore(state / "plugins.json")
    ids = frozenset({"desktop", "files", "monitor"})
    store.write(PluginState(installed=ids, enabled=ids))
    plugins = PluginManager(manifests, store, state / "bin")
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    return PluginContext(tmp_path, plugins)


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
