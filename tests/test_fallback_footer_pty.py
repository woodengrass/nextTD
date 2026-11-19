import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest
from native_environment import native_env as native_env

from tdnext.runtime import manager_for

ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|.)", re.DOTALL)


def read_until(master: int, *expected: str) -> str:
    output = bytearray()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            break
        output.extend(os.read(master, 65536))
        text = ANSI.sub(b"", output).decode("utf-8", errors="replace")
        if all(item in text for item in expected):
            return text
    raise AssertionError(f"PTY did not render {expected!r}: {bytes(output)!r}")


@pytest.mark.parametrize("size", [(80, 24), (120, 36)], ids=["80x24", "wide"])
@pytest.mark.parametrize("header", [False, True], ids=["header-disabled", "header-enabled"])
def test_fallback_footer_in_real_pty(
    tmp_path: Path, native_env: tuple[dict[str, str], str], size: tuple[int, int], header: bool
) -> None:
    env, _binary = native_env
    plugins = manager_for(tmp_path / "state")
    for plugin_id in ("desktop", "files", "monitor"):
        plugins.enable(plugin_id)
    if header:
        plugins.enable("settings")
    else:
        plugins.disable("settings")
    ids = frozenset({"desktop", "files", "monitor"} | ({"settings"} if header else set()))
    assert plugins.store.read().enabled == ids
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "tdnext", "run", str(tmp_path)],
        stdin=slave, stdout=slave, stderr=slave, env={**env, "ZELLIJ": "1"},
    )
    os.close(slave)
    try:
        expected = ("Tool (Files):", "Global:", "Ctrl+q", "Ctrl+t", "Tab focus")
        screen = read_until(master, *expected, *(("Alt+o header",) if header else ()))
        assert ("Alt+o" in screen) is header
        if header:
            os.write(master, b"\x1bo")
            assert "Esc Back" in read_until(master, "Tool (Settings):", "Esc Back")
            os.write(master, b"\x1b")
            assert "Enter open" in read_until(master, "Tool (Files):", "Enter open")
            os.write(master, b"\t\t\x1b[B\r")
            read_until(master, "Tool (Monitor):", "Enter open btop")
            os.write(master, b"\r")
            read_until(master, "cpu", "mem", "proc")
            os.write(master, b"q")
            assert "Enter open btop" in read_until(master, "Tool (Monitor):", "Enter open btop")
        os.write(master, b"\x11")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
