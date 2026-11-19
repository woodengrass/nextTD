import json
import subprocess
import sys
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
from test_native_header_regressions import files_pane


@pytest.fixture
def native_desktop(tmp_path: Path, native_env: tuple[dict[str, str], str]) -> Iterator[NativeSession]:
    env, binary = native_env
    session = NativeSession(tmp_path, env, binary)
    try:
        pane = files_pane(session)
        wait_for(lambda: "Files" in session.action("dump-screen", "--pane-id", pane))
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


def divider_positions(session: NativeSession) -> tuple[int, int, int]:
    pane = next(pane for pane in json.loads(session.action("list-panes", "--all", "--json"))
                if "tdnext.pane view files" in (pane.get("pane_command") or ""))
    lines = session.terminal_text().splitlines()
    horizontal = next(row for row, line in enumerate(lines) if line.startswith("────────"))
    return pane["pane_y"] + 1, pane["pane_x"] - 1, horizontal


def test_native_divider_color_stays_neutral_across_focus_modes_and_group(native_desktop: NativeSession) -> None:
    session = native_desktop
    row, column, horizontal = divider_positions(session)
    files = files_pane(session)
    samples = {"files focused": divider_color(session, row, column, "│")}
    samples["horizontal"] = divider_color(session, horizontal, 10, "─")

    other = session.action("new-pane", "--direction", "right", "--", sys.executable, "-c", "input()").strip()
    wait_for(lambda: other in session.action("list-clients"))

    session.send_key("C-p")
    session.send_key("h")
    wait_for(lambda: files in session.action("list-clients"))
    samples["pane mode"] = divider_color(session, row, column, "│")

    session.send_key("M-p")
    wait_for(lambda: next((pane["index_in_pane_group"] for pane in json.loads(
        session.action("list-panes", "--all", "--json")
    ) if f"terminal_{pane['id']}" == files and not pane["is_plugin"] and pane["index_in_pane_group"]), None))
    samples["group focused"] = divider_color(session, horizontal, 10, "─")
    session.send_key("l")
    wait_for(lambda: other in session.action("list-clients"))
    samples["group unfocused"] = divider_color(session, horizontal, 10, "─")

    assert len(set(samples.values())) == 1, samples
    color = samples["files focused"]
    assert color is not None
    assert color.type is not ColorType.DEFAULT
    red, green, blue = color.get_truecolor()
    assert red == green == blue and 90 <= red <= 165


def test_native_divider_color_stays_unchanged_in_locked_mode(native_desktop: NativeSession) -> None:
    session = native_desktop
    row, column, _ = divider_positions(session)
    normal = divider_color(session, row, column, "│")
    session.send_key("C-g")
    session.send_key("f")
    files = files_pane(session)
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", files))
    assert divider_color(session, row, column, "│") == normal


def test_sidebar_content_focus_remains_visible_when_pane_focus_moves(native_desktop: NativeSession) -> None:
    session = native_desktop
    row, column, _ = divider_positions(session)
    files = files_pane(session)
    selected = next(
        i for i, line in enumerate(session.terminal_text().splitlines())
        if ">" in line[:column] and "Files" in line[:column]
    )
    before = cell_style(session, selected, 0, ">")
    other = session.action("new-pane", "--direction", "right", "--", sys.executable, "-c", "input()").strip()
    wait_for(lambda: other in session.action("list-clients"))
    assert cell_style(session, selected, 0, ">") == before
    separator = divider_color(session, row, column, "│")
    session.send_key("C-p")
    session.send_key("h")
    wait_for(lambda: files in session.action("list-clients"))
    session.send_key("C-p")
    session.send_key("f")
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", files))
    session.send_key("Escape")
    wait_for(lambda: "New file name" not in session.action("dump-screen", "--pane-id", files))
    session.send_key("C-p")
    session.send_key("l")
    wait_for(lambda: other in session.action("list-clients"))
    assert cell_style(session, selected, 0, ">") == before
    assert divider_color(session, row, column, "│") == separator


def test_native_divider_color_stays_unchanged_on_hover(native_desktop: NativeSession) -> None:
    session = native_desktop
    _, _, horizontal = divider_positions(session)
    unfocused = divider_color(session, horizontal, 10, "─")
    mouse = b"\x1b[<35;5;5M"
    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "send-keys", "-t", session.tmux_name,
         "-H", *(f"{byte:02x}" for byte in mouse)],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    session.send_key("f")
    files = files_pane(session)
    wait_for(lambda: "New file name" in session.action("dump-screen", "--pane-id", files))
    assert divider_color(session, horizontal, 10, "─") == unfocused


def test_native_divider_color_survives_theme_switches_and_resize(native_desktop: NativeSession) -> None:
    session = native_desktop
    row, column, _ = divider_positions(session)
    initial = divider_color(session, row, column, "│")
    files = files_pane(session)
    other = session.action("new-pane", "--direction", "right", "--", sys.executable, "-c", "input()").strip()
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    for action, target in (("set-light-theme", files), ("set-dark-theme", other)):
        pane = next(pane for pane in panes if f"terminal_{pane['id']}" == target and not pane["is_plugin"])
        x, y = pane["pane_x"] + 2, pane["pane_y"] + 2
        session.action(action)
        session.mouse(x, y, release=False)
        session.mouse(x, y, release=True)
        wait_for(lambda target=target: target in session.action("list-clients"))
        assert divider_color(session, row, column, "│") == initial

    subprocess.run(
        ["tmux", "-S", session.tmux_socket, "resize-window", "-t", session.tmux_name, "-x", "140", "-y", "40"],
        env=session.env, capture_output=True, check=True, timeout=5,
    )
    wait_for(lambda: any(
        pane["pane_x"] + pane["pane_columns"] == 140
        for pane in json.loads(session.action("list-panes", "--all", "--json"))
        if not pane["is_plugin"]
    ))
    row, column, horizontal = divider_positions(session)
    assert divider_color(session, row, column, "│") == initial
    assert divider_color(session, horizontal, 10, "─") == initial
