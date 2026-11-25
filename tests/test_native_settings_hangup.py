"""A disconnected controlling terminal must not strand a Settings process."""

import fcntl
import os
import pty
import select
import subprocess
import termios
import time
from pathlib import Path


def test_settings_exits_when_controlling_pty_hangs_up(tmp_path: Path) -> None:
    binary = os.environ.get("TDNEXT_SETTINGS_BINARY") or str(
        Path(__file__).parents[1] / "native-panes/settings/target/release/tdnext-settings"
    )
    master, slave = pty.openpty()

    def control_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    with subprocess.Popen([binary], stdin=slave, stdout=slave, stderr=slave, preexec_fn=control_terminal,
                          env={**os.environ, "TDNEXT_STATE_DIR": str(tmp_path)}) as child:
        os.close(slave)
        try:
            assert select.select([master], [], [], 2)[0], "Settings never entered the terminal"
            deadline = time.monotonic() + 2
            tasks = Path(f"/proc/{child.pid}/task")
            while not any("poll" in w.read_text() for w in tasks.glob("*/wchan")):
                assert time.monotonic() < deadline, "Settings never waited for input"
                time.sleep(0.001)
            os.close(master)
            master = -1
            assert child.wait(timeout=2) == 0
        finally:
            if master >= 0:
                os.close(master)
            child.kill()
            child.wait()
