"""Native end-to-end regression for the stage-3 chrome navigation integration.

Scope: real single-client settings open/back through the stage-3 core
only. The core agent owns the chrome layout (one content PTY per tab,
outer chrome with no PTYs, old labels kept); the desktop agent owns
wiring the stage binary into the launcher. These tests pin the
observable contract both agents must satisfy.

Harness: stock ``native_env`` fixture for the isolated tmp state, tmux
socket, and tagged sessions, plus ``NativeSession`` for the single-client
drive. The stage core comes from the explicit ``TDNEXT_CORE_BINARY``
override or the host's own resolution (repo-built ``state/bin/tdnext``
or the packaged binary) and must pass the
launcher capability gate (required commands plus chrome version 1); it
is installed as a real file into the isolated
``env['TDNEXT_STATE_DIR']/bin/tdnext`` so its Linux comm is tdnext.
Missing or incapable candidates fail instead of skipping.

Core-route proof (no mocks, no weakened assertions): the header tab keeps
its bare ``tdnext-header:settings`` name, no chrome helper process is
sampled while the settings window is open, and the session carries the
``TDNEXT_ZELLIJ`` pointer the navigation actions require.
Isolation proof: single-session tests assert the isolated socket shows
exactly this session; the two-session test asserts exactly both sessions.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from native_environment import capable_core_source, core_wanted_message, install_stage_core
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes
from native_session import (
    NativeSession,
    assert_no_chrome_processes,
    assert_no_chrome_ptys,
    chrome_header_text,
    wait_for,
)

ZELLIJ_ENV = "TDNEXT_ZELLIJ"
BARE_HEADER = "tdnext-header:settings"
SETTINGS_PARTS = ("Settings", "Back", "> General", "Provider:", "Style:")


@pytest.fixture
def core_native(
    native_env: tuple[dict[str, str], str],
) -> tuple[dict[str, str], str]:
    """Isolated session env launching on the stage-3 chrome core; fail when absent."""
    env, _stock = native_env
    source = capable_core_source()
    if source is None:
        pytest.fail(f"core test requires a capable core binary: {core_wanted_message()}")
    session_env = dict(env)
    session_env.pop(ZELLIJ_ENV, None)
    link = install_stage_core(Path(session_env["TDNEXT_STATE_DIR"]), source)
    return session_env, str(link)


def capture(session: NativeSession) -> str:
    return subprocess.run(
        ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
        env=session.env, capture_output=True, text=True, check=True, timeout=5,
    ).stdout


def header_tab_name(session: NativeSession) -> str:
    return next(str(tab["name"]) for tab in session.tabs() if str(tab["name"]).startswith(BARE_HEADER))


def core_flag_active(session: NativeSession) -> bool:
    """The session's own tagged processes carry the navigation binary pointer.

    ``TDNEXT_ZELLIJ`` is set by the launcher for the dedicated session and
    required by every navigation action, so its presence proves the session
    runs under the wired core instead of an unconfigured terminal.
    """
    marker = f"TDNEXT_TEST_SESSION={session.tmux_name}".encode()
    pointer = f"{ZELLIJ_ENV}=".encode()
    for _pid, environ, _command in tagged_processes(session.tmux_name):
        if marker not in environ:
            continue
        if any(entry.startswith(pointer) for entry in environ):
            return True
    return False


def settings_open(session: NativeSession, dest: int, sightings: list[str]) -> bool:
    """Settings visible on the header tab; sample the shortcut process each poll."""
    for _pid, _environ, command in tagged_processes(session.tmux_name):
        if b"tdnext.plugins.desktop.shortcut" in command:
            sightings.append(b" ".join(command).decode("utf-8", "replace"))
            break
    if session.active_tab() != dest:
        return False
    return all(part in capture(session) for part in SETTINGS_PARTS)


def test_core_keyboard_open_return_reopen_with_modal_escape_precedence(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        wait_for(lambda: core_flag_active(session))
        session.switch("Files")
        origin_clients = session.action("list-clients")
        session.send_key("M-o")
        dest = session.tab_id(BARE_HEADER)
        sightings: list[str] = []
        wait_for(lambda: settings_open(session, dest, sightings))
        assert header_tab_name(session) == BARE_HEADER
        assert not sightings
        session.send_key("Enter")
        wait_for(lambda: capture(session).count("None (text)") >= 2)
        session.send_key("Escape")
        wait_for(lambda: capture(session).count("None (text)") == 1 and session.active_tab() == dest)
        assert header_tab_name(session) == BARE_HEADER
        session.send_key("Escape")
        wait_for(lambda: session.active_tab() == session.tab_id("Files"))
        assert header_tab_name(session) == BARE_HEADER
        assert session.action("list-clients") == origin_clients
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
        session.send_key("M-o")
        wait_for(lambda: settings_open(session, dest, sightings))
        assert header_tab_name(session) == BARE_HEADER
        assert not sightings
        session.back_to("Files")
        assert session.active_tab() == session.tab_id("Files")
        assert session.action("list-clients") == origin_clients
    finally:
        session.close()


def test_core_mouse_open_back_proves_core_route(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        wait_for(lambda: core_flag_active(session))
        session.switch("Files")
        wait_for(lambda: "Settings" in chrome_header_text(session))
        assert_no_chrome_ptys(session)
        session.click_header()
        dest = session.tab_id(BARE_HEADER)
        sightings: list[str] = []
        wait_for(lambda: settings_open(session, dest, sightings))
        assert header_tab_name(session) == BARE_HEADER
        assert not sightings
        assert core_flag_active(session)
        session.back_to("Files")
        assert session.active_tab() == session.tab_id("Files")
        assert header_tab_name(session) == BARE_HEADER
    finally:
        session.close()


def test_core_header_without_action_preserves_origin_for_next_click(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        wait_for(lambda: core_flag_active(session))
        session.switch("Files")
        wait_for(lambda: "Settings" in chrome_header_text(session))
        origin = session.action("list-clients")
        session.click_header(x=10)
        wait_for(lambda: session.active_tab() == session.tab_id("Files"))
        session.click_header()
        session.wait_for_settings()
        session.back_to("Files")
        wait_for(lambda: session.action("list-clients") == origin)
    finally:
        session.close()


def test_core_other_isolated_session_unaffected(
    tmp_path: Path, core_native: tuple[dict[str, str], str]
) -> None:
    env, core = core_native
    first = NativeSession(tmp_path, env, core)
    try:
        second = NativeSession(tmp_path, env, core)
        try:
            assert first.name != second.name
            assert second.sessions() == {first.name, second.name}
            monitor = wait_for(first.btop_pid)
            first.switch("Monitor")
            second.switch("Files")
            first.send_key("M-o")
            dest = first.tab_id(BARE_HEADER)
            sightings: list[str] = []
            wait_for(lambda: settings_open(first, dest, sightings))
            assert header_tab_name(first) == BARE_HEADER
            assert not sightings
            assert second.active_tab() == second.tab_id("Files")
            assert header_tab_name(second) == BARE_HEADER
            first.back_to("Monitor")
            assert first.active_tab() == first.tab_id("Monitor")
            assert header_tab_name(first) == BARE_HEADER
            assert first.btop_pid() == monitor
            assert second.active_tab() == second.tab_id("Files")
            assert header_tab_name(second) == BARE_HEADER
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.parametrize("via_mouse", [False, True], ids=["keyboard", "header-click"])
def test_core_floating_origin_returns_to_floating_pane(
    tmp_path: Path, core_native: tuple[dict[str, str], str], via_mouse: bool
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        wait_for(lambda: core_flag_active(session))
        session.switch("Files")
        origin = session.action("new-pane", "--floating", "--", sys.executable, "-c", "input()").strip()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        floating = next(item for item in panes if f"terminal_{item['id']}" == origin and not item["is_plugin"])
        session.mouse(floating["pane_x"] + 2, floating["pane_y"] + 2, release=False)
        session.mouse(floating["pane_x"] + 2, floating["pane_y"] + 2, release=True)
        wait_for(lambda: origin in session.action("list-clients"))
        if via_mouse:
            wait_for(lambda: "Settings" in chrome_header_text(session))
            session.click_header()
        else:
            session.send_key("M-o")
        dest = session.tab_id(BARE_HEADER)
        sightings: list[str] = []
        wait_for(lambda: settings_open(session, dest, sightings))
        assert header_tab_name(session) == BARE_HEADER
        assert not sightings
        session.back_to("Files")
        wait_for(lambda: origin in session.action("list-clients"))
    finally:
        session.close()
