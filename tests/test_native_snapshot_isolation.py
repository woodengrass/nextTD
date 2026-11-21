import sys
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.session import SESSION_ENV


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


def test_running_session_keeps_tabs_when_state_disables_monitor(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    """State changes apply on restart, not to the running desktop.

    Survives from the old snapshot-isolation contract: with no snapshot
    token passed to content panes, the running session keeps its own tabs
    (including Monitor and its btop) after ``monitor`` leaves the enabled
    set, instead of following the parent state mid-session.
    """
    from tdnext.core.models import PluginState
    from tdnext.core.state import StateStore

    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        store = StateStore(Path(env["TDNEXT_STATE_DIR"]) / "plugins.json")
        state = store.read()
        store.write(PluginState(installed=state.installed, enabled=state.enabled - {"monitor"}))
        assert session.tab_id("Monitor") != session.tab_id("Files")
        assert session.btop_pid() == monitor
    finally:
        session.close()
