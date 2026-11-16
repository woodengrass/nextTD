import fcntl
import os
import pty
import struct
import subprocess
import sys
import termios
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for
from test_fallback_footer_pty import read_until
from test_settings_lifecycle import make_manager
from textual.widgets import Static

from tdnext.api import PluginContext
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.desktop.app import DesktopApp


@pytest.mark.anyio
async def test_empty_fallback_remains_interactive(tmp_path: Path) -> None:
    app = DesktopApp(PluginContext(tmp_path, make_manager(tmp_path)), [])
    async with app.run_test(size=(80, 24)) as pilot:
        assert "No views enabled" in str(app.query_one("#empty-workspace", Static).render())
        theme = app.theme
        await pilot.press("f6", "alt+o", "ctrl+t")
        assert app.theme != theme
        await pilot.press("ctrl+q")
    assert app.return_code == 0


def test_empty_fallback_in_real_pty(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, _binary = native_env
    ids = frozenset({"desktop"})
    StateStore(Path(env["TDNEXT_STATE_DIR"]) / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "tdnext", "run", str(tmp_path)],
        stdin=slave, stdout=slave, stderr=slave, env={**env, "ZELLIJ": "1"},
    )
    os.close(slave)
    try:
        read_until(master, "No views enabled", "Ctrl+q")
        os.write(master, b"\x11")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)


def test_empty_native_session_displays_desktop(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    ids = frozenset({"desktop"})
    StateStore(Path(env["TDNEXT_STATE_DIR"]) / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=1, expected_panes=1)
    try:
        screen = wait_for(lambda: (text if "No views enabled" in (text := session.action("dump-screen")) else ""))
        assert "TERMINAL DESKTOP" in screen
        assert "Ctrl+q" in screen
        session.send_key("F6")
        assert "No views enabled" in session.action("dump-screen")
    finally:
        session.close()
