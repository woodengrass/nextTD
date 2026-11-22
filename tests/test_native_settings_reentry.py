import json
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


@pytest.mark.parametrize("route", ["header", "native-tab"])
def test_native_settings_refreshes_on_reentry_after_another_session_changes_appearance(
    tmp_path: Path, native_env: tuple[dict[str, str], str], route: str
) -> None:
    env, binary = native_env
    state_dir = Path(env["TDNEXT_STATE_DIR"])
    plugins = StateStore(state_dir / "plugins.json")
    state = plugins.read()
    plugins.write(PluginState(installed=state.installed | {"icons"}, enabled=state.enabled | {"icons"}))
    appearance = AppearanceStore(state_dir / "appearance.json")
    first = NativeSession(tmp_path, env, binary)
    try:
        second = NativeSession(tmp_path, env, binary)
        try:
            monitor = wait_for(first.btop_pid)
            first.open_settings()
            first.back_to("Files")
            second.open_settings()
            panes = json.loads(second.action("list-panes", "--all", "--json"))
            tool = next(item for item in panes if "panes tool settings" in (item.get("pane_command") or ""))
            pane_id = f"terminal_{tool['id']}"
            second.send_key("Enter")
            wait_for(lambda: (screen := second.action("dump-screen", "--pane-id", pane_id)).count("None (text)") >= 2
                     and "Codicons" in screen)
            second.send_key("Down")
            second.send_key("Enter")
            wait_for(lambda: appearance.read().provider_id == "icons")
            second.send_key("Tab")
            second.send_key("Enter")
            wait_for(lambda: "▲" in next(
                line for line in second.action("dump-screen", "--pane-id", pane_id).splitlines() if "Style:" in line
            ).split("Style:", 1)[1])
            second.send_key("Down")
            second.send_key("Enter")
            wait_for(lambda: appearance.read() == Appearance(provider_id="icons", icon_mode="codicons"))
            if route == "header":
                first.open_settings()
            else:
                first.action("go-to-tab-by-id", str(first.tab_id("tdnext-header:settings")))
                first.wait_for_settings()
            panes = json.loads(first.action("list-panes", "--all", "--json"))
            tool = next(item for item in panes if "panes tool settings" in (item.get("pane_command") or ""))
            first_pane = f"terminal_{tool['id']}"
            screen = wait_for(lambda: (text if "Provider: Codicons" in (text := first.action(
                "dump-screen", "--pane-id", first_pane
            )) and "Style: Codicons" in text else ""), timeout=3)
            assert "Provider: Codicons" in screen and "Style: Codicons" in screen
            assert first.btop_pid() == monitor
            content = next(item for item in panes if "tdnext-files" in (item.get("pane_command") or ""))
            assert "FOLDERS" in first.action("dump-screen", "--pane-id", f"terminal_{content['id']}")
        finally:
            second.close()
    finally:
        first.close()
