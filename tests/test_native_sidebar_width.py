"""Native layout stability: single content pane per tab on the Rust desktop.

The old multi-pane tab (header/sidebar/tool/footer panes with a 22/10
sidebar toggle via ``set-pane-fixed-width``) is gone: the supported Rust
desktop renders one content pane per tab and the ``F7`` sidebar toggle no
longer exists. What survives is layout stability: user-added splits keep
their processes and geometry across Settings navigation, and the unbound
``F7`` key leaves every tile untouched.
"""

import json
from pathlib import Path
from typing import TypedDict

from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes
from native_session import NativeSession, wait_for


class Tile(TypedDict):
    tab: int
    x: int
    y: int
    cols: int
    rows: int
    cmd: str | None


def tiles(session: NativeSession) -> dict[int, Tile]:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    return {
        int(pane["id"]): {
            "tab": int(pane.get("tab_id") or 0),
            "x": int(pane.get("pane_x") or 0),
            "y": int(pane.get("pane_y") or 0),
            "cols": int(pane.get("pane_columns") or 0),
            "rows": int(pane.get("pane_rows") or 0),
            "cmd": pane.get("pane_command"),
        }
        for pane in panes
        if not pane.get("is_plugin") and not pane.get("is_floating")
    }


def sleep_pids(session: NativeSession) -> list[int]:
    marker = f"TDNEXT_TEST_SESSION={session.tmux_name}".encode()
    found = []
    for pid, environ, command in tagged_processes(session.tmux_name):
        if marker not in environ:
            continue
        text = b" ".join(command)
        if b"sleep" in text and b"1000" in text:
            found.append(pid)
    return sorted(found)


def files_tiles(session: NativeSession) -> dict[int, Tile]:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    return {
        int(pane["id"]): {
            "tab": int(pane.get("tab_id") or 0),
            "x": int(pane.get("pane_x") or 0),
            "y": int(pane.get("pane_y") or 0),
            "cols": int(pane.get("pane_columns") or 0),
            "rows": int(pane.get("pane_rows") or 0),
            "cmd": pane.get("pane_command"),
        }
        for pane in panes
        if not pane.get("is_plugin") and not pane.get("is_floating") and pane.get("tab_name") == "Files"
    }


def content_pane(session: NativeSession, tab: str) -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(
        item for item in panes
        if not item.get("is_plugin") and not item.get("is_floating") and item.get("tab_name") == tab
        and (item.get("pane_command") or "").startswith(session.env["TDNEXT_PYTHON"])
    )
    return f"terminal_{pane['id']}"


def test_user_splits_survive_settings_navigation(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, core = native_env
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        session.switch("Files")
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        wait_for(lambda: len(files_tiles(session)) == 2 or None)
        wait_for(lambda: any(tile["cmd"] == "/usr/bin/btop" for tile in tiles(session).values()) or None)
        before = tiles(session)
        pids_before = wait_for(lambda: sleep_pids(session) or None)
        assert len(pids_before) == 1
        session.open_settings()
        session.back_to("Files")
        after = tiles(session)
        assert set(after) == set(before)
        for pane_id, geom in after.items():
            assert geom["cmd"] == before[pane_id]["cmd"], f"pane terminal_{pane_id} changed command"
            assert geom["tab"] == before[pane_id]["tab"], f"pane terminal_{pane_id} changed tab"
        assert sleep_pids(session) == pids_before
    finally:
        session.close()


def test_unbound_sidebar_key_leaves_layout_unchanged(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, core = native_env
    session = NativeSession(tmp_path, env, core)
    try:
        session.switch("Files")
        content = content_pane(session, "Files")
        wait_for(lambda: content in session.action("list-clients"))
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        wait_for(lambda: len(files_tiles(session)) == 2 or None)
        wait_for(lambda: any(tile["cmd"] == "/usr/bin/btop" for tile in tiles(session).values()) or None)
        before = tiles(session)
        pids_before = wait_for(lambda: sleep_pids(session) or None)
        session.send_key("F7")
        wait_for(lambda: session.active_tab() == session.tab_id("Files"))
        toggled = tiles(session)
        assert set(toggled) == set(before)
        assert sleep_pids(session) == pids_before
        session.send_key("F7")
        wait_for(lambda: session.active_tab() == session.tab_id("Files"))
        assert tiles(session) == before
        assert sleep_pids(session) == pids_before
        session.open_settings()
        session.back_to("Files")
    finally:
        session.close()
