import json
import re
import subprocess
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def test_native_shell_interaction_and_settings_preserve_the_process(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    home = tmp_path / "home"
    home.mkdir()
    env.update({"HOME": str(home), "SHELL": "/bin/bash", "HISTFILE": str(home / "history"), "INPUTRC": "/dev/null"})
    env.pop("BASH_ENV", None)
    ids = frozenset({"desktop", "files", "shell", "settings"})
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=3)
    try:
        session.send_key("M-2")
        wait_for(lambda: session.active_tab() == session.tab_id("Shell"))
        def shell_pane():
            panes = json.loads(session.action("list-panes", "--all", "--json"))
            return next((item for item in panes if "tdnext.pane view shell" in (item.get("pane_command") or "")), None)

        pane = wait_for(shell_pane)
        pane_id = f"terminal_{pane['id']}"
        session.send_key("printf 'NATIVE_%s\\n' WAIT; read -r answer; printf 'NATIVE_RESULT:%s\\n' \"$answer\"")
        session.send_key("Enter")
        wait_for(lambda: "NATIVE_WAIT" in session.action("dump-screen", "--pane-id", pane_id))
        session.open_settings()
        session.back_to("Shell")
        session.send_key("hello")
        session.send_key("Enter")
        wait_for(lambda: "NATIVE_RESULT:hello" in session.action("dump-screen", "--pane-id", pane_id))
        subprocess.run(
            ["tmux", "-S", session.tmux_socket, "resize-window", "-t", session.tmux_name, "-x", "140", "-y", "40"],
            env=env, capture_output=True, check=True, timeout=5,
        )
        session.send_key("printf 'NATIVE_SIZE:'; stty size")
        session.send_key("Enter")
        wait_for(lambda: re.search(r"NATIVE_SIZE:\d+ \d+", session.action("dump-screen", "--pane-id", pane_id)))
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        resized = next(item for item in panes if item["id"] == pane["id"])
        screen = session.action("dump-screen", "--pane-id", pane_id)
        assert f"NATIVE_SIZE:{resized['pane_content_rows']} {resized['pane_content_columns']}" in screen
        session.send_key("exit")
        session.send_key("Enter")
        def shell_has_exited() -> bool:
            current = json.loads(session.action("list-panes", "--all", "--json"))
            item = next((candidate for candidate in current if candidate["id"] == pane["id"]), None)
            return item is None or item["exited"]

        wait_for(shell_has_exited)
        assert session.active_tab() == session.tab_id("Shell")
    finally:
        session.close()
