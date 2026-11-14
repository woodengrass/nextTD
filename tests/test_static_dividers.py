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
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", "terminal_2"))
        yield session
    finally:
        session.close()


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


def test_native_divider_color_stays_neutral_across_focus_modes_and_group(native_desktop: NativeSession) -> None:
    session = native_desktop
    samples = {"files focused": divider_color(session, 8, 21, "│")}
    samples["unfocused"] = divider_color(session, 1, 10, "─")

    # Moving left in Pane mode proves the mode change was consumed before capture.
    session.send_key("C-p")
    session.send_key("h")
    wait_for(lambda: "terminal_1" in session.action("list-clients"))
    samples["pane mode"] = divider_color(session, 8, 21, "│")

    session.send_key("M-p")
    wait_for(lambda: next((pane["index_in_pane_group"] for pane in json.loads(
        session.action("list-panes", "--all", "--json")
    ) if pane["id"] == 1 and not pane["is_plugin"] and pane["index_in_pane_group"]), None))
    samples["group focused"] = divider_color(session, 1, 10, "─")
    session.send_key("l")
    wait_for(lambda: "terminal_2" in session.action("list-clients"))
    samples["group unfocused"] = divider_color(session, 1, 10, "─")

    assert len(set(samples.values())) == 1, samples
    color = samples["files focused"]
    assert color is not None
    assert color.type is not ColorType.DEFAULT
    red, green, blue = color.get_truecolor()
    assert red == green == blue and 90 <= red <= 165


def test_native_divider_color_stays_unchanged_in_locked_mode(native_desktop: NativeSession) -> None:
    session = native_desktop
    normal = divider_color(session, 8, 21, "│")
    session.send_key("C-g")
    session.send_key("f")
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", "terminal_2"))
    assert divider_color(session, 8, 21, "│") == normal


def test_sidebar_content_focus_remains_visible_when_pane_focus_moves(native_desktop: NativeSession) -> None:
    session = native_desktop
    before = cell_style(session, 8, 5, " ")
    session.send_key("C-p")
    session.send_key("h")
    wait_for(lambda: "terminal_1" in session.action("list-clients"))
    session.send_key("C-p")
    session.send_key("Down")
    focused = wait_for(lambda: ((style,) if (style := cell_style(session, 8, 5, " ")) != before else None))[0]
    separator = divider_color(session, 8, 21, "│")
    session.send_key("C-p")
    session.send_key("l")
    wait_for(lambda: "terminal_2" in session.action("list-clients"))
    unfocused = wait_for(lambda: ((style,) if (style := cell_style(session, 8, 5, " ")) != focused else None))[0]
    assert (focused.color, focused.bgcolor) != (unfocused.color, unfocused.bgcolor)
    assert divider_color(session, 8, 21, "│") == separator


def test_native_divider_color_stays_unchanged_on_hover(native_desktop: NativeSession) -> None:
    session = native_desktop
    unfocused = divider_color(session, 1, 10, "─")
    mouse = b"\x1b[<35;5;5M"
    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "send-keys", "-t", session.tmux_name,
         "-H", *(f"{byte:02x}" for byte in mouse)],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    session.send_key("f")
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", "terminal_2"))
    assert divider_color(session, 1, 10, "─") == unfocused


def test_native_divider_color_survives_theme_switches_and_resize(native_desktop: NativeSession) -> None:
    session = native_desktop
    initial = divider_color(session, 8, 21, "│")
    for action, target, x in (("set-light-theme", "terminal_1", 5), ("set-dark-theme", "terminal_2", 50)):
        session.action(action)
        session.mouse(x, 5, release=False)
        session.mouse(x, 5, release=True)
        wait_for(lambda target=target: target in session.action("list-clients"))
        assert divider_color(session, 8, 21, "│") == initial

    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "resize-window", "-t", session.tmux_name, "-x", "140", "-y", "40"],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    wait_for(lambda: any(
        pane["pane_columns"] == 140 for pane in json.loads(session.action("list-panes", "--all", "--json"))
        if not pane["is_plugin"]
    ))
    assert divider_color(session, 8, 21, "│") == initial
    assert divider_color(session, 1, 10, "─") == initial
