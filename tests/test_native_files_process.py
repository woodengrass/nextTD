"""Runtime proof that the live desktop runs the Rust Files binary for its pane.

B1: The Files tab runs the expected ``tdnext-files`` binary for its own
pane ID in this tagged session (``/proc/PID/exe`` is the same file as the
expected binary, not an argv match), renders the Rust UI, answers ``f``
with ``New file name``, and closes it with ``Esc``. An argv-only decoy
named ``tdnext-files`` is rejected by the same exe check.
B2 (harness prerequisite, not product behavior): starting a session
without the built Files binary aborts with guidance.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import exe_matches_expected, pane_id_of, tagged_processes
from native_session import NativeSession, wait_for


def files_pane_id(session: NativeSession) -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(
        item for item in panes
        if "tdnext-files" in (item.get("pane_command") or "")
        and not item.get("is_plugin")
        and not item.get("is_floating")
    )
    return f"terminal_{pane['id']}"


def rust_files_pids_for_pane(tag: str, pane_id: str, expected_binary: str) -> list[int]:
    numeric = pane_id.removeprefix("terminal_")
    found: list[int] = []
    for pid, environ, _command in tagged_processes(tag):
        if pane_id_of(environ) != numeric:
            continue
        if exe_matches_expected(pid, expected_binary):
            found.append(pid)
    return found


def rust_files_pids_in_session(tag: str, expected_binary: str) -> list[int]:
    return [
        pid for pid, _environ, _command in tagged_processes(tag)
        if exe_matches_expected(pid, expected_binary)
    ]


def python_files_pids(tag: str) -> list[int]:
    found: list[int] = []
    for _pid, _environ, command in tagged_processes(tag):
        text = b" ".join(command)
        if b"tdnext.pane" in text and b"files" in text:
            found.append(1)
    return found


def test_files_pane_runs_expected_rust_binary_and_answers_file_dialog(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    from tdnext.core.models import PluginState
    from tdnext.core.state import StateStore

    env, binary = native_env
    ids = frozenset({"desktop", "files"})
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=1)
    decoy: subprocess.Popen[bytes] | None = None
    try:
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        commands = [pane.get("pane_command") or "" for pane in panes]
        assert any("tdnext-files" in command for command in commands), commands
        assert not any("tdnext.pane view files" in command for command in commands), commands
        assert python_files_pids(session.tmux_name) == []

        pane = files_pane_id(session)
        expected = session.files_binary
        pids = wait_for(lambda: rust_files_pids_for_pane(session.tmux_name, pane, expected) or None)
        assert len(pids) >= 1
        assert rust_files_pids_in_session(session.tmux_name, expected) == pids

        decoy_env = dict(os.environ)
        decoy_env["TDNEXT_TEST_SESSION"] = session.tmux_name
        decoy = subprocess.Popen(
            ["bash", "-c", f"exec -a tdnext-files {sys.executable} -u -c 'import time; time.sleep(30)'"],
            env=decoy_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        def decoy_argv() -> bytes | None:
            try:
                data = Path(f"/proc/{decoy.pid}/cmdline").read_bytes()
            except OSError:
                return None
            return data if b"tdnext-files" in data else None

        decoy_argv_value = wait_for(decoy_argv)
        assert b"tdnext-files" in decoy_argv_value, decoy_argv_value
        assert not exe_matches_expected(decoy.pid, expected)
        assert decoy.pid not in rust_files_pids_for_pane(session.tmux_name, pane, expected)
        assert decoy.pid not in rust_files_pids_in_session(session.tmux_name, expected)

        wait_for(lambda: "Files  /" in session.action("dump-screen", "--pane-id", pane))
        wait_for(lambda: "FILES" in session.action("dump-screen", "--pane-id", pane))
        wait_for(lambda: pane in session.action("list-clients"))
        assert session.active_tab() == session.tab_id("Files")
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane))
        session.send_key("Escape")
        wait_for(lambda: "New file name" not in session.action("dump-screen", "--pane-id", pane))
    finally:
        if decoy is not None:
            decoy.terminate()
            try:
                decoy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                decoy.kill()
        session.close()


def test_harness_aborts_when_files_binary_is_missing(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    """Harness prerequisite: the session drive needs the built Files binary."""
    import pytest

    env, binary = native_env
    env = dict(env)
    env["TDNEXT_FILES_BINARY"] = str(tmp_path / "missing-tdnext-files")
    with pytest.raises(RuntimeError, match="Rust Files binary is missing"):
        NativeSession(tmp_path, env, binary)
