"""Close TDNext sessions by process name, then uninstall the plugin.

Runs as an independent process launched after the user confirms the
uninstall (CLI or Settings). The helper is detached, so it survives the
death of the calling session. Identification is by Linux process name only:
every own-uid process whose comm is exactly ``tdnext`` is a project-specific
process (the bundled modified Zellij binary is a real file named tdnext, the
tdnext CLI console script has comm tdnext, and the fallback desktop claims
the name at launch). Stock Zellij has comm zellij and is never matched.
The helper itself runs as ``python -m tdnext.core.uninstall_helper`` so its
comm is python and it self-excludes. The CLI may keep its own process alive to
report the result; a fallback desktop parent is closed. Discovery failure
aborts before any state changes instead of silently succeeding.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from tdnext.runtime import manager_for

from .errors import PluginError
from .lifecycle import commit_removal, prepare_removal
from .removal import RemovalRequest

PROCESS_NAME: Final = "tdnext"
_TERM_GRACE_SECONDS: Final = 2.0
_KILL_GRACE_SECONDS: Final = 2.0
_HELPER_TIMEOUT_SECONDS: Final = 120


class SurvivorsError(PluginError):
    def __init__(self, pids: tuple[int, ...]) -> None:
        super().__init__(f"TDNext processes still alive after SIGKILL: {', '.join(map(str, pids))}")
        self.pids = pids

    def __str__(self) -> str:
        return f"{self.detail}; uninstall aborted"


class ProcessDiscoveryError(PluginError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"cannot enumerate processes: {detail}")
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.detail}; uninstall aborted"


@contextmanager
def _already_closed(request: RemovalRequest) -> Generator[tuple[str, ...], None, None]:
    yield ()


def _warn(message: str) -> None:
    try:
        print(f"Warning: {message}", file=sys.stderr)
    except BrokenPipeError:
        pass


def list_tdnext_pids(*, proc: Path = Path("/proc"), exclude: frozenset[int] = frozenset()) -> tuple[int, ...]:
    mine = os.getuid()
    found: list[int] = []
    try:
        entries = tuple(proc.iterdir())
    except OSError as exc:
        raise ProcessDiscoveryError(str(exc)) from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid in exclude:
            continue
        try:
            if entry.stat().st_uid != mine:
                continue
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise ProcessDiscoveryError(f"cannot inspect process {pid}: {exc}") from exc
        if comm == PROCESS_NAME:
            found.append(pid)
    return tuple(found)


def _alive(pid: int, *, proc: Path) -> bool:
    entry = proc / str(pid)
    if not entry.exists():
        return False
    try:
        data = (entry / "stat").read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    end = data.rfind(b")")
    if end < 0 or end + 2 >= len(data):
        return True
    return data[end + 2 : end + 3] != b"Z"


def _wait_gone(pids: tuple[int, ...], timeout: float, *, proc: Path) -> tuple[int, ...]:
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _alive(pid, proc=proc)]
        if remaining:
            time.sleep(0.05)
    return tuple(remaining)


def close_pids(targets: tuple[int, ...], *, proc: Path = Path("/proc")) -> None:
    if not targets:
        return
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            _warn(f"cannot signal process {pid}: {exc}")
    remaining = _wait_gone(tuple(targets), _TERM_GRACE_SECONDS, proc=proc)
    if not remaining:
        return
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError as exc:
            _warn(f"cannot signal process {pid}: {exc}")
    survivors = _wait_gone(remaining, _KILL_GRACE_SECONDS, proc=proc)
    if survivors:
        raise SurvivorsError(pids=survivors)


def close_by_name(*, proc: Path = Path("/proc"), keep_parent: bool = False) -> None:
    exclude = {os.getpid()}
    if keep_parent:
        exclude.add(os.getppid())
    targets = list_tdnext_pids(proc=proc, exclude=frozenset(exclude))
    close_pids(targets, proc=proc)


def uninstall_with_close(
    *, state: Path, plugin_id: str, proc: Path = Path("/proc"), keep_parent: bool = False,
) -> frozenset[str]:
    manager = manager_for(state, removal_guard=_already_closed)
    with manager.code_store.locked():
        request, target = prepare_removal(manager, plugin_id)
        if keep_parent:
            close_by_name(proc=proc, keep_parent=True)
        else:
            close_by_name(proc=proc)
        return commit_removal(manager, request, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close TDNext sessions, then uninstall a plugin.")
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--plugin", required=True)
    parser.add_argument("--keep-parent", action="store_true")
    args = parser.parse_args(argv)
    try:
        affected = uninstall_with_close(state=args.state, plugin_id=args.plugin, keep_parent=args.keep_parent)
    except PluginError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        print(f"Uninstalled: {args.plugin}; Disabled: {', '.join(sorted(affected)) or '(already disabled)'}")
    except BrokenPipeError:
        pass
    return 0


def launch_uninstall_helper(state: Path, plugin_id: str, *, keep_parent: bool = False) -> str:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "tdnext.core.uninstall_helper",
             "--state", str(state), "--plugin", plugin_id, *(("--keep-parent",) if keep_parent else ())],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=_HELPER_TIMEOUT_SECONDS, start_new_session=True, check=False,
        )
    except OSError as exc:
        raise PluginError(f"cannot launch uninstall helper: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PluginError(f"uninstall helper timed out after {_HELPER_TIMEOUT_SECONDS}s") from exc
    if completed.returncode != 0:
        raise PluginError(completed.stderr.strip() or f"uninstall helper failed: {completed.returncode}")
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
