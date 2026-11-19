"""Native end-to-end regression for the safe sidebar width toggle.

The sidebar collapse/expand is core chrome with no PTY: the F7 toggle
moves only the boundary next to the sidebar, so pane IDs, OS PIDs, tiled
topology, and focus survive repeated toggles while the Files content
absorbs exactly the 22-to-10 column delta. ``override-layout`` from the
stale startup layout is never used: it re-applies a two-child workspace
subtree and crams user-added splits into the sidebar column. In a
general stock session the F7 toggle is unbound and leaves every tile
untouched.
"""

import json
import time
from pathlib import Path
from typing import TypedDict

import pytest
from native_environment import capable_core_source, core_wanted_message, install_stage_core
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes
from native_session import NativeSession, StockSession, assert_no_chrome_ptys, wait_for

EXPANDED = 22
COLLAPSED = 10
DELTA = EXPANDED - COLLAPSED


@pytest.fixture
def width_native(
    native_env: tuple[dict[str, str], str],
) -> tuple[dict[str, str], str]:
    """Isolated session env launching on the stage-3 chrome core; fail when absent."""
    env, _stock = native_env
    source = capable_core_source()
    if source is None:
        pytest.fail(f"width test requires a capable core binary: {core_wanted_message()}")
    session_env = dict(env)
    session_env.pop("TDNEXT_ZELLIJ", None)
    link = install_stage_core(Path(session_env["TDNEXT_STATE_DIR"]), source)
    return session_env, str(link)


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


def content_pane(session: NativeSession, view: str = "files") -> str:
    """Terminal id of a tab's content PTY (one per tab; chrome has none)."""
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(
        item for item in panes
        if f"tdnext.pane view {view}" in (item.get("terminal_command") or item.get("pane_command") or "")
        and not item.get("is_floating")
    )
    return f"terminal_{pane['id']}"


def focus_content(session: NativeSession, pane_id: str) -> None:
    """Click the content pane center so later geometry reads hold focus still."""
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(item for item in panes if f"terminal_{item['id']}" == pane_id)
    session.mouse(int(pane["pane_x"]) + 2, int(pane["pane_y"]) + 2, release=False)
    time.sleep(0.4)
    session.mouse(int(pane["pane_x"]) + 2, int(pane["pane_y"]) + 2, release=True)
    wait_for(lambda: pane_id in session.action("list-clients"))


def test_width_toggle_preserves_user_splits(tmp_path: Path, width_native: tuple[dict[str, str], str]) -> None:
    env, core = width_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        session.switch("Files")
        content = content_pane(session)
        session.mouse(60, 20, release=False)
        session.mouse(60, 20, release=True)
        time.sleep(0.5)
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        time.sleep(0.5)
        session.action("new-pane", "--direction", "down", "--", "sleep", "1000")
        time.sleep(0.5)
        focus_content(session, content)
        assert_no_chrome_ptys(session)
        before = tiles(session)
        pids_before = sleep_pids(session)
        clients_before = session.action("list-clients")
        expanded_capture = session.terminal_text()
        assert "Files" in expanded_capture
        assert len(pids_before) == 2, f"expected two user sleeps, got {pids_before}"
        content_id = next(i for i in before if f"terminal_{i}" == content)

        for _ in range(2):
            session.send_key("F7")
            wait_for(
                lambda: (
                    geom if (geom := tiles(session).get(content_id)) is not None
                    and geom["cols"] == before[content_id]["cols"] + DELTA
                    and geom["x"] == before[content_id]["x"] - DELTA else None
                ),
                timeout=15,
            )
            collapsed = tiles(session)
            assert set(collapsed) == set(before), "collapse adds or drops panes"
            active = session.active_tab()
            band_top = collapsed[content_id]["y"]
            band_bottom = band_top + collapsed[content_id]["rows"]
            for pid, geom in collapsed.items():
                if pid == content_id:
                    continue
                assert geom["rows"] == before[pid]["rows"], f"pane terminal_{pid} changed height"
                assert geom["tab"] == before[pid]["tab"], f"pane terminal_{pid} changed tab"
                assert geom["cmd"] == before[pid]["cmd"], f"pane terminal_{pid} changed command"
                if geom["tab"] == active and band_top <= geom["y"] < band_bottom:
                    assert geom["x"] == before[pid]["x"] - DELTA, (
                        f"pane terminal_{pid} did not shift with the sidebar: {geom}"
                    )
                    assert geom["cols"] == before[pid]["cols"], (
                        f"pane terminal_{pid} changed width: {geom}"
                    )
                else:
                    assert geom == before[pid], f"pane terminal_{pid} moved outside the toggle band: {geom}"
            assert sleep_pids(session) == pids_before, "user processes must survive collapse"
            assert session.action("list-clients") == clients_before, "focus must stay put"
            def collapsed_screen() -> str:
                text = session.terminal_text()
                return text if text != expanded_capture and "Files" in text else ""

            collapsed_capture = wait_for(collapsed_screen)
            assert "Files" in collapsed_capture

            session.send_key("F7")
            wait_for(
                lambda: tiles(session) == before or None,
                timeout=15,
            )
            assert tiles(session) == before, "expand must restore exact geometry"
            assert sleep_pids(session) == pids_before, "user processes must survive expand"
            assert session.action("list-clients") == clients_before, "focus must stay put"
            assert "Files" in session.terminal_text()
    finally:
        session.close()


def test_stock_toggle_leaves_layout_unchanged(native_env: tuple[dict[str, str], str]) -> None:
    env, stock = native_env
    session = StockSession(env, stock)
    try:
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        time.sleep(0.5)
        session.action("new-pane", "--direction", "down", "--", "sleep", "1000")
        time.sleep(0.5)
        before = tiles(session)
        pids_before = sleep_pids(session)
        assert len(pids_before) == 2, f"expected two user sleeps, got {pids_before}"
        session.send_key("F7")
        time.sleep(3)
        assert tiles(session) == before, "stock toggle must not move any tile"
        assert sleep_pids(session) == pids_before, "stock toggle must not kill user processes"
    finally:
        session.close()
