import subprocess
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def footer_screen(session: NativeSession, tool: str = "") -> str:
    def screen() -> str:
        capture = subprocess.run(
            ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
            env=session.env, capture_output=True, text=True, check=False, timeout=5,
        )
        text = capture.stdout
        lines = text.splitlines()
        current = next((line for line in lines if "Tool (" in line), "")
        bottom = "\n".join(lines[-3:])
        if "Global:" in bottom and (not tool or f"Tool ({tool}):" in current):
            return f"{current}\n{bottom}"
        return ""

    return wait_for(screen)


def test_native_pty_footer_follows_files_monitor_and_settings(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        files = footer_screen(session)
        assert "Tool (Files):" in files and "Enter open" in files
        assert all(key in files for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))

        session.switch("Monitor")
        monitor = footer_screen(session, "Monitor")
        assert "Tool (Monitor):" in monitor and "btop: q quit" in monitor
        assert all(key in monitor for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))

        session.open_settings()
        settings = footer_screen(session, "Settings")
        assert "Tool (Settings):" in settings and "Esc back" in settings
        assert all(key in settings for key in ("Global:", "Ctrl+q", "Ctrl+p", "Alt+o", "Alt+1..2"))
        session.back_to("Monitor")
        assert "Tool (Monitor):" in footer_screen(session, "Monitor")
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
        screen = footer_screen(session)
        assert "Tool (Files):" in screen and "Global:" in screen
        assert "Alt+1 view" in screen and "Ctrl+p" in screen and "Ctrl+q" in screen
        assert "Alt+o" not in screen and "Alt+1..9" not in screen
    finally:
        session.close()
