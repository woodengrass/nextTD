import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_processes import tagged_processes, terminate_tagged
from native_session import NativeSession, wait_for

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


@pytest.fixture
def isolated_native(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> tuple[dict[str, str], str]:
    base, binary = native_env
    excluded = {
        "BASH_ENV", "ENV", "TMUX", "TMUX_PANE", "TDNEXT_SESSION_SNAPSHOT", "TDNEXT_PATH_BOOTSTRAPPED",
        "TDNEXT_ORIGINAL_PATH", "TDNEXT_ORIGINAL_PATH_PRESENT",
    }
    env = {key: value for key, value in base.items() if key not in excluded}
    home = tmp_path / "home"
    home.mkdir()
    env.update({
        "HOME": str(home),
        "HISTFILE": str(home / "history"),
        "INPUTRC": "/dev/null",
        "SHELL": "/bin/bash",
        "TDNEXT_TEST_EXPECTED_PATH": env["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env, binary


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


def actions_finished(session: NativeSession) -> bool:
    for _pid, _environ, command in tagged_processes(session.tmux_name):
        if b"open-header" in command or b"tdnext.plugins.desktop.shortcut" in command:
            return False
        if len(command) > 1 and Path(os.fsdecode(command[0])).name == "zellij" and command[1] == b"action":
            return False
    return True


def close_session(session: NativeSession) -> None:
    try:
        session.stop_tmux()
    finally:
        terminate_tagged(session.tmux_name)


def open_shortcut(session: NativeSession) -> None:
    session.send_key("M-o")
    visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
    wait_for(lambda: actions_finished(session))


def test_cold_start_opens_settings_without_a_permission_prompt(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        tabs = session.tabs()
        header = next(tab for tab in tabs if str(tab["name"]).startswith("tdnext-header:settings"))
        assert str(header["name"]) == "tdnext-header:settings"
    finally:
        close_session(session)


def test_shortcut_returns_to_shell_process_and_uses_original_session_snapshot(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    ids = frozenset({"desktop", "files", "monitor", "settings", "shell"})
    store = StateStore(tmp_path / "state" / "plugins.json")
    store.write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=4)
    try:
        visible(session, "Tool (Files)")
        btop_pid = wait_for(session.btop_pid)
        session.send_key("M-3")
        visible(session, "Tool (Shell)")
        session.send_key("printf 'BEFORE_PID_%s\\n' \"$$\"")
        session.send_key("Enter")
        pid_before = wait_for(lambda: re.search(r"BEFORE_PID_(\d+)", capture(session))).group(1)

        store.write(PluginState(installed=ids, enabled=ids - {"settings"}))
        open_shortcut(session)
        session.send_key("Escape")
        visible(session, "Tool (Shell)")
        wait_for(lambda: actions_finished(session))
        session.send_key(
            "printf 'AFTER_PID_%s\\nSNAP_%s\\nBOOT_%s\\nPATH_%s\\n' \"$$\" "
            "\"${TDNEXT_SESSION_SNAPSHOT-absent}\" \"${TDNEXT_PATH_BOOTSTRAPPED-absent}\" "
            '"$([ "$PATH" = "$TDNEXT_TEST_EXPECTED_PATH" ] && printf same || printf changed)"'
        )
        session.send_key("Enter")
        after = visible(session, "SNAP_absent", "BOOT_absent", "PATH_same")
        pid_after = re.search(r"AFTER_PID_(\d+)", after)
        assert pid_after is not None
        assert pid_after.group(1) == pid_before
        assert session.btop_pid() == btop_pid
    finally:
        close_session(session)


def test_disabled_header_shortcut_does_not_open_a_phantom_action(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    ids = frozenset({"desktop", "files", "monitor"})
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=2)
    try:
        visible(session, "Tool (Files)")
        btop_pid = wait_for(session.btop_pid)

        session.send_key("M-o")
        session.send_key("f")

        visible(session, "New file name")
        assert session.btop_pid() == btop_pid
    finally:
        close_session(session)


def test_arbitrary_native_pane_keeps_user_path_and_receives_no_snapshot(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        code = (
            "import os; print('PANE_SNAPSHOT_'+os.environ.get('TDNEXT_SESSION_SNAPSHOT','absent')); "
            "print('PANE_PATH_'+('same' if os.environ.get('PATH')==os.environ['TDNEXT_TEST_EXPECTED_PATH'] "
            "else 'changed'), flush=True); input()"
        )

        session.action("new-pane", "--floating", "--", sys.executable, "-u", "-c", code)

        visible(session, "PANE_SNAPSHOT_absent", "PANE_PATH_same")
    finally:
        close_session(session)


def test_header_click_returns_focus_to_files(tmp_path: Path, isolated_native: tuple[dict[str, str], str]) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)", "Settings")

        session.click_header()
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        wait_for(lambda: actions_finished(session))
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))
        session.send_key("f")

        visible(session, "New file name")
    finally:
        close_session(session)


@pytest.mark.parametrize("via_shortcut", [True, False], ids=["keyboard", "header-click"])
def test_header_returns_to_the_exact_floating_pane(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str], via_shortcut: bool
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)", "Settings")
        command = (
            "print('ORIGIN_READY', flush=True); answer=input(); "
            "print('ORIGIN_REPLY:'+answer, flush=True); input()"
        )
        pane_id = session.action("new-pane", "--floating", "--", sys.executable, "-u", "-c", command).strip()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        pane = next(item for item in panes if f"terminal_{item['id']}" == pane_id and not item["is_plugin"])
        visible(session, "ORIGIN_READY")
        session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=False)
        session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=True)
        wait_for(lambda: pane_id in session.action("list-clients"))

        if via_shortcut:
            open_shortcut(session)
        else:
            session.click_header()
            visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
            wait_for(lambda: actions_finished(session))
        tabs = session.tabs()
        header = next(tab for tab in tabs if str(tab["name"]).startswith("tdnext-header:settings"))
        assert str(header["name"]) == "tdnext-header:settings"
        session.send_key("Escape")
        visible(session, "ORIGIN_READY")
        wait_for(lambda: actions_finished(session))
        session.send_key("proof")
        session.send_key("Enter")

        visible(session, "ORIGIN_REPLY:proof")
    finally:
        close_session(session)


def test_locked_mode_still_passes_shortcut_through(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")

        session.send_key("C-g")
        session.send_key("M-o")
        session.send_key("f")
        visible(session, "New file name")
        session.send_key("Escape")
        wait_for(lambda: "New file name" not in capture(session))
        session.send_key("C-g")
        open_shortcut(session)
    finally:
        close_session(session)


def test_independent_sessions_retain_their_own_return_destinations(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    first = NativeSession(tmp_path, env, binary)
    try:
        second = NativeSession(tmp_path, env, binary)
        try:
            visible(first, "Tool (Files)")
            second.send_key("M-2")
            visible(second, "Tool (Monitor)")
            first_btop, second_btop = wait_for(first.btop_pid), wait_for(second.btop_pid)

            open_shortcut(first)
            open_shortcut(second)
            first.send_key("Escape")
            visible(first, "Tool (Files)")
            wait_for(lambda: actions_finished(first))
            first.send_key("f")
            visible(first, "New file name")
            second.send_key("Escape")
            visible(second, "Tool (Monitor)")

            assert first.btop_pid() == first_btop
            assert second.btop_pid() == second_btop
        finally:
            close_session(second)
    finally:
        close_session(first)
