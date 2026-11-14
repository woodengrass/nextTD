import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_desktop_host import default_context

from tdnext.api import ViewPlugin
from tdnext.core.models import PluginManifest
from tdnext.core.session import SESSION_ENV, SessionSnapshot, SessionView, session_token
from tdnext.plugins.desktop import plugin as desktop_plugin
from tdnext.plugins.desktop.plugin import DesktopPlugin
from tdnext.plugins.files.plugin import FilesPlugin


def capture_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header_ids: tuple[str, ...], *, compatible: bool = True
) -> tuple[str, str, dict[str, str], str]:
    context = default_context(tmp_path, monkeypatch)
    manifests = context.manager.manifests
    views: list[tuple[PluginManifest, ViewPlugin]] = [(manifests["files"], FilesPlugin())]
    views.extend(
        (manifests["settings"].model_copy(update={"id": plugin_id, "name": plugin_id}), FilesPlugin())
        for plugin_id in header_ids
    )
    session = SessionSnapshot(views=tuple(SessionView(manifest=manifest, icon="[]") for manifest, _ in views))
    context = replace(context, session=session)
    captured: list[tuple[str, str, dict[str, str]]] = []

    def run_zellij(command: list[str], *, env: dict[str, str], check: bool) -> subprocess.CompletedProcess[str]:
        assert check is False
        config = Path(command[command.index("--config") + 1]).read_text(encoding="utf-8")
        captured.append((config, command[command.index("--layout-string") + 1], env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(context.manager, "executable", lambda _name: "/fake/zellij")
    monkeypatch.setattr(desktop_plugin, "broker_host_compatible", lambda _zellij: compatible)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda _fallback: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setenv(SESSION_ENV, "inherited-session-must-not-leak")
    monkeypatch.delenv("ZELLIJ", raising=False)

    DesktopPlugin().run(context, views)

    config, layout, env = captured[0]
    return config, layout, env, session_token(session)


def test_shortcut_targets_first_enabled_header_without_session_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, layout, env, snapshot = capture_launch(tmp_path, monkeypatch, ("preferences", "settings"))

    run_line = next(line for line in config.splitlines() if line.strip().startswith("Run "))
    command = shlex.split(run_line.partition("{")[0])

    assert command[0:3] == ["Run", sys.executable, "-m"]
    assert command[-1] == "preferences"
    assert "--snapshot" not in command
    assert snapshot not in config
    assert snapshot in layout
    assert SESSION_ENV not in env
    assert layout.count("navigator-broker.wasm") == 1
    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env


def test_shortcut_falls_back_to_python_run_without_broker_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tdnext.plugins.desktop import plugin as desktop_plugin

    monkeypatch.setattr(desktop_plugin, "broker_asset_path", lambda: None)
    config, layout, env, snapshot = capture_launch(tmp_path, monkeypatch, ("preferences", "settings"))

    run_line = next(line for line in config.splitlines() if line.strip().startswith("Run "))
    command = shlex.split(run_line.partition("{")[0])

    assert command[0:3] == ["Run", sys.executable, "-m"]
    assert command[-1] == "preferences"
    assert "--snapshot" not in command
    assert snapshot not in config
    assert snapshot in layout
    assert SESSION_ENV not in env
    assert "TDNEXT_NAVIGATOR_BROKER" not in env
    assert "TDNEXT_NAVIGATOR_HEADER" not in env


def test_shortcut_has_no_binding_when_no_header_is_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _layout, _env, _snapshot = capture_launch(tmp_path, monkeypatch, ())

    bindings = [line.strip() for line in config.splitlines()]

    assert 'unbind "Alt o"' in bindings
    assert 'bind "Alt o" {' not in bindings


def test_incompatible_host_keeps_native_shortcut_without_loading_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, layout, env, _snapshot = capture_launch(tmp_path, monkeypatch, ("settings",), compatible=False)
    assert 'bind "Alt o"' in config
    assert "navigator-broker.wasm" not in layout
    assert "TDNEXT_NAVIGATOR_BROKER" not in env


@pytest.mark.parametrize(
    ("output", "returncode", "compatible"),
    [("zellij 0.45.1\n", 0, True), ("zellij 0.45.2\n", 0, False),
     ("zellij 0.45.1-dev\n", 0, False), ("", 0, False), ("zellij 0.45.1\n", 1, False)],
)
def test_broker_host_version_gate(
    monkeypatch: pytest.MonkeyPatch, output: str, returncode: int, compatible: bool
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, output)
    )
    assert desktop_plugin.broker_host_compatible("/fake/zellij") is compatible


@pytest.mark.parametrize("error", [OSError("missing"), subprocess.TimeoutExpired("zellij", 5)])
def test_broker_host_version_failure_skips_broker(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert not desktop_plugin.broker_host_compatible("/fake/zellij")


def test_shortcut_entrypoint_forwards_target_without_loading_ui_or_modifying_environment() -> None:
    script = """
import json, os, runpy, sys
from tdnext.plugins.desktop import navigation
original = dict(os.environ)
result = {}
def open_header(target):
    result['target'] = target
    result['environment_unchanged'] = dict(os.environ) == original
    result['heavy_modules'] = sorted(name for name in sys.modules if name == 'textual' or name.startswith('textual.')
        or name == 'typer' or name.startswith('typer.') or name == 'tdnext.launcher'
        or name == 'tdnext.plugins.desktop.panes' or name.startswith('tdnext.plugins.settings.')
        or name.startswith('tdnext.plugins.files.'))
navigation.open_header = open_header
sys.argv = ['tdnext.plugins.desktop.shortcut', 'preferences']
runpy.run_module('tdnext.plugins.desktop.shortcut', run_name='__main__')
print(json.dumps(result))
"""
    env = {**os.environ, SESSION_ENV: "unchanged-sentinel", "PYTHONDONTWRITEBYTECODE": "1"}

    process = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)

    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"target": "preferences", "environment_unchanged": True, "heavy_modules": []}


def test_shortcut_entrypoint_surfaces_navigation_errors() -> None:
    env = {key: value for key, value in os.environ.items() if key != "TDNEXT_ZELLIJ"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    process = subprocess.run(
        [sys.executable, "-m", "tdnext.plugins.desktop.shortcut", "settings"],
        env=env, capture_output=True, text=True, check=False,
    )

    assert process.returncode == 1
    assert "Cannot control Zellij" in process.stderr
