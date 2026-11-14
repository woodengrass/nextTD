"""Cold-broker consent native regression: host prompt, mouse focus, y/n in isolation.

Each test owns a fully isolated native session (private ``tmp_path`` state
dir, unique Zellij socket dir, unique tagged tmux socket; teardown kills
only processes carrying this test's unique tag via ``terminate_tagged``).
No user sessions, no VM, no broad kill, no installs.

Cold means the permission cache starts absent, so the Zellij host shows its
own permission prompt for the preloaded 12-row broker pane. The ``y``/``n``
keypresses below are synthetic test authorizations delivered to the
isolated session's own prompt only -- not real user grants -- and each
decision runs in its own separate session.

Behavior asserted on the real terminal (tmux capture-pane, bounded waits,
capture-on-failure, no fixed sleeps):
1. the actual Zellij host prompt is shown (``asks permission to:`` /
   ``Allow?`` / ``(y/n)``) with no pre-granted cache;
2. the prompt is focused via mouse on the broker pane before answering;
3. after the decision the host prompt and the 12-row broker pane are gone;
4. the permissions cache holds a full grant only after ``y``; after ``n``
   it holds no granted permissions;
5. in the same session the legacy ``Alt+o``/``Escape`` round-trip still
   works and Files-pane user input is unaffected.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes, terminate_tagged
from native_session import NativeSession, wait_for

from tdnext.plugins.desktop import plugin as desktop_plugin


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


def absent(session: NativeSession, *unexpected: str, timeout: float = 20) -> None:
    def screen() -> bool:
        text = capture(session)
        return all(part not in text for part in unexpected)

    try:
        wait_for(screen, timeout=timeout)
    except AssertionError as exc:
        raise AssertionError(f"Expected {unexpected!r} to be gone; screen:\n{capture(session)}") from exc


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


def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "zellij-cache" / "zellij" / "permissions.kdl"


def broker_panes(session: NativeSession) -> list[dict]:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    return [
        pane
        for pane in panes
        if pane.get("is_plugin") and not pane.get("is_floating") and "navigator-broker" in str(pane.get("title") or "")
    ]


def wait_for_broker_pane(session: NativeSession) -> dict:
    try:
        return wait_for(lambda: next(iter(broker_panes(session)), None), timeout=20)
    except AssertionError as exc:
        panes = session.action("list-panes", "--all", "--json")
        raise AssertionError(f"Cold broker pane never appeared; panes={panes!r}, screen:\n{capture(session)}") from exc


def wait_for_no_broker_pane(session: NativeSession) -> None:
    try:
        wait_for(lambda: not broker_panes(session), timeout=20)
    except AssertionError as exc:
        panes = session.action("list-panes", "--all", "--json")
        raise AssertionError(
            f"Broker pane did not close after consent decision; panes={panes!r}, screen:\n{capture(session)}"
        ) from exc


def focus_broker_pane(session: NativeSession, broker: dict) -> None:
    pane_id = f"plugin_{broker['id']}"
    session.mouse(int(broker["pane_x"]) + 2, int(broker["pane_y"]) + 2, release=False)
    session.mouse(int(broker["pane_x"]) + 2, int(broker["pane_y"]) + 2, release=True)
    try:
        wait_for(lambda: pane_id in session.action("list-clients"), timeout=15)
    except AssertionError as exc:
        raise AssertionError(
            f"Broker pane {pane_id} did not take focus; clients={session.action('list-clients')!r}, "
            f"screen:\n{capture(session)}"
        ) from exc


def has_full_grant(tmp_path: Path, asset: Path) -> bool:
    try:
        text = cache_path(tmp_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    entries = desktop_plugin._parse_permission_cache(text)
    if entries is None:
        return False
    granted = entries.get(str(asset))
    return granted is not None and all(perm in granted for perm in desktop_plugin.BROKER_PERMISSIONS)


def wait_for_grant(tmp_path: Path, asset: Path, *, expected: bool) -> None:
    try:
        wait_for(lambda: has_full_grant(tmp_path, asset) is expected, timeout=20)
    except AssertionError as exc:
        body = cache_path(tmp_path).read_text(encoding="utf-8") if cache_path(tmp_path).exists() else "<absent>"
        raise AssertionError(f"Grant state did not become {expected}; cache:\n{body}") from exc


def legacy_shortcut_still_works(session: NativeSession) -> None:
    session.send_key("M-o")
    visible(session, "Settings", "Back", "> General", "Provider:", "Style:")
    wait_for(lambda: actions_finished(session))
    session.send_key("Escape")
    visible(session, "Tool (Files)")
    wait_for(lambda: actions_finished(session))
    session.send_key("f")
    visible(session, "New file name")
    session.send_key("Escape")
    wait_for(lambda: "New file name" not in capture(session))


def run_consent_decision(tmp_path: Path, env: dict[str, str], binary: str, decision: str) -> None:
    assert decision in {"y", "n"}
    asset = desktop_plugin.broker_asset_path()
    assert asset is not None, "bundled broker WASM missing; cannot exercise cold consent"
    assert not cache_path(tmp_path).exists(), "cold session must start with no permission cache"
    session = NativeSession(tmp_path, env, binary)
    try:
        visible(session, "Tool (Files)")
        assert session.sessions() == {session.name}

        # Genuine first Alt+o with no priming: the legacy route navigates
        # to the header tab, where the host shows its own permission
        # prompt for the preloaded cold broker pane.
        session.send_key("M-o")
        visible(session, "asks permission to:", "Allow?", "(y/n)")
        assert not has_full_grant(tmp_path, asset)

        # Cold broker pane is the reachable 12-row prompt surface,
        # identified by its bundled WASM title.
        broker = wait_for_broker_pane(session)
        assert broker["pane_rows"] == 12, f"cold broker pane must be 12 rows: {broker!r}"

        # Focus the host prompt via mouse, then answer with the
        # synthetic test authorization for this isolated session only.
        focus_broker_pane(session, broker)
        visible(session, "asks permission to:", "Allow?", "(y/n)")
        session.send_key(decision)

        # Host prompt and 12-row broker pane are removed after the decision.
        absent(session, "asks permission to:", "Allow?", "(y/n)")
        wait_for_no_broker_pane(session)

        # Cache holds a full grant only after y; n grants nothing.
        wait_for_grant(tmp_path, asset, expected=(decision == "y"))
        if decision == "n":
            assert not has_full_grant(tmp_path, asset)

        # Same-session legacy route stays usable; user input unaffected.
        # The decision leaves us on the header tab with focus stolen by
        # the closed broker pane, so refocus the Settings tool pane first.
        tool = next(
            pane
            for pane in json.loads(session.action("list-panes", "--all", "--json"))
            if "panes tool settings" in (pane.get("pane_command") or "")
        )
        tool_id = f"terminal_{tool['id']}"
        session.mouse(int(tool["pane_x"]) + 2, int(tool["pane_y"]) + 2, release=False)
        session.mouse(int(tool["pane_x"]) + 2, int(tool["pane_y"]) + 2, release=True)
        try:
            wait_for(lambda: tool_id in session.action("list-clients"), timeout=15)
        except AssertionError as exc:
            raise AssertionError(
                f"Settings tool pane {tool_id} did not take focus after consent; "
                f"clients={session.action('list-clients')!r}, screen:\n{capture(session)}"
            ) from exc
        session.send_key("Escape")
        visible(session, "Tool (Files)")
        wait_for(lambda: actions_finished(session))
        legacy_shortcut_still_works(session)
    finally:
        close_session(session)


def test_cold_consent_grant_keeps_legacy_shortcut(tmp_path: Path, isolated_native: tuple[dict[str, str], str]) -> None:
    env, binary = isolated_native
    run_consent_decision(tmp_path, env, binary, "y")


def test_cold_consent_denial_keeps_legacy_shortcut(tmp_path: Path, isolated_native: tuple[dict[str, str], str]) -> None:
    env, binary = isolated_native
    run_consent_decision(tmp_path, env, binary, "n")
