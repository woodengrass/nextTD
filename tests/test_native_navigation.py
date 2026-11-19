import fcntl
import json
import os
import pty
import struct
import subprocess
import sys
import termios
from pathlib import Path

from native_environment import native_env as native_env
from native_session import (
    NativeSession,
    assert_no_chrome_processes,
    assert_no_chrome_ptys,
    chrome_header_text,
    wait_for,
)
from test_fallback_footer_pty import read_until


def test_native_settings_returns_to_exact_tab_without_restarting_btop(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pid = wait_for(session.btop_pid)
        session.switch("Files")
        session.open_settings()
        session.back_to("Files")
        session.switch("Monitor")
        session.open_settings()
        session.back_to("Monitor")
        assert session.btop_pid() == pid
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
    finally:
        session.close()


def test_native_sessions_keep_return_tabs_separate(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    first = NativeSession(tmp_path, env, binary)
    try:
        second = NativeSession(tmp_path, env, binary)
        try:
            first.switch("Monitor")
            second.switch("Files")
            first.open_settings()
            second.open_settings()
            first.back_to("Monitor")
            second.back_to("Files")
        finally:
            second.close()
    finally:
        first.close()


def test_native_header_responds_to_mouse(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        wait_for(lambda: "Settings" in chrome_header_text(session))
        assert_no_chrome_ptys(session)
        session.click_header()
        target = session.tab_id("tdnext-header:settings")
        wait_for(lambda: session.active_tab() == target)
        session.back_to("Files")
    finally:
        session.close()


def test_native_header_clicks_only_activate_actions(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        wait_for(lambda: "Settings" in chrome_header_text(session))
        files = next(pane for pane in json.loads(
            session.action("list-panes", "--all", "--json")
        ) if "tdnext.pane view files" in (pane.get("pane_command") or ""))
        session.header_mouse(5, release=False)
        wait_for(lambda: f"terminal_{files['id']}" in session.action("list-clients"))
        session.header_mouse(5, release=True)
        wait_for(lambda: f"terminal_{files['id']}" in session.action("list-clients"))
        assert session.active_tab() == session.tab_id("Files")
        session.click_header()
        wait_for(lambda: session.active_tab() == session.tab_id("tdnext-header:settings"))
        session.back_to("Files")
    finally:
        session.close()


def test_native_small_session_lifecycle(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    """An 80x24 terminal keeps the direct-mode fallback lifecycle without a nested Zellij session."""
    env, binary = native_env
    before = subprocess.run(
        [binary, "list-sessions", "--no-formatting"],
        capture_output=True, text=True, env=env, check=False, timeout=5,
    ).stdout
    before_sessions = {line.split()[0] for line in before.splitlines() if line.strip()}
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "tdnext", "run", str(tmp_path)],
        stdin=slave, stdout=slave, stderr=slave, env=env,
    )
    os.close(slave)
    try:
        read_until(master, "WORKSPACE", "Tool (Files):", "Global:", "Ctrl+q")
        os.write(master, b"\x1bo")
        read_until(master, "Tool (Settings):", "Esc Back")
        os.write(master, b"\x1b")
        read_until(master, "Tool (Files):")
        os.write(master, b"\x11")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
    after = subprocess.run(
        [binary, "list-sessions", "--no-formatting"],
        capture_output=True, text=True, env=env, check=False, timeout=5,
    ).stdout
    after_sessions = {line.split()[0] for line in after.splitlines() if line.strip()}
    assert after_sessions == before_sessions
