import json
import subprocess
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, assert_no_chrome_processes, assert_no_chrome_ptys, wait_for
from test_native_core_navigation import capture
from test_native_core_navigation import core_native as core_native


def wait_for_click_ready(session: NativeSession) -> None:
    """Bounded readiness for the post-reattach Back click: exactly one attached
    client, stably focused on the settings tool pane, with Back stably rendered
    at the click cell. Stability across consecutive observations, not a fixed
    delay; sends no keyboard or mouse input, so the pre-first-key CLI routing
    state pinned by this regression is untouched."""
    steady = 0

    def click_ready() -> bool:
        nonlocal steady
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(
            (pane for pane in panes if "tdnext.pane view settings" in (pane.get("pane_command") or "")),
            None,
        )
        if tool is None:
            steady = 0
            return False
        pane_id = f"terminal_{tool['id']}"
        rows = [line.split() for line in session.action("list-clients").splitlines()[1:] if line.split()]
        if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != pane_id:
            steady = 0
            return False
        screen = capture(session).splitlines()
        if len(screen) <= 3 or screen[2][111:115] != "Back":
            steady = 0
            return False
        steady += 1
        return steady >= 3

    wait_for(click_ready)


def wait_for_back_cell(session: NativeSession) -> None:
    """Bounded wait for Back at its pinned settings chrome cell.

    The settings pane dump can show Back before the core composites
    the frame to the terminal, so poll the capture instead of asserting
    on the first frame; the pinned-cell assertion itself is unchanged.
    """
    def visible() -> bool:
        screen = capture(session).splitlines()
        return len(screen) > 2 and screen[2][111:115] == "Back"

    wait_for(visible)


def test_core_back_click_without_detach(tmp_path: Path, core_native: tuple[dict[str, str], str]) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        session.switch("Files")
        origin_tab = session.active_tab()
        origin_pane = session.action("list-clients").splitlines()[1].split()[1]
        session.open_settings()
        wait_for_back_cell(session)
        assert capture(session).splitlines()[2][111:115] == "Back"
        session.mouse(113, 3, release=False)
        session.mouse(113, 3, release=True)
        wait_for(lambda: session.active_tab() == origin_tab)
        wait_for(lambda: session.action("list-clients").splitlines()[1].split()[1] == origin_pane)
    finally:
        session.close()


@pytest.mark.parametrize("return_method", ["click", "escape", "cli"])
def test_core_reconnect_restores_exact_tab_and_pane(
    tmp_path: Path, core_native: tuple[dict[str, str], str], return_method: str
) -> None:
    env, core = core_native
    session = NativeSession(tmp_path, env, core)
    try:
        assert session.sessions() == {session.name}
        session.switch("Files")
        origin_tab = session.active_tab()
        origin_clients = session.action("list-clients")
        origin_pane = origin_clients.splitlines()[1].split()[1]
        session.open_settings()
        session.action("detach")

        def detached() -> bool:
            result = subprocess.run(
                [core, "action", "list-clients"], env=session.target_env,
                capture_output=True, text=True, check=True, timeout=5,
            )
            return len(result.stdout.splitlines()) == 1

        wait_for(detached)
        wait_for(lambda: subprocess.run(
            ["tmux", "-S", session.tmux_socket, "has-session", "-t", session.tmux_name],
            env=env, capture_output=True, check=False, timeout=5,
        ).returncode != 0)
        subprocess.run(
            ["tmux", "-S", session.tmux_socket, "-f", "/dev/null", "new-session", "-d",
             "-s", session.tmux_name, "-x", "120", "-y", "36", "-e",
             f"TDNEXT_TEST_SESSION={session.tmux_name}", core, "attach", session.name],
            env=env, capture_output=True, check=True, timeout=5,
        )
        wait_for(lambda: session.ready(3))
        session.wait_for_settings()
        assert_no_chrome_ptys(session)
        assert_no_chrome_processes(session.tmux_name)
        wait_for_back_cell(session)
        captured = capture(session)
        (tmp_path / "reattached-screen.txt").write_text(captured)
        screen = captured.splitlines()
        assert screen[2][111:115] == "Back", screen[2]
        if return_method == "click":
            wait_for_click_ready(session)
            session.mouse(113, 3, release=False)
            session.mouse(113, 3, release=True)
        elif return_method == "escape":
            session.send_key("Escape")
        else:
            session.action("return-to-origin")
        wait_for(lambda: session.active_tab() == origin_tab)
        wait_for(lambda: session.action("list-clients").splitlines()[1].split()[1] == origin_pane)
        (tmp_path / "reconnect-proof.txt").write_text(
            f"method={return_method}\nback=113,4\norigin_tab={origin_tab}\n"
            f"origin_pane={origin_pane}\nrestored_tab={session.active_tab()}\n"
            f"restored_pane={session.action('list-clients').splitlines()[1].split()[1]}\n"
        )
    finally:
        session.close()
