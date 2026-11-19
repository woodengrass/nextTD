import json
from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, assert_no_chrome_processes, assert_no_chrome_ptys, wait_for

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.runtime import manager_for

CODICON_GLYPH = "\uea83"


def test_native_settings_keyboard_selects_provider_and_style(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    state = Path(env["TDNEXT_STATE_DIR"])
    manager_for(state).enable("icons")
    session = NativeSession(tmp_path, env, binary)
    try:
        session.open_settings()
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        tool = next(p for p in panes if "tdnext.pane view settings" in (p.get("pane_command") or ""))
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


def test_native_content_keeps_its_own_icon_snapshot_after_settings_change(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    env, binary = native_env
    state = Path(env["TDNEXT_STATE_DIR"])
    plugins = manager_for(state)
    plugins.enable("icons")
    appearance = AppearanceStore(state / "appearance.json")
    appearance.write(Appearance(provider_id="icons", icon_mode="codicons"))
    first = NativeSession(tmp_path, env, binary)
    try:
        # Stage 3 draws view icons in core chrome (sidebar), so the
        # snapshot glyph is asserted on the terminal capture, not the
        # content pane dump.
        wait_for(lambda: CODICON_GLYPH in first.terminal_text())
        monitor = wait_for(first.btop_pid)
        appearance.write(Appearance(provider_id="icons", icon_mode="compatibility"))
        plugins.disable("icons")
        first.open_settings()
        first.back_to("Files")
        assert_no_chrome_ptys(first)
        screen = wait_for(lambda: (output if CODICON_GLYPH in (output := first.terminal_text()) else ""))
        assert CODICON_GLYPH in screen
        assert first.btop_pid() == monitor
        second = NativeSession(tmp_path, env, binary)
        try:
            screen = wait_for(lambda: (output if "Files" in (output := second.terminal_text()) else ""))
            assert "▤" in screen
            assert CODICON_GLYPH not in screen
            assert_no_chrome_ptys(second)
            assert_no_chrome_processes(second.tmux_name)
        finally:
            second.close()
    finally:
        first.close()
