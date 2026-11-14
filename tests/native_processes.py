import os
import select
import signal
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path


def tagged_processes(tag: str) -> Iterator[tuple[int, list[bytes], list[bytes]]]:
    marker = f"TDNEXT_TEST_SESSION={tag}".encode()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            environ = (process / "environ").read_bytes().split(b"\0")
            if marker in environ:
                yield int(process.name), environ, (process / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def attached_session(tag: str, expected_panes: int) -> str | None:
    sessions: dict[str, set[bytes]] = {}
    for _pid, environ, _command in tagged_processes(tag):
        pane = next((entry.removeprefix(b"ZELLIJ_PANE_ID=") for entry in environ
                     if entry.startswith(b"ZELLIJ_PANE_ID=")), b"")
        name = next((entry.removeprefix(b"ZELLIJ_SESSION_NAME=") for entry in environ
                     if entry.startswith(b"ZELLIJ_SESSION_NAME=")), b"")
        if name and pane:
            session = os.fsdecode(name)
            sessions.setdefault(session, set()).add(pane)
            if len(sessions[session]) >= expected_panes:
                return session
    return None


def terminate_tagged(tag: str) -> None:
    with ExitStack() as stack:
        handles: list[int] = []
        for pid, _environ, _command in tagged_processes(tag):
            try:
                handle = os.pidfd_open(pid)
                stack.callback(os.close, handle)
                signal.pidfd_send_signal(handle, signal.SIGTERM)
                handles.append(handle)
            except ProcessLookupError:
                continue
        for handle in handles:
            exited = select.poll()
            exited.register(handle, select.POLLIN)
            if not exited.poll(1000):
                try:
                    signal.pidfd_send_signal(handle, signal.SIGKILL)
                except ProcessLookupError:
                    continue
                if not exited.poll(1000):
                    raise AssertionError(f"Tagged process did not exit after SIGKILL: pidfd={handle}")


def monitor_pid(name: str) -> int | None:
    marker = f"ZELLIJ_SESSION_NAME={name}".encode()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if marker not in (process / "environ").read_bytes().split(b"\0"):
                continue
            command = (process / "cmdline").read_bytes().split(b"\0")
            if ((process / "comm").read_text().strip() == "btop" and len(command) == 2 and command[1] == b""
                    and Path(os.fsdecode(command[0])).name == "btop"):
                return int(process.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return None
