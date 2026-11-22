"""Check listing order, hidden files, and create/rename dialogs in the real Rust Files pane."""

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


def require_binary() -> str:
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

    def __init__(self, binary: str, directory: Path, state_dir: Path) -> None:
        self.socket = str(state_dir / "tmux.sock")
        self.session = "files"
        subprocess.run(
            ["tmux", "-S", self.socket, "-f", os.devnull, "new-session", "-d", "-s", self.session,
             "-x", "130", "-y", "30",
             "-e", f"TDNEXT_STATE_DIR={state_dir}", binary, str(directory)],
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

    def close(self) -> None:
        subprocess.run(
            ["tmux", "-S", self.socket, "kill-server"],
            check=False, capture_output=True, timeout=10,
        )


def row_of(frame: str, name: str) -> int:
    for index, line in enumerate(frame.splitlines()):
        if name in line:
            return index
    raise AssertionError(f"{name!r} not visible in Files screen:\n{frame}")


def test_real_listing_sorts_folders_first_and_hides_dotfiles_until_toggled(tmp_path: Path) -> None:
    """B1: folders list before files and dotfiles stay hidden by default.

    B2: pressing ``.`` reveals dotfiles alongside the existing entries.
    """
    binary = require_binary()
    work = tmp_path / "work"
    work.mkdir()
    (work / "z-folder").mkdir()
    (work / "a-file").touch()
    (work / ".hidden").touch()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pty = FilesPty(binary, work, state_dir)
    try:
        frame = pty.wait_for("a-file")
        assert ".hidden" not in frame
        assert row_of(frame, "z-folder") < row_of(frame, "a-file")
        frame = pty.wait_for("a-file")
        pty.send(".")
        frame = pty.wait_for(".hidden")
        assert row_of(frame, "z-folder") < row_of(frame, ".hidden") < row_of(frame, "a-file")
    finally:
        pty.close()


def test_real_create_and_rename_dialogs_change_real_files(tmp_path: Path) -> None:
    """B1: the ``f`` dialog creates a real file.

    B2: the ``r`` dialog renames the selected real file without touching its
    contents.
    """
    binary = require_binary()
    work = tmp_path / "work"
    work.mkdir()
    (work / "seed.txt").touch()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pty = FilesPty(binary, work, state_dir)
    try:
        pty.wait_for("seed.txt")
        pty.send("f")
        pty.wait_for("New file name")
        pty.type_text("before.txt")
        pty.send("Enter")
        pty.wait_for("before.txt")
        created = work / "before.txt"
        assert created.is_file()
        created.write_text("unchanged content", encoding="utf-8")
        pty.send("Home")
        pty.send("r")
        pty.wait_for("Rename to")
        pty.type_text("after.txt")
        pty.send("Enter")
        pty.wait_for("after.txt")
        assert (work / "after.txt").read_text(encoding="utf-8") == "unchanged content"
        assert not (work / "before.txt").exists()
    finally:
        pty.close()
