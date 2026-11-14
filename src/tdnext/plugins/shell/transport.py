"""Own a shell PTY; terminal parsing and rendering belong to Ghostty."""

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
from collections.abc import Callable
from functools import partial
from pathlib import Path

import anyio

from tdnext.core.errors import PluginError
from tdnext.core.local_env import restored_env

from .launch import user_shell


class PtySession:
    """Mutable owner of one PTY master and its child process."""

    def __init__(self, directory: Path) -> None:
        master, slave = pty.openpty()
        try:
            os.set_blocking(master, False)
            environment = {**restored_env(), "TERM": "xterm-256color", "COLORTERM": "truecolor"}
            self.process = subprocess.Popen(
                [sys.executable, "-m", "tdnext.plugins.shell.pty_exec", user_shell()],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=directory,
                env=environment,
                start_new_session=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.master: int | None = master
        self._shell_pidfd: int | None = None
        if sys.platform == "linux" and hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                self._shell_pidfd = os.pidfd_open(self.process.pid)
            except OSError as exc:
                self._pidfd_error: OSError | None = exc
            else:
                self._pidfd_error = None
        else:
            self._pidfd_error = None
        self._closed = anyio.Event()

    def resize(self, columns: int, rows: int) -> None:
        if self.master is not None and columns > 0 and rows > 0:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    async def send(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining and self.master is not None:
            try:
                count = os.write(self.master, remaining)
            except BlockingIOError:
                await anyio.wait_writable(self.master)
            except OSError as exc:
                if exc.errno == errno.EIO and self.process.poll() is not None:
                    return
                raise
            else:
                remaining = remaining[count:]

    async def _read(self, receive: Callable[[bytes], None], done: anyio.Event) -> None:
        try:
            while (master := self.master) is not None:
                await anyio.wait_readable(master)
                if self.master != master:
                    return
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        return
                    raise
                if not data:
                    return
                receive(data)
        except anyio.ClosedResourceError:
            return
        finally:
            done.set()

    async def watch(self, receive: Callable[[bytes], None]) -> int:
        done = anyio.Event()
        code: int | None = None
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(self._read, receive, done)
            code = await anyio.to_thread.run_sync(self.process.wait, abandon_on_cancel=True)
            with anyio.move_on_after(0.25):
                await done.wait()
            tasks.cancel_scope.cancel()
        if code is None:
            raise PluginError("Shell process wait ended without an exit status")
        return code

    @staticmethod
    def _signal_pidfd(pidfd: int, sig: signal.Signals) -> None:
        try:
            signal.pidfd_send_signal(pidfd, sig)
        except ProcessLookupError:
            return  # The referenced process already exited.

    @staticmethod
    def _foreground_matches(master: int, foreground: int) -> bool:
        try:
            return os.tcgetpgrp(master) == foreground
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.ENOTTY):
                return False
            raise

    @staticmethod
    def _process_running(pidfd: int) -> bool:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        return not poller.poll(0)

    def _signal_foreground(self, master: int, foreground: int, sig: signal.Signals) -> None:
        owner = self._shell_pidfd
        if owner is None or not self._process_running(owner):
            return
        # A group can outlive its leader. Pin each member and recheck PTY/session ownership;
        # signaling a numeric process group could hit a recycled, unrelated group.
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                pid = int(entry.name)
                if pid == self.process.pid:
                    continue
                try:
                    if os.getpgid(pid) != foreground or os.getsid(pid) != self.process.pid:
                        continue
                    target = os.pidfd_open(pid)
                    try:
                        if not self._foreground_matches(master, foreground):
                            return
                        if (
                            os.getpgid(pid) == foreground
                            and os.getsid(pid) == self.process.pid
                            and self._process_running(owner)
                        ):
                            self._signal_pidfd(target, sig)
                    finally:
                        os.close(target)
                except ProcessLookupError:
                    continue

    async def _wait_for_foreground(self, master: int, foreground: int) -> None:
        with anyio.move_on_after(0.25):
            while self._foreground_matches(master, foreground):
                await anyio.sleep(0.01)

    async def aclose(self) -> None:
        master, self.master = self.master, None
        if master is None:
            await self._closed.wait()
            return
        master_open = True
        try:
            try:
                try:
                    foreground = os.tcgetpgrp(master)
                except OSError as exc:
                    if exc.errno not in (errno.EIO, errno.ENOTTY):
                        raise
                    foreground = -1
                if foreground > 0 and foreground != self.process.pid and self._shell_pidfd is not None:
                    # Keep the PTY open and let the shell reap its foreground job.
                    await anyio.to_thread.run_sync(self._signal_foreground, master, foreground, signal.SIGHUP)
                    await self._wait_for_foreground(master, foreground)
                    await anyio.to_thread.run_sync(self._signal_foreground, master, foreground, signal.SIGKILL)
                    await self._wait_for_foreground(master, foreground)
            finally:
                if self._shell_pidfd is not None:
                    self._signal_pidfd(self._shell_pidfd, signal.SIGHUP)
                else:
                    anyio.notify_closing(master)
                    os.close(master)
                    master_open = False
        finally:
            try:
                try:
                    await anyio.to_thread.run_sync(partial(self.process.wait, timeout=0.5))
                except subprocess.TimeoutExpired as exc:
                    if self._shell_pidfd is None:
                        detail = f": {self._pidfd_error}" if self._pidfd_error is not None else ""
                        raise PluginError(f"Cannot safely force-terminate shell without Linux pidfds{detail}") from exc
                    self._signal_pidfd(self._shell_pidfd, signal.SIGKILL)
                    await anyio.to_thread.run_sync(self.process.wait)
            finally:
                try:
                    if master_open:
                        anyio.notify_closing(master)
                        os.close(master)
                finally:
                    try:
                        if self._shell_pidfd is not None:
                            os.close(self._shell_pidfd)
                    finally:
                        self._closed.set()
