"""Shared PTY driver for the real ``tdnext-files`` binary.

One place for binary location, tmux server handling, and screen waits so
individual Files PTY tests do not copy the harness.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BINARY = Path(
    os.environ.get(
        "TDNEXT_FILES_BINARY",
        Path(__file__).parents[1] / "native-panes" / "files" / "target" / "release" / "tdnext-files",
    )
)


def require_files_binary() -> str:
    if not BINARY.is_file() or not os.access(BINARY, os.X_OK):
        pytest.fail(
            f"native test requires the built Files binary at {BINARY}: "
            "build with cargo build --locked --release --manifest-path native-panes/files/Cargo.toml"
        )
    if shutil.which("tmux") is None:
        pytest.skip("native test requires tmux")
    return str(BINARY)


class FilesPty:
    """Drive the real ``tdnext-files`` binary on a private tmux server."""

    def __init__(
        self,
        binary: str,
        directory: Path,
        state_dir: Path,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.socket = str(state_dir / "tmux.sock")
        self.session = "files"
        env_args: list[str] = []
        env = {"TDNEXT_STATE_DIR": str(state_dir)}
        env.update(extra_env or {})
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        subprocess.run(
            ["tmux", "-S", self.socket, "-f", os.devnull, "new-session", "-d", "-s", self.session,
             "-x", "130", "-y", "30", *env_args, binary, str(directory)],
            check=True, capture_output=True, timeout=10,
        )

    def send(self, *keys: str) -> None:
        subprocess.run(
            ["tmux", "-S", self.socket, "send-keys", "-t", self.session, *keys],
            check=True, capture_output=True, timeout=10,
        )

    def type_text(self, text: str) -> None:
        self.send("-l", text)

    def screen(self) -> str:
        return subprocess.run(
            ["tmux", "-S", self.socket, "capture-pane", "-p", "-t", self.session],
            check=True, text=True, capture_output=True, timeout=10,
        ).stdout

    def wait_for(self, needle: str, *, timeout: float = 30.0) -> str:
        deadline = time.monotonic() + timeout
        frame = ""
        while time.monotonic() < deadline:
            frame = self.screen()
            if needle in frame:
                return frame
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {needle!r} in Files screen:\n{frame}")

    def click_sgr(self, label: str, same_row_as: str | None = None) -> None:
        frame = self.wait_for(same_row_as or label)
        for y, row in enumerate(frame.splitlines(), start=1):
            if label in row and (same_row_as is None or same_row_as in row):
                x = row.index(label) + 1 + len(label) // 2
                self.send("-l", f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m")
                return
        raise AssertionError(f"No clickable row for {label!r}")

    def resize(self, width: int, height: int) -> None:
        subprocess.run(
            ["tmux", "-S", self.socket, "resize-window", "-t", self.session, "-x", str(width), "-y", str(height)],
            check=True, capture_output=True, timeout=10,
        )

    def close(self) -> None:
        subprocess.run(
            ["tmux", "-S", self.socket, "kill-server"],
            check=False, capture_output=True, timeout=10,
        )
