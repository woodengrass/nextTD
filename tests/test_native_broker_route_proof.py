"""Core-route proof: genuine Alt+o uses the Rust core, not a fallback.

Navigation is the core's ``OpenTabWithReturn`` (``Alt+o``) and
``return-to-origin`` (``Escape``) through the supported Rust desktop.

Route signals (observed on this test's own isolated session, bounded
``wait_for`` only, no sleeps):
- Positive proof: Settings renders AND the header tab is active.
- Core signature: the header tab keeps its bare ``tdnext-header:settings``
  name (the core switches by stable id and never renames) and no shortcut
  fallback process is sampled while the settings window is open.
"""

import os
import subprocess
from pathlib import Path

import pytest
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes, terminate_tagged
from native_session import NativeSession, wait_for

SETTINGS_PARTS = ("Settings", "Back", "> General", "Provider:", "Style:")
BARE_HEADER = "tdnext-header:settings"


@pytest.fixture
def isolated_native(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> tuple[dict[str, str], str]:
    base, binary = native_env
    excluded = {
        "BASH_ENV", "ENV", "TMUX", "TMUX_PANE", "TDNEXT_SESSION_SNAPSHOT", "TDNEXT_PATH_BOOTSTRAPPED",
        "TDNEXT_ORIGINAL_PATH", "TDNEXT_ORIGINAL_PATH_PRESENT",
    }
    env = {key: value for key, value in base.items() if key not in excluded}
    home = tmp_path / "home"
    home.mkdir()
    env.update({
        "HOME": str(home),
        "HISTFILE": str(home / "history"),
        "INPUTRC": "/dev/null",
        "SHELL": "/bin/bash",
        "TDNEXT_TEST_EXPECTED_PATH": env["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env, binary


def capture(session: NativeSession) -> str:
    return subprocess.run(
        ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
        env=session.env, capture_output=True, text=True, check=True, timeout=5,
    ).stdout


def visible(session: NativeSession, *expected: str) -> str:
    def screen() -> str | None:
        text = capture(session)
        return text if all(part in text for part in expected) else None

    try:
        return wait_for(screen, timeout=20)
    except AssertionError as exc:
        raise AssertionError(f"Expected {expected!r} on the terminal; screen:\n{capture(session)}") from exc


def actions_finished(session: NativeSession) -> bool:
    for _pid, _environ, command in tagged_processes(session.tmux_name):
        if b"open-header" in command or b"tdnext.plugins.desktop.shortcut" in command:
            return False
        if len(command) > 1 and Path(os.fsdecode(command[0])).name == "zellij" and command[1] == b"action":
            return False
    return True


def close_session(session: NativeSession) -> None:
    try:
        session.stop_tmux()
    finally:
        terminate_tagged(session.tmux_name)


def header_tab_name(session: NativeSession) -> str:
    tabs = session.tabs()
    return next(str(tab["name"]) for tab in tabs if str(tab["name"]).startswith(BARE_HEADER))


def opened_via_settings(session: NativeSession, dest: int, sightings: list[str]) -> bool:
    """Settings proof plus fallback-process sampling for one poll."""
    for _pid, _environ, command in tagged_processes(session.tmux_name):
        if b"tdnext.plugins.desktop.shortcut" in command:
            sightings.append(b" ".join(command).decode("utf-8", "replace"))
            break
    if session.active_tab() != dest:
        return False
    return all(part in capture(session) for part in SETTINGS_PARTS)


def test_core_alt_o_uses_core_route_without_fallback(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        assert session.sessions() == {session.name}

        files = session.tab_id("Files")
        assert session.active_tab() == files
        assert header_tab_name(session) == BARE_HEADER

        dest = session.tab_id(BARE_HEADER)
        sightings: list[str] = []
        session.send_key("M-o")
        try:
            wait_for(lambda _dest=dest, _seen=sightings: opened_via_settings(session, _dest, _seen))
        except AssertionError as exc:
            raise AssertionError(
                f"Alt+o never rendered Settings on the header tab; screen:\n{capture(session)}"
            ) from exc
        wait_for(lambda: actions_finished(session))
        assert header_tab_name(session) == BARE_HEADER, "core route never renames the header tab"
        assert not sightings, f"fallback process ran during navigation: {sightings}"
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))
        assert session.active_tab() == files
        assert header_tab_name(session) == BARE_HEADER
    finally:
        close_session(session)
