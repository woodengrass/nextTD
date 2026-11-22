import subprocess
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for
from test_native_core_navigation import core_native as core_native
from test_native_header_regressions import files_pane


def capture(session: NativeSession) -> str:
    return subprocess.run(
        ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
        env=session.env, capture_output=True, text=True, check=True, timeout=5,
    ).stdout


def visible(session: NativeSession, *expected: str) -> str:
    def screen() -> str | None:
        text = capture(session)
        return text if all(part in text for part in expected) else None

    try:
        return wait_for(screen, timeout=20)
    except AssertionError as exc:
        raise AssertionError(f"Expected {expected!r} on the terminal; screen:\n{capture(session)}") from exc


def test_core_locked_mode_passes_shortcut_to_focused_pane(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        pane_id = files_pane(session)
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane_id))
        visible(session, "Tool (Files)")
        session.send_key("C-g")
        session.send_key("M-o")
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane_id))
        visible(session, "New file name")
        assert session.active_tab() == session.tab_id("Files")
        session.send_key("Escape")
        wait_for(lambda: "New file name" not in session.action("dump-screen", "--pane-id", pane_id))
        wait_for(lambda: "New file name" not in capture(session))
        session.send_key("C-g")
        session.open_settings()
        visible(session, "Settings", "Back")
        session.back_to("Files")
        visible(session, "Tool (Files)")
    finally:
        session.close()
