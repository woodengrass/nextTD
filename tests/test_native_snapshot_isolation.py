import os
import sys
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for
from test_settings_lifecycle import make_manager
from test_settings_ui import make_app

from tdnext import launcher
from tdnext.core.models import PluginState
from tdnext.core.session import SESSION_ENV
from tdnext.core.state import StateStore
from tdnext.launcher import enabled_views


def test_public_launch_ignores_and_removes_inherited_legacy_snapshot(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    monkeypatch.setattr(launcher, "manager", lambda: manager)
    monkeypatch.setenv(SESSION_ENV, "not-a-valid-session")
    launcher.launch(tmp_path)
    assert (tmp_path / "views.txt").read_text(encoding="utf-8") == "files,settings"
    assert SESSION_ENV not in os.environ


def test_general_view_discovery_does_not_consume_an_ambient_session(tmp_path: Path, monkeypatch) -> None:
    _app, manager = make_app(tmp_path)
    monkeypatch.setenv(SESSION_ENV, "not-a-valid-session")
    assert [manifest.id for manifest, _ in enabled_views(manager)] == ["files", "settings"]


def test_ordinary_new_zellij_pane_does_not_inherit_desktop_snapshot(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        code = f"import os; print('SNAPSHOT=' + ('present' if {SESSION_ENV!r} in os.environ else 'absent')); input()"
        pane = session.action("new-pane", "--floating", "--", sys.executable, "-c", code).strip()
        screen = wait_for(lambda: (text if "SNAPSHOT=" in (text := session.action(
            "dump-screen", "--pane-id", pane
        )) else ""))
        assert "SNAPSHOT=absent" in screen
    finally:
        session.close()


def test_nested_public_launch_uses_current_plugin_state_not_parent_snapshot(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        store = StateStore(Path(env["TDNEXT_STATE_DIR"]) / "plugins.json")
        state = store.read()
        store.write(PluginState(installed=state.installed, enabled=state.enabled - {"monitor"}))
        pane = session.action(
            "new-pane", "--floating", "--", sys.executable, "-m", "tdnext", "run", str(tmp_path)
        ).strip()
        screen = wait_for(lambda: (text if "WORKSPACE" in (text := session.action(
            "dump-screen", "--pane-id", pane
        )) else ""))
        assert "Files" in screen
        assert "Monitor" not in screen
        assert session.btop_pid() == monitor
        assert session.tab_id("Monitor") != session.tab_id("Files")
    finally:
        session.close()
