import errno
from pathlib import Path

import anyio
import pytest

from tdnext.plugins.shell.transport import PtySession


@pytest.mark.anyio
async def test_closing_pty_releases_a_backpressured_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("HISTFILE", "/dev/null")
    monkeypatch.setenv("INPUTRC", "/dev/null")
    monkeypatch.delenv("BASH_ENV", raising=False)
    session = PtySession(tmp_path)
    output = bytearray()
    ready, blocked, finished = anyio.Event(), anyio.Event(), anyio.Event()
    wait_writable = anyio.wait_writable

    def receive(data: bytes) -> None:
        output.extend(data)
        if b"WRITER_READY" in output:
            ready.set()

    async def observe_backpressure(fd: int) -> None:
        blocked.set()
        await wait_writable(fd)

    async def write_large_input() -> None:
        try:
            await session.send(b"x" * 1_000_000)
        except anyio.ClosedResourceError:
            return
        except OSError as exc:
            if exc.errno in (errno.EBADF, errno.EIO):
                return
            raise
        finally:
            finished.set()

    monkeypatch.setattr(anyio, "wait_writable", observe_backpressure)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(session.watch, receive)
            await session.send(
                b"stty -echo -icanon; python3 -c 'import time; print(\"WRITER_\"+\"READY\",flush=True); "
                b"time.sleep(60)'\n"
            )
            with anyio.fail_after(8):
                await ready.wait()
            tasks.start_soon(write_large_input)
            with anyio.fail_after(8):
                await blocked.wait()
            await session.aclose()
            with anyio.fail_after(3):
                await finished.wait()
            assert session.process.poll() is not None
            tasks.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            await session.aclose()
