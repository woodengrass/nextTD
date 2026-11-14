import os
import select
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession


class StopBeforeLaunch(Exception):
    pass


def test_native_startup_does_not_unlink_an_in_progress_peer_socket(
    tmp_path: Path, native_env: tuple[dict[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, binary = native_env
    socket_dir = Path(env["ZELLIJ_SOCKET_DIR"]) / "contract_version_1"
    socket_dir.mkdir()
    peer_path = socket_dir / "peer-starting"
    original_run = subprocess.run

    def run(command: list[str], **kwargs):
        if command[0] == "tmux" and "new-session" in command:
            assert peer_path.exists(), "starting a session removed another server's bound socket"
            raise StopBeforeLaunch
        return original_run(command, **kwargs)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.bind(str(peer_path))
        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(StopBeforeLaunch):
            NativeSession(tmp_path, env, binary)


def test_monitor_pid_does_not_confuse_version_probe_for_monitor() -> None:
    session = NativeSession.__new__(NativeSession)
    session.name = f"pid-probe-{uuid.uuid4().hex}"
    code = (
        "import ctypes, os; "
        "assert ctypes.CDLL(None).prctl(15, ctypes.c_char_p(b'btop'), 0, 0, 0) == 0; "
        "print('ready', flush=True); os.read(0, 1)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code, "--version"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env={**os.environ, "ZELLIJ_SESSION_NAME": session.name}, text=True,
    ) as probe:
        try:
            assert probe.stdout is not None
            assert select.select([probe.stdout], [], [], 5)[0]
            assert probe.stdout.readline().strip() == "ready"
            assert session.btop_pid() is None
        finally:
            probe.terminate()


def test_cleanup_terminates_only_the_exact_tagged_session(monkeypatch: pytest.MonkeyPatch) -> None:
    tag = f"cleanup-{uuid.uuid4().hex}"
    code = "import os; print('ready', flush=True); os.read(0, 1)"

    def start(session_tag: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={**os.environ, "TDNEXT_TEST_SESSION": session_tag}, text=True,
        )

    with start(tag) as owned, start(f"{tag}-other") as other:
        try:
            for process in (owned, other):
                assert process.stdout is not None
                assert select.select([process.stdout], [], [], 5)[0]
                assert process.stdout.readline().strip() == "ready"
            def report_success(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 0, "", "")

            monkeypatch.setattr(subprocess, "run", report_success)
            session = NativeSession.__new__(NativeSession)
            session.binary = "zellij"
            session.name = "session"
            session.env = {}
            session.tmux_socket = "isolated-socket"
            session.tmux_name = tag
            session.close()
            assert owned.wait(timeout=5) == -signal.SIGTERM
            assert other.poll() is None
        finally:
            owned.terminate()
            other.terminate()


def test_cleanup_escalates_when_tagged_process_ignores_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    tag = f"stubborn-{uuid.uuid4().hex}"
    code = "import os,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); os.read(0,1)"
    with subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TDNEXT_TEST_SESSION": tag}, text=True,
    ) as process:
        try:
            assert process.stdout is not None
            assert select.select([process.stdout], [], [], 5)[0]
            assert process.stdout.readline().strip() == "ready"

            def report_success(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 0, "", "")

            monkeypatch.setattr(subprocess, "run", report_success)
            session = NativeSession.__new__(NativeSession)
            session.binary = "zellij"
            session.name = "session"
            session.env = {}
            session.tmux_socket = "isolated-socket"
            session.tmux_name = tag
            session.close()
            assert process.wait(timeout=3) == -signal.SIGKILL
        finally:
            process.kill()
