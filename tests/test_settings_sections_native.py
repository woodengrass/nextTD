import json
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, assert_no_chrome_processes, assert_no_chrome_ptys, wait_for

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.runtime import manager_for


def open_general(session: NativeSession) -> str:
    session.send_key("M-o")
    target = session.tab_id("tdnext-header:settings")
    wait_for(lambda: session.active_tab() == target)
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    tool = next(pane for pane in panes if "tdnext.pane view settings" in (pane.get("pane_command") or ""))
    pane_id = f"terminal_{tool['id']}"
    wait_for(lambda: pane_id in session.action("list-clients"))
    wait_for(lambda: "Icon appearance" in session.action("dump-screen", "--pane-id", pane_id))
    return pane_id


def test_native_sections_return_to_files_without_restarting_btop(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        pane = open_general(session)
        session.send_key("BTab")
        session.send_key("Enter")
        wait_for(lambda: "Manage trusted local plugins" in session.action("dump-screen", "--pane-id", pane))
        session.send_key("BTab")
        session.send_key("BTab")
        session.send_key("Enter")
        wait_for(lambda: "Icon appearance" in session.action("dump-screen", "--pane-id", pane))
        session.back_to("Files")
        assert session.btop_pid() == monitor
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
    finally:
        session.close()


def test_native_settings_readiness_accepts_plugins_on_reentry(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        session.open_settings()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(pane for pane in panes if "tdnext.pane view settings" in (pane.get("pane_command") or ""))
        pane_id = f"terminal_{tool['id']}"
        assert "> General" in session.action("dump-screen", "--pane-id", pane_id)
        session.send_key("BTab")
        session.send_key("Enter")
        wait_for(lambda: "> Plugins" in session.action("dump-screen", "--pane-id", pane_id))
        session.back_to("Files")
        session.open_settings()
        assert "> Plugins" in session.action("dump-screen", "--pane-id", pane_id)
        session.back_to("Files")
        assert session.btop_pid() == monitor
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
    finally:
        session.close()


def test_native_sessions_refresh_appearance_without_changing_running_icons(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    state_dir = Path(env["TDNEXT_STATE_DIR"])
    manager_for(state_dir).enable("icons")
    appearance = AppearanceStore(state_dir / "appearance.json")
    first = NativeSession(tmp_path, env, binary)
    try:
        monitor = wait_for(first.btop_pid)
        open_general(first)
        first.back_to("Files")
        second = NativeSession(tmp_path, env, binary)
        try:
            pane = open_general(second)
            second.send_key("Enter")
            wait_for(lambda: "Codicons" in second.action("dump-screen", "--pane-id", pane))
            second.send_key("Down")
            second.send_key("Enter")
            wait_for(lambda: appearance.read().provider_id == "icons")
            second.send_key("Tab")
            second.send_key("Enter")
            wait_for(lambda: "▲" in next(
                line for line in second.action("dump-screen", "--pane-id", pane).splitlines() if "Style:" in line
            ).split("Style:", 1)[1])
            second.send_key("Down")
            second.send_key("Enter")
            wait_for(lambda: appearance.read() == Appearance(provider_id="icons", icon_mode="codicons"))
            pane = open_general(first)
            screen = wait_for(lambda: (text if "Provider: Codicons" in (text := first.action(
                "dump-screen", "--pane-id", pane
            )) and "Style: Codicons" in text else ""))
            assert "Provider: Codicons" in screen and "Style: Codicons" in screen
            assert first.btop_pid() == monitor
        finally:
            second.close()
    finally:
        first.close()


def test_native_shell_keeps_interactive_job_when_settings_opens(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    home = tmp_path / "home"
    home.mkdir()
    env.update({"HOME": str(home), "SHELL": "/bin/bash", "HISTFILE": "/dev/null", "INPUTRC": "/dev/null"})
    env.pop("BASH_ENV", None)
    ids = frozenset({"desktop", "files", "shell", "settings"})
    manager_for(tmp_path / "state").enable("shell")
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=3)
    try:
        session.send_key("M-2")
        wait_for(lambda: session.active_tab() == session.tab_id("Shell"))
        def shell_pane():
            panes = json.loads(session.action("list-panes", "--all", "--json"))
            return next(
                (item for item in panes if "tdnext.pane view shell" in (item.get("terminal_command") or "")), None
            )

        shell = wait_for(shell_pane)
        pane_id = f"terminal_{shell['id']}"
        session.send_key("printf 'NATIVE_%s\\n' WAIT; read -r answer; printf 'NATIVE_RESULT:%s\\n' \"$answer\"")
        session.send_key("Enter")
        wait_for(lambda: "NATIVE_WAIT" in session.action("dump-screen", "--pane-id", pane_id))
        open_general(session)
        session.back_to("Shell")
        session.send_key("hello")
        session.send_key("Enter")
        wait_for(lambda: "NATIVE_RESULT:hello" in session.action("dump-screen", "--pane-id", pane_id))
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
    finally:
        session.close()
