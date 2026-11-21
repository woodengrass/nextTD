import json
import sys
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore


def files_pane(session: NativeSession) -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(pane for pane in panes if "tdnext.pane view files" in (pane.get("pane_command") or ""))
    return f"terminal_{pane['id']}"


@pytest.mark.parametrize("x", [5, 110], ids=["non-action", "settings-action"])
def test_native_header_mouse_returns_focus_to_files_and_file_keys_work(
    tmp_path: Path, native_env: tuple[dict[str, str], str], x: int
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pane = files_pane(session)
        wait_for(lambda: pane in session.action("list-clients"))
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane))
        if x == 110:
            session.click_header(x=x)
            wait_for(lambda: session.active_tab() == session.tab_id("tdnext-header:settings"))
            session.back_to("Files")
        else:
            session.header_mouse(x, release=False)
            session.header_mouse(x, release=True)
            wait_for(lambda: pane in session.action("list-clients"))
            assert session.active_tab() == session.tab_id("Files")
        wait_for(lambda: pane in session.action("list-clients"), timeout=3)
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane))
        session.send_key("header-proof.txt")
        session.send_key("Enter")
        wait_for(lambda: (tmp_path / "header-proof.txt").is_file())
    finally:
        session.close()


@pytest.mark.parametrize("x", [5, 110], ids=["non-action", "active-action"])
def test_native_active_settings_header_click_keeps_escape_operable(
    tmp_path: Path, native_env: tuple[dict[str, str], str], x: int
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        session.open_settings()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(pane for pane in panes if "panes tool settings" in (pane.get("pane_command") or ""))
        wait_for(lambda: f"terminal_{tool['id']}" in session.action("list-clients"))
        session.header_mouse(x, release=False)
        session.header_mouse(x, release=True)
        wait_for(lambda: f"terminal_{tool['id']}" in session.action("list-clients"))
        assert session.active_tab() == session.tab_id("tdnext-header:settings")
        session.back_to("Files")
    finally:
        session.close()


@pytest.mark.parametrize("mouse", [False, True], ids=["keyboard", "header-click"])
def test_native_settings_returns_to_originating_sidebar_pane(
    tmp_path: Path, native_env: tuple[dict[str, str], str], mouse: bool
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pane = files_pane(session)
        wait_for(lambda: pane in session.action("list-clients"))
        session.mouse(60, 20, release=False)
        session.mouse(60, 20, release=True)
        wait_for(lambda: pane in session.action("list-clients"))
        if mouse:
            session.click_header()
            session.wait_for_settings()
        else:
            session.open_settings()
        session.back_to("Files")
        wait_for(lambda: pane in session.action("list-clients"))
    finally:
        session.close()


def test_native_shortcut_returns_to_originating_floating_pane(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pane_id = session.action("new-pane", "--floating", "--", sys.executable, "-c", "input()").strip()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        pane = next(item for item in panes if f"terminal_{item['id']}" == pane_id and not item["is_plugin"])
        session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=False)
        session.mouse(pane["pane_x"] + 2, pane["pane_y"] + 2, release=True)
        wait_for(lambda: pane_id in session.action("list-clients"))
        session.open_settings()
        session.back_to("Files")
        wait_for(lambda: pane_id in session.action("list-clients"))
    finally:
        session.close()


@pytest.mark.parametrize("monitor", [False, True], ids=["one-workspace", "two-workspaces"])
def test_native_numeric_shortcut_cannot_enter_header_tab(
    tmp_path: Path, native_env: tuple[dict[str, str], str], monitor: bool
) -> None:
    env, binary = native_env
    ids = frozenset({"desktop", "files", "settings"} | ({"monitor"} if monitor else set()))
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=3 if monitor else 2)
    try:
        pane = files_pane(session)
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane))
        session.send_key("M-3" if monitor else "M-2")
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane), timeout=3)
        assert session.active_tab() == session.tab_id("Files")
    finally:
        session.close()


def test_native_numeric_shortcuts_follow_three_sidebar_views(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    plugin = tmp_path / "state" / "plugins" / "third"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        'id = "third"\nname = "Third"\nversion = "0.1.0"\ndescription = "Third view"\n'
        'manifest_version = 1\napi_version = 1\nview_interfaces = ["tdnext.textual-view.v1"]\n'
        'entrypoint = "tdnext.plugins.files.plugin:FilesPlugin"\nrequires_plugins = ["desktop"]\n'
        'capabilities = ["desktop.view", "files.browse"]\nrequires_executables = []\n',
        encoding="utf-8",
    )
    ids = frozenset({"desktop", "files", "monitor", "settings", "third"})
    StateStore(tmp_path / "state" / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    session = NativeSession(tmp_path, env, binary, expected_tabs=4)
    try:
        target = session.tab_id("Third")
        session.send_key("M-3")
        wait_for(lambda: session.active_tab() == target)
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        third = next(pane for pane in panes if "tdnext.pane view third" in (pane.get("pane_command") or ""))
        pane_id = f"terminal_{third['id']}"
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane_id))
        session.send_key("M-4")
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane_id))
        assert session.active_tab() == target
    finally:
        session.close()


def test_native_locked_mode_passes_desktop_shortcuts_through(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pane_id = files_pane(session)
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane_id))
        session.send_key("C-g")
        session.send_key("M-o")
        session.send_key("f")
        wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane_id))
        assert session.active_tab() == session.tab_id("Files")
        session.send_key("Escape")
        wait_for(lambda: "New file name" not in session.action("dump-screen", "--pane-id", pane_id))
        session.send_key("C-g")
        session.open_settings()
    finally:
        session.close()
