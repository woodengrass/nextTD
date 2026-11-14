import errno
import os
import signal
import sys
from pathlib import Path

import anyio
import pytest
from test_shell_pipeline_cleanup import stop_owned_process

from tdnext.core.errors import PluginError
from tdnext.plugins.shell.transport import PtySession


def stubborn_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PtySession:
    shell = tmp_path / "stubborn-shell"
    shell.write_text(
        "#!/usr/bin/python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "print('SHELL_READY', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    monkeypatch.setenv("HOME", str(tmp_path))
    return PtySession(tmp_path)


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "pidfd_open"), reason="requires Linux pidfds")
async def test_shell_escalation_uses_owned_pidfd_before_closing_pty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = stubborn_shell(tmp_path, monkeypatch)
    ready = anyio.Event()
    output = bytearray()
    owned = os.pidfd_open(session.process.pid)
    assert session.master is not None
    master = session.master
    master_closed = False
    sent: list[signal.Signals] = []
    close = os.close
    send = signal.pidfd_send_signal

    def receive(data: bytes) -> None:
        output.extend(data)
        if b"SHELL_READY" in output:
            ready.set()

    def record_close(fd: int) -> None:
        nonlocal master_closed
        if fd == master:
            master_closed = True
        close(fd)

    def record_signal(pidfd: int, sig: signal.Signals) -> None:
        assert not master_closed
        sent.append(sig)
        send(pidfd, sig)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(session.watch, receive)
            with anyio.fail_after(8):
                await ready.wait()
            with monkeypatch.context() as trace:
                trace.setattr(os, "close", record_close)
                trace.setattr(signal, "pidfd_send_signal", record_signal)
                await session.aclose()
            assert master_closed and sent == [signal.SIGHUP, signal.SIGKILL]
            assert session.process.returncode == -signal.SIGKILL
            tasks.cancel_scope.cancel()
    finally:
        stop_owned_process(owned)
        os.close(owned)
        with anyio.CancelScope(shield=True):
            await session.aclose()
            await anyio.to_thread.run_sync(session.process.wait)


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "pidfd_open"), reason="requires Linux pidfds")
async def test_missing_pidfd_fails_closed_for_a_shell_ignoring_hangup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = anyio.Event()
    output = bytearray()
    open_pidfd = os.pidfd_open

    def unavailable(_pid: int) -> int:
        raise OSError(errno.ENOSYS, "pidfd unavailable")

    with monkeypatch.context() as setup:
        setup.setattr(os, "pidfd_open", unavailable)
        session = stubborn_shell(tmp_path, setup)
    owned = open_pidfd(session.process.pid)

    def receive(data: bytes) -> None:
        output.extend(data)
        if b"SHELL_READY" in output:
            ready.set()

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(session.watch, receive)
            with anyio.fail_after(8):
                await ready.wait()
            with pytest.raises(PluginError, match="Cannot safely force-terminate"):
                await session.aclose()
            assert session.process.poll() is None
            tasks.cancel_scope.cancel()
    finally:
        stop_owned_process(owned)
        os.close(owned)
        with anyio.CancelScope(shield=True):
            await session.aclose()
            await anyio.to_thread.run_sync(session.process.wait)
