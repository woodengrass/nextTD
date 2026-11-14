import json
import sys
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def test_native_settings_keyboard_selects_provider_and_style(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    state = Path(env["TDNEXT_STATE_DIR"])
    store = StateStore(state / "plugins.json")
    initial = store.read()
    store.write(PluginState(installed=initial.installed | {"icons"}, enabled=initial.enabled | {"icons"}))
    session = NativeSession(tmp_path, env, binary)
    try:
        session.open_settings()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(p for p in panes if "panes tool settings" in (p.get("pane_command") or ""))
        pane_id = f"terminal_{tool['id']}"
        session.send_key("Enter")
        appearance = AppearanceStore(state / "appearance.json")
        wait_for(lambda: (screen := session.action("dump-screen", "--pane-id", pane_id)).count("None (text)") >= 2
                 and "Codicons" in screen)
        session.send_key("Down")
        session.send_key("Enter")
        wait_for(lambda: appearance.read().provider_id == "icons")
        session.send_key("Tab")
        session.send_key("Enter")
        wait_for(lambda: "▲" in next(
            line for line in session.action("dump-screen", "--pane-id", pane_id).splitlines() if "Style:" in line
        ).split("Style:", 1)[1])
        session.send_key("Down")
        session.send_key("Enter")
        wait_for(lambda: appearance.read() == Appearance(provider_id="icons", icon_mode="codicons"))
        session.back_to("Files")
    finally:
        session.close()


def test_native_panes_keep_their_own_icon_snapshot_after_settings_change(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    state = Path(env["TDNEXT_STATE_DIR"])
    store = StateStore(state / "plugins.json")
    initial = store.read()
    store.write(PluginState(installed=initial.installed | {"icons"}, enabled=initial.enabled | {"icons"}))
    appearance = AppearanceStore(state / "appearance.json")
    appearance.write(Appearance(provider_id="icons", icon_mode="codicons"))
    first = NativeSession(tmp_path, env, binary)
    try:
        panes = json.loads(first.action("list-panes", "--all", "--json"))
        sidebar = next(p for p in panes if "panes sidebar files" in (p.get("pane_command") or ""))
        command = sidebar["pane_command"].split()
        snapshot = command[command.index("--snapshot") + 1]
        wait_for(lambda: "\uea83" in first.action("dump-screen", "--pane-id", f"terminal_{sidebar['id']}"))
        monitor = wait_for(first.btop_pid)
        appearance.write(Appearance(provider_id="icons", icon_mode="compatibility"))
        store.write(initial)
        first.open_settings()
        first.back_to("Files")
        pane = first.action(
            "new-pane", "--floating", "--", sys.executable, "-m", "tdnext.plugins.desktop.panes", "sidebar", "files",
            "--snapshot", snapshot,
        ).strip()
        wait_for(lambda: "\uea83" in first.action("dump-screen", "--pane-id", pane))
        assert first.btop_pid() == monitor
        second = NativeSession(tmp_path, env, binary)
        try:
            panes = json.loads(second.action("list-panes", "--all", "--json"))
            sidebar = next(p for p in panes if "panes sidebar files" in (p.get("pane_command") or ""))
            screen = wait_for(lambda: (output if "Files" in (output := second.action(
                "dump-screen", "--pane-id", f"terminal_{sidebar['id']}"
            )) else ""))
            assert "▤" in screen
            assert "\uea83" not in screen
        finally:
            second.close()
    finally:
        first.close()
