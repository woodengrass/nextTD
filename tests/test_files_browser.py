"""Launch the configured editor from Rust Files and inspect its arguments and environment."""

from __future__ import annotations

import os
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
    if not shutil_which("tmux"):
        pytest.skip("native test requires tmux")
    return str(BINARY)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


class FilesPty:
    """Drive the real ``tdnext-files`` binary on a private tmux server."""

    def __init__(
        self, binary: str, directory: Path, state_dir: Path, extra_env: dict[str, str] | None = None
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

    def wait_for(self, needle: str, *, timeout: float = 20.0) -> str:
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


def test_editor_runs_configured_command_with_selected_real_file_and_restored_env(tmp_path: Path) -> None:
    """B1: pressing ``e`` runs ``$EDITOR`` (with flags) on the selected file.

    B2: the editor child sees the restored ``PATH`` and none of the
    ``TDNEXT_*`` bootstrap markers.
    """
    binary = require_binary()
    work = tmp_path / "work"
    work.mkdir()
    source = work / "notes.txt"
    source.write_text("existing content", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    log_args = tmp_path / "argv.log"
    log_env = tmp_path / "env.log"
    fake_editor = tmp_path / "fake-editor"
    fake_editor.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$LOG_ARGS"\n'
        'printf "PATH=%s\\n" "${PATH-<unset>}" >> "$LOG_ENV"\n'
        'for marker in TDNEXT_PATH_BOOTSTRAPPED TDNEXT_ORIGINAL_PATH TDNEXT_ORIGINAL_PATH_PRESENT; do\n'
        '  eval "value=\\${$marker-<unset>}"\n'
        '  printf "%s=%s\\n" "$marker" "$value" >> "$LOG_ENV"\n'
        "done\n",
        encoding="utf-8",
    )
    fake_editor.chmod(0o755)
    restored_path = "restored-sentinel-path"
    pty = FilesPty(
        binary,
        work,
        state_dir,
        extra_env={
            "EDITOR": f"{fake_editor} --flag",
            "LOG_ARGS": str(log_args),
            "LOG_ENV": str(log_env),
            "PATH": "sentinel-should-be-replaced",
            "TDNEXT_PATH_BOOTSTRAPPED": "1",
            "TDNEXT_ORIGINAL_PATH_PRESENT": "1",
            "TDNEXT_ORIGINAL_PATH": restored_path,
        },
    )
    try:
        pty.wait_for("notes.txt")
        pty.send("e")

        def launched() -> bool:
            try:
                return log_args.is_file()
            except OSError:
                return False

        deadline = time.monotonic() + 20.0
        while not launched():
            if time.monotonic() > deadline:
                raise AssertionError(f"fake editor never ran; screen:\n{pty.screen()}")
            time.sleep(0.05)
        assert log_args.read_text(encoding="utf-8").splitlines() == ["--flag", str(source)]
        assert dict(
            line.split("=", 1) for line in log_env.read_text(encoding="utf-8").splitlines()
        ) == {
            "PATH": restored_path,
            "TDNEXT_PATH_BOOTSTRAPPED": "<unset>",
            "TDNEXT_ORIGINAL_PATH": "<unset>",
            "TDNEXT_ORIGINAL_PATH_PRESENT": "<unset>",
        }
        assert source.read_text(encoding="utf-8") == "existing content"
        pty.wait_for("notes.txt")
    finally:
        pty.close()
