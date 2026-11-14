"""Warmed-broker route proof: genuine Alt+o must use the WASM broker, not the Python fallback.

Why this exists: ``test_native_broker_warmed_regression`` checks the launcher
env flag plus visible Settings, but the static config always keeps a Python
``Run ... shortcut`` Alt+o binding. Settings can therefore render (with an
``@<tab>:<pane>`` suffix rename) while the broker never rebinds and the
fallback serves every press; that test passes vacuously in that state.

Route signals (all observed on this test's own isolated session, bounded
``wait_for`` only, no sleeps):
- Positive proof every round: Settings renders AND the header tab is active.
  Without this, "no rename" could mean the press was swallowed.
- Fallback signature: a transient ``tdnext.plugins.desktop.shortcut``
  process appears during the open window, and the header tab is renamed to
  the ``@`` suffix form. The Python path always renames when origin and
  destination differ; the broker path switches by stable id and never
  renames.
- Broker signature: Settings plus the active header tab with the header
  name still bare and no shortcut process sampled during the window.

Readiness: the broker pane is a 1-row pane whose screen dump is empty and
the host may never deliver the confirming keybinds event, so no READY screen
signal is awaited. Instead the genuine press is retried a bounded number of
times: a slow rebind shows fallback-then-broker across rounds, while a
never-rebind shows the fallback signature every round and fails with the
observed signatures attached. At most one new test session exists at a time
and teardown kills only processes carrying this test's unique tag.
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

SETTINGS_PARTS = ("Settings", "Back", "> General", "Provider:", "Style:")

BARE_HEADER = "tdnext-header:settings"

MAX_PRESS_ROUNDS = 3


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
    return next(str(tab["name"]) for tab in tabs if str(tab["name"]).startswith(BARE_HEADER))


def opened_via_settings(session: NativeSession, dest: int, sightings: list[str]) -> bool:
    """Settings proof plus event-based fallback-process sampling for one poll."""
    for _pid, _environ, command in tagged_processes(session.tmux_name):
        if b"tdnext.plugins.desktop.shortcut" in command:
            sightings.append(b" ".join(command).decode("utf-8", "replace"))
            break
    if session.active_tab() != dest:
        return False
    return all(part in capture(session) for part in SETTINGS_PARTS)


def reset_header_name(session: NativeSession) -> None:
    """Restore the bare header name after a fallback round renamed it."""
    current = header_tab_name(session)
    if "@" not in current:
        return
    tab_id = session.tab_id(BARE_HEADER)
    session.action("rename-tab-by-id", str(tab_id), BARE_HEADER)
    wait_for(lambda: header_tab_name(session) == BARE_HEADER)


def test_warmed_alt_o_uses_wasm_broker_not_python_fallback(
    tmp_path: Path, isolated_native: tuple[dict[str, str], str]
) -> None:
    env, binary = isolated_native
    seed_synthetic_warm_cache(tmp_path)
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")

        # Warmed proof, same as the regression test: the synthetic warm cache
        # flagged this session, and the isolated socket shows no user session.
        wait_for(lambda: launcher_flag_active(session), timeout=15)
        assert session.sessions() == {session.name}

        # Clean start so a fallback cannot pass vacuously: Files is active
        # (origin != destination, so the Python path must rename) and the
        # header name is bare (no leftover suffix to confuse the signal).
        files = session.tab_id("Files")
        assert session.active_tab() == files
        assert header_tab_name(session) == BARE_HEADER

        attempts: list[str] = []
        for _ in range(MAX_PRESS_ROUNDS):
            wait_for(lambda: session.active_tab() == files)
            reset_header_name(session)
            dest = session.tab_id(BARE_HEADER)
            sightings: list[str] = []
            session.send_key("M-o")
            try:
                wait_for(lambda _dest=dest, _seen=sightings: opened_via_settings(session, _dest, _seen))
            except AssertionError as exc:
                raise AssertionError(
                    f"Warmed Alt+o never rendered Settings on the header tab; screen:\n{capture(session)}"
                ) from exc
            wait_for(lambda: actions_finished(session))
            name = header_tab_name(session)
            if "@" not in name and not sightings:
                # Broker route proven: navigation happened (Settings + active
                # header tab) with no rename and no fallback process.
                session.send_key("Escape")
                visible(session, "Tool (Files)")
                wait_for(lambda: actions_finished(session))
                assert session.active_tab() == files
                assert header_tab_name(session) == BARE_HEADER
                return
            attempts.append(f"header={name!r} fallback_process={sightings[:1]}")
            session.send_key("Escape")
            visible(session, "Tool (Files)")
            wait_for(lambda: actions_finished(session))
        raise AssertionError(
            "Warmed Alt+o used the Python fallback in every round "
            f"(expected the bare {BARE_HEADER!r} with no shortcut process): {attempts}"
        )
    finally:
        close_session(session)
