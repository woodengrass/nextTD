"""Core navigation regression: genuine first Alt+o and hybrid mouse->keyboard.

The broker permission cache, the ``TDNEXT_NAVIGATOR_BROKER`` launcher flag,
and the Python fallback rename (``tdnext-header:settings@<tab>:<pane>``)
were removed with the Python host. Navigation is the core's
``OpenTabWithReturn`` / ``return-to-origin`` on the supported Rust desktop,
which keeps the bare header name on every route.

Behavior asserted on an isolated session (private ``tmp_path`` state dir,
unique Zellij socket dir, unique tagged tmux socket) with bounded waits:
1. the genuine first ``Alt+o`` after startup renders Settings and the
   header tab is active and bare; ``Escape`` returns to Files;
2. a mouse header click opens Settings and ``Escape`` returns; the next
   keyboard ``Alt+o``/``Escape`` round-trip succeeds with the header name
   still bare;
3. locked mode stays passthrough: ``Alt+o`` reaches the Files pane while
   locked instead of opening Settings.
"""

import subprocess
from pathlib import Path

import pytest
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import terminate_tagged
from native_session import NativeSession, wait_for


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


def close_session(session: NativeSession) -> None:
    try:
        session.stop_tmux()
    finally:
        terminate_tagged(session.tmux_name)


def header_tab_name(session: NativeSession) -> str:
    tabs = session.tabs()
    return next(str(tab["name"]) for tab in tabs if str(tab["name"]).startswith("tdnext-header:settings"))


def test_warmed_broker_first_shortcut_and_hybrid_mouse_keyboard(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        assert session.sessions() == {session.name}

        # 1. Genuine first Alt+o with no priming: Settings renders bare.
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        assert header_tab_name(session) == "tdnext-header:settings"
        session.send_key("Escape")
        visible(session, "Tool (Files)")

        # 2. Hybrid: mouse click opens, Escape returns, then keyboard
        #    Alt+o/Escape succeeds with the header name still bare.
        session.click_header()
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        assert header_tab_name(session) == "tdnext-header:settings"
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        assert header_tab_name(session) == "tdnext-header:settings"
        session.send_key("Escape")
        visible(session, "Tool (Files)")

        # 3. Locked mode stays passthrough.
        session.send_key("C-g")
        session.send_key("M-o")
        session.send_key("f")
        visible(session, "New file name")
        session.send_key("Escape")
        wait_for(lambda: "New file name" not in capture(session))
        session.send_key("C-g")
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
    finally:
        close_session(session)
