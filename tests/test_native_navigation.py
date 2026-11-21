import json
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for


def test_native_settings_returns_to_exact_tab_without_restarting_btop(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pid = wait_for(session.btop_pid)
        session.switch("Files")
        session.open_settings()
        session.back_to("Files")
        session.switch("Monitor")
        session.open_settings()
        session.back_to("Monitor")
        assert session.btop_pid() == pid
    finally:
        session.close()


def test_native_sessions_keep_return_tabs_separate(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    first = NativeSession(tmp_path, env, binary)
    try:
        second = NativeSession(tmp_path, env, binary)
        try:
            first.switch("Monitor")
            second.switch("Files")
            first.open_settings()
            second.open_settings()
            first.back_to("Monitor")
            second.back_to("Files")
        finally:
            second.close()
    finally:
        first.close()


def test_native_header_responds_to_mouse(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        session.switch("Files")
        session.click_header()
        target = session.tab_id("tdnext-header:settings")
        wait_for(lambda: session.active_tab() == target)
        session.back_to("Files")
    finally:
        session.close()


def test_native_header_clicks_only_activate_actions(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> None:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        session.switch("Files")
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        files = next(pane for pane in panes if "tdnext.pane view files" in (pane.get("pane_command") or ""))
        files_id = f"terminal_{files['id']}"
        wait_for(lambda: files_id in session.action("list-clients"))
        # Blank header area focuses nothing away: no tab switch, focus stays.
        session.header_mouse(5, release=False)
        session.header_mouse(5, release=True)
        wait_for(lambda: files_id in session.action("list-clients"))
        assert session.active_tab() == session.tab_id("Files")
        session.click_header()
        wait_for(lambda: session.active_tab() == session.tab_id("tdnext-header:settings"))
        session.back_to("Files")
    finally:
        session.close()
