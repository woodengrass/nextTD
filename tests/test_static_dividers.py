import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for
from rich.cells import cell_len
from rich.color import Color, ColorType
from rich.console import Console
from rich.style import Style
from rich.text import Text


@pytest.fixture
def native_desktop(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> Iterator[NativeSession]:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        panes = json.loads(session.action("list-panes", "--all", "--json"))
        files = next(pane for pane in panes if "tdnext.pane view files" in (pane.get("pane_command") or ""))
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", f"terminal_{files['id']}"))
        yield session
    finally:
        session.close()


def files_pane(session: NativeSession) -> str:
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    files = next(pane for pane in panes if "tdnext.pane view files" in (pane.get("pane_command") or ""))
    return f"terminal_{files['id']}"


def cell_style(session: NativeSession, row: int, column: int, glyph: str) -> Style:
    capture = subprocess.run(
        ["tmux", "-S", session.tmux_socket, "capture-pane", "-p", "-e", "-t", session.tmux_name],
        env=session.env, capture_output=True, text=True, check=True, timeout=5,
    )
    line = Text.from_ansi(capture.stdout.splitlines()[row])
    cell = 0
    for offset, character in enumerate(line.plain):
        if cell == column:
            assert character == glyph
            return line.get_style_at_offset(Console(), offset)
        cell += cell_len(character)
    raise AssertionError(f"Missing divider at row {row}, column {column}")


def divider_color(session: NativeSession, row: int, column: int, glyph: str) -> Color | None:
    return cell_style(session, row, column, glyph).color


def assert_neutral(color: Color | None) -> None:
    assert color is not None
    assert color.type is not ColorType.DEFAULT
    red, green, blue = color.get_truecolor()
    assert red == green == blue and 90 <= red <= 165


def test_native_divider_color_stays_neutral_across_focus_modes_and_group(native_desktop: NativeSession) -> None:
    session = native_desktop
    samples = {"files focused": divider_color(session, 8, 21, "│")}
    samples["unfocused"] = divider_color(session, 1, 10, "─")

    session.switch("Monitor")
    samples["monitor tab"] = divider_color(session, 8, 21, "│")

    session.switch("Files")
    session.send_key("C-g")
    samples["locked"] = divider_color(session, 8, 21, "│")
    session.send_key("C-g")

    session.open_settings()
    samples["settings tab"] = divider_color(session, 1, 10, "─")
    session.back_to("Files")
    samples["returned"] = divider_color(session, 8, 21, "│")

    assert len(set(samples.values())) == 1, samples
    assert_neutral(samples["files focused"])


def test_native_divider_color_stays_unchanged_in_locked_mode(native_desktop: NativeSession) -> None:
    session = native_desktop
    pane = files_pane(session)
    normal = divider_color(session, 8, 21, "│")
    session.send_key("C-g")
    session.send_key("f")
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane))
    assert divider_color(session, 8, 21, "│") == normal


def test_sidebar_content_focus_remains_visible_when_pane_focus_moves(native_desktop: NativeSession) -> None:
    session = native_desktop
    files_selected = cell_style(session, 8, 5, " ")
    separator = divider_color(session, 8, 21, "│")
    session.switch("Monitor")
    monitor_selected = wait_for(
        lambda: ((style,) if (style := cell_style(session, 8, 5, " ")) != files_selected else None)
    )[0]
    session.switch("Files")
    wait_for(lambda: cell_style(session, 8, 5, " ") == files_selected or None)
    assert (monitor_selected.color, monitor_selected.bgcolor) != (files_selected.color, files_selected.bgcolor)
    assert divider_color(session, 8, 21, "│") == separator


def test_native_divider_color_stays_unchanged_on_hover(native_desktop: NativeSession) -> None:
    session = native_desktop
    pane = files_pane(session)
    unfocused = divider_color(session, 1, 10, "─")
    mouse = b"\x1b[<35;5;5M"
    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "send-keys", "-t", session.tmux_name,
         "-H", *(f"{byte:02x}" for byte in mouse)],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    session.send_key("f")
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", pane))
    assert divider_color(session, 1, 10, "─") == unfocused


def test_native_divider_color_survives_theme_switches_and_resize(native_desktop: NativeSession) -> None:
    session = native_desktop
    initial = divider_color(session, 8, 21, "│")
    session.action("set-light-theme")
    session.action("set-dark-theme")
    wait_for(lambda: divider_color(session, 8, 21, "│") == initial or None)
    assert divider_color(session, 8, 21, "│") == initial

    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "resize-window", "-t", session.tmux_name, "-x", "140", "-y", "40"],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    wait_for(lambda: any(
        pane["pane_columns"] != 98 for pane in json.loads(session.action("list-panes", "--all", "--json"))
        if not pane["is_plugin"]
    ) or None)
    assert divider_color(session, 8, 21, "│") == initial
    assert divider_color(session, 1, 10, "─") == initial
