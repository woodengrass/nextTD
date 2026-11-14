import os
import re
import signal
import sys
from pathlib import Path

import anyio
import pytest

from tdnext.plugins.shell.transport import PtySession


def leader_gone(group: int) -> bool:
    try:
        os.getsid(group)
    except ProcessLookupError:
        return True
    return False


def stop_owned_process(pidfd: int) -> None:
    try:
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    except ProcessLookupError:
        return


@pytest.mark.anyio
@pytest.mark.skipif(sys.platform != "linux" or not hasattr(os, "pidfd_open"), reason="requires Linux pidfds")
@pytest.mark.parametrize("ignore_interrupt", [False, True], ids=["ignore-hangup", "ignore-hangup-and-interrupt"])
async def test_closing_leaderless_pipeline_preserves_another_interactive_pty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore_interrupt: bool
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("HISTFILE", "/dev/null")
    monkeypatch.setenv("INPUTRC", "/dev/null")
    monkeypatch.delenv("BASH_ENV", raising=False)
    victim = PtySession(tmp_path)
    peer = PtySession(tmp_path)
    victim_output, peer_output = bytearray(), bytearray()
    victim_ready, peer_ready, peer_result = anyio.Event(), anyio.Event(), anyio.Event()
    pattern = re.compile(rb"PIPE_ID:(\d+):(\d+):(\d+)")
    pidfd: int | None = None

    def receive_victim(data: bytes) -> None:
        victim_output.extend(data)
        if pattern.search(victim_output):
            victim_ready.set()

    def receive_peer(data: bytes) -> None:
        peer_output.extend(data)
        if b"PEER_READY" in peer_output:
            peer_ready.set()
        if b"PEER_RESULT:still-interactive" in peer_output:
            peer_result.set()

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(victim.watch, receive_victim)
            tasks.start_soon(peer.watch, receive_peer)
            await peer.send(b"printf 'PEER_%s\\n' READY\n")
            signal_setup = "signal.signal(signal.SIGINT,signal.SIG_IGN); " if ignore_interrupt else ""
            command = (
                "true | python3 -c 'import os,signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
                f"{signal_setup}print(\"PIPE_ID:%d:%d:%d\" % (os.getpid(),os.getpgrp(),os.getsid(0)),flush=True); "
                "time.sleep(60)'\n"
            )
            await victim.send(command.encode())
            with anyio.fail_after(8):
                await peer_ready.wait()
                await victim_ready.wait()
            match = pattern.search(victim_output)
            assert match is not None
            member, group, session_id = (int(value) for value in match.groups())
            pidfd = os.pidfd_open(member)
            with anyio.fail_after(8):
                while not leader_gone(group):
                    await anyio.sleep(0.01)
            assert victim.master is not None
            assert os.tcgetpgrp(victim.master) == group
            assert os.getpgid(member) == group and os.getsid(member) == session_id == victim.process.pid
            assert peer.process.pid != session_id

            master = victim.master
            original_tcgetpgrp = os.tcgetpgrp
            with monkeypatch.context() as race:
                race.setattr(
                    os, "tcgetpgrp", lambda fd: peer.process.pid if fd == master else original_tcgetpgrp(fd)
                )
                await anyio.to_thread.run_sync(victim._signal_foreground, master, group, signal.SIGKILL)
            with anyio.move_on_after(0.1) as changed_foreground:
                await anyio.wait_readable(pidfd)
            assert changed_foreground.cancel_called, "signal was sent after the foreground changed"

            await victim.aclose()

            with anyio.move_on_after(1) as deadline:
                await anyio.wait_readable(pidfd)
            assert not deadline.cancel_called, "owned pipeline member survived PTY cleanup"
            await peer.send(b"read -r answer; printf 'PEER_RESULT:%s\\n' \"$answer\"\nstill-interactive\n")
            with anyio.fail_after(8):
                await peer_result.wait()
            assert peer.process.poll() is None
            await peer.aclose()
            tasks.cancel_scope.cancel()
    finally:
        if pidfd is not None:
            stop_owned_process(pidfd)
            os.close(pidfd)
        with anyio.CancelScope(shield=True):
            await victim.aclose()
            await peer.aclose()
