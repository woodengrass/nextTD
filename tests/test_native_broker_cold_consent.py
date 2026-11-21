"""Cold-start consent regression (broker removed): first Alt+o opens Settings directly.

The WASM navigator broker and its Zellij permission cache were removed from
the product. A cold session therefore shows no host prompt
(``asks permission to:`` / ``Allow?`` / ``(y/n)``) and seeds no permissions
cache. What survives from the old cold-consent contract is the user-visible
behavior: the genuine first ``Alt+o`` renders Settings, ``Escape`` returns
to Files, and Files-pane input is unaffected -- all on an isolated session
(private ``tmp_path`` state dir, unique Zellij socket dir, unique tagged
tmux socket; teardown kills only this test's tagged processes).
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
        "BASH_ENV",
        "ENV",
        "TMUX",
        "TMUX_PANE",
        "TDNEXT_SESSION_SNAPSHOT",
        "TDNEXT_PATH_BOOTSTRAPPED",
        "TDNEXT_ORIGINAL_PATH",
        "TDNEXT_ORIGINAL_PATH_PRESENT",
    }
    env = {key: value for key, value in base.items() if key not in excluded}
    home = tmp_path / "home"
    home.mkdir()
    env.update(
        {
            "HOME": str(home),
            "HISTFILE": str(home / "history"),
            "INPUTRC": "/dev/null",
            "SHELL": "/bin/bash",
            "TDNEXT_TEST_EXPECTED_PATH": env["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env, binary


def capture(session: NativeSession) -> str:
    return subprocess.run(
        ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-t", session.tmux_name],
        env=session.env,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout


def visible(session: NativeSession, *expected: str) -> str:
    def screen() -> str | None:
        text = capture(session)
        return text if all(part in text for part in expected) else None

    try:
        return wait_for(screen, timeout=20)
    except AssertionError as exc:
        raise AssertionError(f"Expected {expected!r} on the terminal; screen:\n{capture(session)}") from exc


def absent(session: NativeSession, *unexpected: str, timeout: float = 5) -> None:
    def screen() -> bool:
        text = capture(session)
        return all(part not in text for part in unexpected)

    try:
        wait_for(screen, timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"Expected {unexpected!r} to stay absent; screen:\n{capture(session)}") from exc


def close_session(session: NativeSession) -> None:
    try:
        session.stop_tmux()
    finally:
        terminate_tagged(session.tmux_name)


def header_is_bare(session: NativeSession) -> bool:
    return next(
        str(tab["name"]) for tab in session.tabs() if str(tab["name"]).startswith("tdnext-header:settings")
    ) == "tdnext-header:settings"


def files_input_works(session: NativeSession) -> None:
    session.send_key("f")
    visible(session, "New file name")
    session.send_key("Escape")
    wait_for(lambda: "New file name" not in capture(session))


def test_cold_consent_grant_keeps_legacy_shortcut(tmp_path: Path, isolated_native: tuple[dict[str, str], str]) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        assert session.sessions() == {session.name}
        # No broker prompt on a cold session: first Alt+o goes to Settings.
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        absent(session, "asks permission to:", "Allow?", "(y/n)")
        assert header_is_bare(session)
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        files_input_works(session)
    finally:
        close_session(session)


def test_cold_consent_denial_keeps_legacy_shortcut(tmp_path: Path, isolated_native: tuple[dict[str, str], str]) -> None:
    env, binary = isolated_native
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        assert session.sessions() == {session.name}
        # Mouse path also reaches Settings directly with no host prompt.
        session.click_header()
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        absent(session, "asks permission to:", "Allow?", "(y/n)")
        assert header_is_bare(session)
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        files_input_works(session)
    finally:
        close_session(session)
