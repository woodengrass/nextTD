import json
from pathlib import Path

from native_environment import native_env as native_env
from native_processes import tagged_processes
from native_session import NativeSession, StockSession, assert_no_chrome_processes, assert_no_chrome_ptys


def test_existing_stock_session_survives_new_core_session(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, stock = native_env
    env = {key: value for key, value in env.items() if key != "TDNEXT_ZELLIJ"}
    original = StockSession(env, stock)
    try:
        assert original.name in original.sessions()
        panes_before = {
            pane["id"]
            for pane in json.loads(original.action("list-panes", "--all", "--json"))
            if not pane.get("is_plugin")
        }
        assert not any(
            entry.startswith(b"TDNEXT_ZELLIJ=")
            for _, variables, _ in tagged_processes(original.tmux_name)
            for entry in variables
        )
        dedicated = NativeSession(tmp_path, env, stock)
        try:
            assert dedicated.name != original.name
            assert dedicated.sessions() == {original.name, dedicated.name}
            assert any(
                entry.startswith(b"TDNEXT_ZELLIJ=")
                for _, variables, _ in tagged_processes(dedicated.tmux_name)
                for entry in variables
            )
            dedicated.open_settings()
            dedicated.back_to("Files")
            assert_no_chrome_ptys(dedicated)
            assert_no_chrome_processes(dedicated.tmux_name)
            assert original.name in dedicated.sessions()
            panes_after = {
                pane["id"]
                for pane in json.loads(original.action("list-panes", "--all", "--json"))
                if not pane.get("is_plugin")
            }
            assert panes_after == panes_before
        finally:
            dedicated.close()
    finally:
        original.close()
