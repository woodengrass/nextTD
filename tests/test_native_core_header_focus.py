import json
import subprocess
import sys
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for
from test_native_core_navigation import capture, core_flag_active
from test_native_core_navigation import core_native as core_native


def wait_for_native_header(session: NativeSession) -> None:
    def header() -> bool:
        capture_text = subprocess.run(
            ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
            env=session.env, capture_output=True, text=True, check=False, timeout=5,
        ).stdout
        return "Settings [Alt+o]" in capture_text

    wait_for(header)


def open_floating(session: NativeSession) -> str:
    pane_id = session.action("new-pane", "--floating", "--", sys.executable, "-c", "input()").strip()
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(item for item in panes if f"terminal_{item['id']}" == pane_id and not item["is_plugin"])
    session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=False)
    session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=True)
    wait_for(lambda: pane_id in session.action("list-clients"))
    return pane_id


@pytest.mark.parametrize("floating", [False, True], ids=["tiled", "floating"])
def test_core_rapid_blank_then_settings_returns_to_exact_pane(
    tmp_path: Path, core_native: tuple[dict[str, str], str], floating: bool
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        wait_for(lambda: core_flag_active(session))
        session.switch("Files")
        wait_for_native_header(session)
        if floating:
            open_floating(session)
        origin = session.action("list-clients")
        gestures = b"\x1b[<0;10;1M\x1b[<0;10;1m\x1b[<0;110;1M\x1b[<0;110;1m"
        subprocess.run(
            ["tmux", "-S", session.tmux_socket, "send-keys", "-t", session.tmux_name, "-H",
             *[f"{byte:02x}" for byte in gestures]],
            env=env, capture_output=True, check=True, timeout=5,
        )
        session.wait_for_settings()
        session.back_to("Files")
        wait_for(lambda: session.action("list-clients") == origin)
    finally:
        session.close()


def test_core_blank_header_returns_focus_to_floating_origin(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        wait_for_native_header(session)
        floating = open_floating(session)
        session.header_mouse(10, release=False)
        session.header_mouse(10, release=True)
        wait_for(lambda: floating in session.action("list-clients"))
        assert session.active_tab() == session.tab_id("Files")
    finally:
        session.close()


@pytest.mark.parametrize("x", [10, 110], ids=["blank", "settings"])
def test_core_active_header_preserves_dialog_and_restores_tool_focus(
    tmp_path: Path, core_native: tuple[dict[str, str], str], x: int
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        session.open_settings()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(pane for pane in panes if "panes tool settings" in (pane.get("pane_command") or ""))
        tool_id = f"terminal_{tool['id']}"
        wait_for(lambda: tool_id in session.action("list-clients"))
        session.send_key("Enter")
        wait_for(lambda: capture(session).count("None (text)") >= 2)
        session.header_mouse(x, release=False)
        session.header_mouse(x, release=True)
        wait_for(lambda: tool_id in session.action("list-clients"))
        session.send_key("Escape")
        wait_for(lambda: capture(session).count("None (text)") == 1)
        assert session.active_tab() == session.tab_id("tdnext-header:settings")
        session.back_to("Files")
    finally:
        session.close()
