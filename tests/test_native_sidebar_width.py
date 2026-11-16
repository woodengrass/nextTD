"""Native end-to-end regression for the safe sidebar width toggle.

The sidebar collapse/expand must use the custom core
``set-pane-fixed-width`` action, which moves only the boundary next to the
sidebar: pane IDs, OS PIDs, tiled topology, and focus survive repeated
toggles, and the sidebar actually switches between 22 and 10 columns.
``override-layout`` from the stale startup layout is never used: it
re-applies a two-child workspace subtree and crams user-added splits into
the sidebar column. On stock Zellij the toggle refuses with a visible error
and leaves every tile untouched.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import TypedDict

import pytest
from native_environment import native_env as native_env  # noqa: F401  (fixture re-export)
from native_processes import tagged_processes
from native_session import NativeSession, wait_for

ROOT = Path(__file__).parents[1]
WIDTH_ENV = "TDNEXT_CORE_BINARY"
WIDTH_LINK = "zellij-core-navigation-v3"
EXPANDED = 22
COLLAPSED = 10


@pytest.fixture
def width_native(
    native_env: tuple[dict[str, str], str],
) -> tuple[dict[str, str], str]:
    """Isolated session env launching on the v3 width-capable core binary."""
    env, _stock = native_env
    candidate = os.environ.get(WIDTH_ENV, "").strip() or str(ROOT / "state" / "bin" / WIDTH_LINK)
    source = Path(candidate)
    if not source.is_file() or not os.access(source, os.X_OK):
        pytest.fail(
            f"width core {candidate!r} is not an executable file; build it with scripts/build-zellij-core-navigation.sh"
        )
    probe = subprocess.run(
        [str(source), "action", "set-pane-fixed-width", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if probe.returncode != 0:
        pytest.fail(f"width core {source} does not expose 'set-pane-fixed-width'")
    session_env = dict(env)
    bin_dir = Path(session_env["TDNEXT_STATE_DIR"]) / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / WIDTH_LINK
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(source)
    shadow = bin_dir / "zellij"
    if shadow.is_symlink() or shadow.exists():
        shadow.unlink()
    shadow.symlink_to(source)
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


def sidebar_pane(session: NativeSession, tab: str = "files") -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(p for p in panes if f"panes sidebar {tab}" in (p.get("pane_command") or ""))
    return f"terminal_{pane['id']}"


def focus_sidebar(session: NativeSession, pane_id: str) -> None:
    # Click the help area: focusing without hitting the rail-title button,
    # whose release-only click (while unfocused) would toggle the sidebar.
    session.mouse(5, 34, release=False)
    time.sleep(0.4)
    session.mouse(5, 34, release=True)
    wait_for(lambda: pane_id in session.action("list-clients"))
    wait_for(lambda: "WORKSPACE" in sidebar_screen(session, pane_id), timeout=15)


def toggle_and_wait(session: NativeSession, pane_id: str, width: int) -> None:
    session.send_key("F7")
    wait_for(
        lambda: (
            next(
                (
                    pane.get("pane_columns")
                    for pane in json.loads(session.action("list-panes", "--all", "--json"))
                    if f"terminal_{pane['id']}" == pane_id
                ),
                None,
            )
            == width
        ),
        timeout=15,
    )


def sidebar_screen(session: NativeSession, pane_id: str) -> str:
    return session.action("dump-screen", "--pane-id", pane_id)


def test_width_toggle_preserves_user_splits(tmp_path: Path, width_native: tuple[dict[str, str], str]) -> None:
    env, core = width_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        session.switch("Files")
        session.mouse(60, 20, release=False)
        session.mouse(60, 20, release=True)
        time.sleep(0.5)
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        time.sleep(0.5)
        session.action("new-pane", "--direction", "down", "--", "sleep", "1000")
        time.sleep(0.5)
        sidebar = sidebar_pane(session)
        focus_sidebar(session, sidebar)
        before = tiles(session)
        pids_before = sleep_pids(session)
        clients_before = session.action("list-clients")
        assert len(pids_before) == 2, f"expected two user sleeps, got {pids_before}"
        assert before[next(i for i in before if f"terminal_{i}" == sidebar)]["cols"] == EXPANDED

        for _ in range(2):
            toggle_and_wait(session, sidebar, COLLAPSED)
            collapsed = tiles(session)
            assert set(collapsed) == set(before), "collapse adds or drops panes"
            active = session.active_tab()
            sidebar_id = next(i for i in collapsed if f"terminal_{i}" == sidebar)
            assert collapsed[sidebar_id]["cols"] == COLLAPSED
            sidebar_row = collapsed[sidebar_id]
            assert (sidebar_row["x"], sidebar_row["y"]) == (0, before[sidebar_id]["y"])
            band_top, band_bottom = sidebar_row["y"], sidebar_row["y"] + sidebar_row["rows"]
            for pid, geom in collapsed.items():
                if f"terminal_{pid}" == sidebar:
                    continue
                assert geom["rows"] == before[pid]["rows"], f"pane terminal_{pid} changed height"
                assert geom["tab"] == before[pid]["tab"], f"pane terminal_{pid} changed tab"
                assert geom["cmd"] == before[pid]["cmd"], f"pane terminal_{pid} changed command"
                if geom["tab"] == active and band_top <= geom["y"] < band_bottom:
                    assert geom["x"] >= COLLAPSED, (
                        f"pane terminal_{pid} slipped under the sidebar: {geom}"
                    )
            assert sleep_pids(session) == pids_before, "user processes must survive collapse"
            assert session.action("list-clients") == clients_before, "focus must stay put"
            screen = sidebar_screen(session, sidebar)
            assert "> F7" in screen and "WORKSPACE" not in screen

            toggle_and_wait(session, sidebar, EXPANDED)
            assert tiles(session) == before, "expand must restore exact geometry"
            assert sleep_pids(session) == pids_before, "user processes must survive expand"
            assert session.action("list-clients") == clients_before, "focus must stay put"
            assert "WORKSPACE" in sidebar_screen(session, sidebar)
    finally:
        session.close()


def test_stock_toggle_leaves_layout_unchanged(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, stock = native_env
    session = NativeSession(tmp_path, env, stock)
    try:
        session.switch("Files")
        session.mouse(60, 20, release=False)
        session.mouse(60, 20, release=True)
        time.sleep(0.5)
        session.action("new-pane", "--direction", "right", "--", "sleep", "1000")
        time.sleep(0.5)
        sidebar = sidebar_pane(session)
        focus_sidebar(session, sidebar)
        before = tiles(session)
        pids_before = sleep_pids(session)
        ready_screen = sidebar_screen(session, sidebar)
        session.send_key("F7")
        # The error toast wraps the long message, so no single phrase is
        # matchable; any screen change here is the failure feedback itself.
        wait_for(lambda: sidebar_screen(session, sidebar) != ready_screen, timeout=15)
        time.sleep(7)
        assert tiles(session) == before, "stock toggle must not move any tile"
        assert sleep_pids(session) == pids_before, "stock toggle must not kill user processes"
    finally:
        session.close()
