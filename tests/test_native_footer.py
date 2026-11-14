import json
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def footer_screen(session: NativeSession, view: str) -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(item for item in panes if f"panes footer {view}" in (item.get("pane_command") or ""))
    pane_id = f"terminal_{pane['id']}"
    return wait_for(
        lambda: screen if "Global:" in (screen := session.action("dump-screen", "--pane-id", pane_id)) else ""
    )


def test_native_pty_footer_follows_files_monitor_and_settings(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        files = footer_screen(session, "files")
        assert "Tool (Files):" in files and "Enter open" in files
        assert all(key in files for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))

        session.switch("Monitor")
        monitor = footer_screen(session, "monitor")
        assert "Tool (Monitor):" in monitor and "btop: q quit" in monitor
        assert all(key in monitor for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))

        session.open_settings()
        settings = footer_screen(session, "settings")
        assert "Tool (Settings):" in settings and "Esc Back" in settings
        assert all(key in settings for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))
        session.back_to("Monitor")
        assert "Tool (Monitor):" in footer_screen(session, "monitor")
    finally:
        session.close()


def test_native_pty_footer_omits_disabled_header_shortcut(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    ids = frozenset({"desktop", "files"})
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=1)
    try:
        screen = footer_screen(session, "files")
        assert "Tool (Files):" in screen and "Global:" in screen
        assert "Alt+1 view" in screen and "Ctrl+p" in screen and "Ctrl+q" in screen
        assert "Alt+o" not in screen and "Alt+1..9" not in screen
    finally:
        session.close()
