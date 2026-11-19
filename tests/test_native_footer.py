"""Stage-3 footer coverage: the core draws the footer with the old labels.

The footer is outer chrome with no PTY, so every assertion reads the
bottom terminal rows of the visible capture instead of dumping a dead
``panes footer`` pane. Labels stay the ones the host rendered before.
"""

from pathlib import Path

from native_environment import native_env as native_env
from native_session import (
    NativeSession,
    assert_no_chrome_processes,
    assert_no_chrome_ptys,
    chrome_footer_text,
    wait_for,
)

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def footer(session: NativeSession, *expected: str) -> str:
    def screen() -> str | None:
        text = chrome_footer_text(session)
        return text if all(part in text for part in expected) else None

    try:
        return wait_for(screen, timeout=20)
    except AssertionError as exc:
        raise AssertionError(
            f"Expected {expected!r} in the chrome footer; footer:\n{chrome_footer_text(session)}"
        ) from exc


def test_native_pty_footer_follows_files_monitor_and_settings(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        files = footer(session, "Tool (Files):", "Enter open")
        assert "Global:" in files and "Ctrl+q" in files and "Alt+o" in files and "Alt+1..2" in files

        session.switch("Monitor")
        monitor = footer(session, "Tool (Monitor):", "btop: q quit")
        assert "Global:" in monitor and "Ctrl+q" in monitor and "Alt+o" in monitor and "Alt+1..2" in monitor

        session.open_settings()
        settings = footer(session, "Tool (Settings):", "Esc Back")
        assert "Global:" in settings and "Ctrl+q" in settings and "Alt+o" in settings and "Alt+1..2" in settings
        session.back_to("Monitor")
        assert "Tool (Monitor):" in footer(session, "Tool (Monitor):")
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
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
        screen = footer(session, "Tool (Files):", "Global:")
        assert "Alt+1 view" in screen and "Ctrl+p" in screen and "Ctrl+q" in screen
        assert "Alt+o" not in screen and "Alt+1..9" not in screen
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
    finally:
        session.close()
