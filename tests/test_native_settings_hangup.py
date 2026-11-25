"""A disconnected terminal must not strand a Settings process."""

import fcntl
import os
import pty
import select
import subprocess
import termios
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("after_frame", [True, False], ids=["rendered", "starting"])
def test_settings_exits_when_pty_hangs_up(tmp_path: Path, after_frame: bool) -> None:
    binary = os.environ.get("TDNEXT_SETTINGS_BINARY") or str(
        Path(__file__).parents[1] / "native-panes/settings/target/release/tdnext-settings"
    )
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (32, 100))

    def control_terminal() -> None:
        os.setsid()
        if after_frame:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    with subprocess.Popen([binary], stdin=slave, stdout=slave, stderr=slave, preexec_fn=control_terminal,
                          env={**os.environ, "TDNEXT_STATE_DIR": str(tmp_path)}) as child:
        os.close(slave)
        try:
            if after_frame:
                frame = bytearray()
                deadline = time.monotonic() + 2
                while b"Icons: restart. Editor: next file open." not in frame:
                    assert select.select([master], [], [], max(0, deadline - time.monotonic()))[0], "no first frame"
                    frame.extend(os.read(master, 8192))
            # Without a controlling tty we can close even before the program
            # installs signal handlers, exercising the I/O failure without SIGHUP.
            os.close(master)
            master = -1
            assert child.wait(timeout=2) == 0
        finally:
            if master >= 0:
                os.close(master)
            child.kill()
            child.wait()
