import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
from pathlib import Path

import pytest
from test_fallback_footer_pty import read_until

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


@pytest.mark.parametrize("size", [(80, 24), (120, 36)], ids=["narrow", "wide"])
def test_fallback_shell_in_real_outer_pty_keeps_desktop_and_interactive_program(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    ids = frozenset({"desktop", "files", "settings", "shell"})
    state = tmp_path / "state"
    StateStore(state / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    excluded = {"TMUX", "TMUX_PANE", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "BASH_ENV"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    env.update({
        "HOME": str(home), "SHELL": "/bin/bash", "HISTFILE": str(home / "history"), "INPUTRC": "/dev/null",
        "TERM": "xterm-256color", "TDNEXT_STATE_DIR": str(state), "ZELLIJ": "1",
        "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    })
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "tdnext", "run", str(tmp_path)], stdin=slave, stdout=slave, stderr=slave, env=env
    )
    os.close(slave)
    try:
        read_until(master, "WORKSPACE", "Tool (Files):", "Global:")
        os.write(master, b"\x1b[17~\x1b[B\r")
        read_until(master, "Tool (Shell):", "F6 sidebar")
        os.write(master, b"printf 'PTY_%s\\n' WAIT; read -r answer; printf 'PTY_RESULT:%s\\n' \"$answer\"\r")
        read_until(master, "PTY_WAIT")
        os.write(master, b"\x1bo")
        read_until(master, "Tool (Settings):")
        os.write(master, b"\x1b")
        read_until(master, "Tool (Shell):")
        os.write(master, b"hello\r")
        read_until(master, "PTY_RESULT:hello")
        new_size = (size[0] + 10, size[1] + 4)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", new_size[1], new_size[0], 0, 0))
        os.kill(process.pid, signal.SIGWINCH)
        expected_size = f"{new_size[1] - 3} {new_size[0] - 22}"
        command = (
            f"until [ \"$(stty size)\" = '{expected_size}' ]; do sleep 0.05; done; "
            "printf 'PTY_SIZE:'; stty size\r"
        )
        os.write(master, command.encode())
        read_until(master, f"PTY_SIZE:{expected_size}")
        os.write(master, b"\x11")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
