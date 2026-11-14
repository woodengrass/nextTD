"""Warmed-broker native regression: genuine first Alt+o and hybrid mouse->keyboard.

Provenance: mirrors the private isolated warmed proof (``live2.py`` in the
benchmark scratch) and the broker README session evidence, but as a
repeatable test that owns its isolation:

- The permission cache is seeded under the native fixture's own state dir
  (``<tmp>/state/zellij-cache/zellij/permissions.kdl``) with a synthetic
  entry for the exact bare asset path carrying the full grant set.
  No real grant, no user cache, no user session is touched: the
  session runs on its own socket dir and its own tagged tmux socket, and
  teardown kills only processes carrying this test's unique tag.
- The launcher flag is confirmed active by observing
  ``TDNEXT_NAVIGATOR_BROKER=1`` in the tagged session processes before any
  navigation is asserted.
- Behavior asserted is user-visible on the real terminal (via tmux
  capture-pane) with bounded waits and no sleeps:
  1. the genuine first ``Alt+o`` after startup, with no priming keypress,
     renders Settings; ``Escape`` returns to Files (fails against the
     first-key bug where the opening press is swallowed);
  2. a mouse header click opens Settings and ``Escape`` returns; the next
     keyboard ``Alt+o``/``Escape`` round-trip succeeds and the header tab
     carries the ``@<tab>:<pane>`` suffix (fails against the suffix bug
     where the rename never lands and the hybrid back mis-navigates);
  3. locked mode stays passthrough: ``Alt+o`` reaches the Files pane while
     locked instead of opening Settings.
"""

import os
import subprocess
from pathlib import Path

import pytest
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes, terminate_tagged
from native_session import NativeSession, wait_for

from tdnext.plugins.desktop import plugin as desktop_plugin

FULL_PERMS = (
    "    ReadApplicationState\n"
    "    ChangeApplicationState\n"
    "    Reconfigure\n"
    "    ReadCliPipes\n"
)


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


def seed_synthetic_warm_cache(tmp_path: Path) -> Path:
    """Seed the isolated test cache with the host's exact bare-path key."""
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None
    cache = tmp_path / "state" / "zellij-cache" / "zellij"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "permissions.kdl").write_text(
        f'"{asset}" {{\n{FULL_PERMS}}}\n',
        encoding="utf-8",
    )
    return asset


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


def launcher_flag_active(session: NativeSession) -> bool:
    """The launcher observed the synthetic warm cache and flagged the session."""
    marker = f"TDNEXT_TEST_SESSION={session.tmux_name}".encode()
    for _pid, environ, _command in tagged_processes(session.tmux_name):
        if marker not in environ:
            continue
        if b"TDNEXT_NAVIGATOR_BROKER=1" in environ:
            return True
    return False


def header_tab_name(session: NativeSession) -> str:
    tabs = session.tabs()
    return next(str(tab["name"]) for tab in tabs if str(tab["name"]).startswith("tdnext-header:settings"))


def test_warmed_broker_first_shortcut_and_hybrid_mouse_keyboard(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    seed_synthetic_warm_cache(tmp_path)
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")

        # Launcher proof: the synthetic warm cache flagged this session,
        # and the isolated socket shows no user session leaked in.
        wait_for(lambda: launcher_flag_active(session), timeout=15)
        assert session.sessions() == {session.name}

        # 1. Genuine first Alt+o with no priming: Settings renders.
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        wait_for(lambda: actions_finished(session))
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))

        # 2. Hybrid: mouse click opens, Escape returns, then keyboard
        #    Alt+o/Escape succeeds with the suffixed tab the back path needs.
        session.click_header()
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        wait_for(lambda: actions_finished(session))
        assert "@" in header_tab_name(session)
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))
        session.send_key("M-o")
        visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
        wait_for(lambda: actions_finished(session))
        suffixed = header_tab_name(session)
        assert suffixed.startswith("tdnext-header:settings@")
        assert ":terminal_" in suffixed or ":plugin_" in suffixed
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))

        # 3. Locked mode stays passthrough while warmed.
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
