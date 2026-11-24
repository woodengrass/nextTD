"""Shared terminal-cell golden capture/comparison helper for tests.

One place for lossless golden-screen capture so individual tests never copy
the harness. The golden recorder and every golden test share the
two functions below; nothing else in this file is public API.

Contract
--------
* Capture runs ``tmux capture-pane -p -e -N``: tmux already owns the cell
  grid, so there is no VT emulation here and no extra dependency. ``-e``
  keeps every foreground/background attribute as SGR escapes, ``-N`` keeps
  trailing spaces (including styled trailing blanks); cells that were never
  written have no style, so the helper pads them with default-style spaces.
* The serialized frame is stable plain text: one line per grid row, each raw
  ESC byte rendered as the visible four characters ``\\x1b`` (a literal
  backslash in the grid is doubled first, so escaping is reversible), rows
  joined by ``\\n`` with a trailing newline, plus a ``# golden-screen v1``
  header carrying the geometry. Every row's plain display width is exactly
  ``cols`` after padding, so geometry survives even for blank rows.
* Size is strict: the pane size is queried first and must equal
  ``cols``/``rows``; the captured row count and every row width are checked
  too. Wide (double-cell) characters count two columns via
  ``unicodedata.east_asian_width``; combining marks count zero.
* Golden files live in ``tests/golden/<name>.screen`` and are owned by the
  golden recorder. This helper never writes or updates them; a missing or
  mismatching file is an ``AssertionError`` with the first differing row
  and cell spelled out.

Assumptions: tmux >= 3 (``-e -N`` flags), pane output decodes as UTF-8,
and ``target`` addresses exactly the pane to capture.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from pathlib import Path

__all__ = ["ANSI16_DIR", "GOLDEN_DIR", "assert_frame", "assert_frame_in", "capture_frame"]

#: Directory owning every ``<name>.screen`` fixture. The golden recorder
#: generates files here; this helper only reads them.
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

#: Unified old-Python ANSI-look oracle used in both terminal modes
#: (``linux`` and ``xterm-256color``). Owned by the independent recorder;
#: this helper only reads it.
ANSI16_DIR = GOLDEN_DIR / "ansi16"

_ESC = "\x1b"
_SGR_RE = re.compile(r"\x1b\[[0-9;:]*m")
_HEADER_RE = re.compile(r"# golden-screen v1 cols=(\d+) rows=(\d+)")
_TIMEOUT = 10.0
_DIFF_CONTEXT = 300


def _plain_width(text: str) -> int:
    """Display-cell width of SGR-free text (wide chars cost two)."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _escape(raw: str) -> str:
    """Render one captured row as visible plain text (reversible)."""
    return raw.replace("\\", "\\\\").replace(_ESC, "\\x1b")


def _unescape(visible: str) -> str:
    """Inverse of :func:`_escape`, used only for failure diagnostics."""
    out: list[str] = []
    i = 0
    while i < len(visible):
        if visible.startswith("\\\\", i):
            out.append("\\")
            i += 2
        elif visible.startswith("\\x1b", i):
            out.append(_ESC)
            i += 4
        else:
            out.append(visible[i])
            i += 1
    return "".join(out)


def _columns(plain: str) -> list[tuple[int, str]]:
    """Map display columns to their glyph (combining marks stay attached)."""
    columns: list[tuple[int, str]] = []
    column = 0
    for char in plain:
        if unicodedata.combining(char):
            if columns:
                start, glyph = columns[-1]
                columns[-1] = (start, glyph + char)
            continue
        columns.append((column, char))
        column += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return columns


def capture_frame(socket: Path, target: str, cols: int, rows: int) -> str:
    """Capture one deterministic lossless frame of a tmux pane grid.

    Args:
        socket: Path to the private tmux server socket.
        target: Tmux target of the pane to capture (session or pane id).
        cols: Expected pane width in display cells; must match exactly.
        rows: Expected pane height in rows; must match exactly.

    Returns:
        The serialized frame, ready to compare or store verbatim.

    Raises:
        ValueError: ``cols``/``rows`` are not positive.
        AssertionError: Pane size, captured row count, or a row width does
            not match ``cols``/``rows``.
    """
    if cols <= 0 or rows <= 0:
        raise ValueError(f"golden capture needs positive geometry, got cols={cols} rows={rows}")
    size = subprocess.run(
        ["tmux", "-S", str(socket), "display-message", "-p", "-t", target, "#{pane_width} #{pane_height}"],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT,
    ).stdout.strip()
    actual_cols, _, actual_rows = size.partition(" ")
    if (actual_cols, actual_rows) != (str(cols), str(rows)):
        raise AssertionError(
            f"golden size mismatch for {target!r}: pane is {actual_cols}x{actual_rows}, "
            f"expected {cols}x{rows}; resize the pane instead of recording a new size"
        )
    captured = subprocess.run(
        ["tmux", "-S", str(socket), "capture-pane", "-p", "-e", "-N", "-t", target],
        check=True, capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT,
    ).stdout
    if not captured.endswith("\n"):
        raise AssertionError(f"golden capture for {target!r} is not newline terminated: {captured!r}")
    raw_lines = captured.split("\n")[:-1]
    if len(raw_lines) != rows:
        raise AssertionError(
            f"golden capture for {target!r} returned {len(raw_lines)} rows, expected {rows}"
        )
    serialised: list[str] = []
    for index, raw in enumerate(raw_lines):
        width = _plain_width(_SGR_RE.sub("", raw))
        if width > cols:
            raise AssertionError(
                f"golden capture for {target!r} row {index} is {width} cells wide, "
                f"expected {cols}: {raw!r}"
            )
        if width < cols:
            if _ESC in raw and not raw.endswith(f"{_ESC}[0m") and not raw.endswith(f"{_ESC}[m"):
                raw += f"{_ESC}[0m"
            raw += " " * (cols - width)
        serialised.append(_escape(raw))
    return f"# golden-screen v1 cols={cols} rows={rows}\n" + "\n".join(serialised) + "\n"


def _first_cell_diff(expected_plain: str, actual_plain: str) -> str:
    expected_columns = _columns(expected_plain)
    actual_columns = _columns(actual_plain)
    for (column, want), (_, got) in zip(expected_columns, actual_columns, strict=False):
        if want != got:
            return f"first differing cell: column {column}: expected {want!r}, actual {got!r}"
    if len(expected_columns) != len(actual_columns):
        return (
            "same glyph prefix, different row width: "
            f"expected {len(expected_columns)} cells, actual {len(actual_columns)} cells"
        )
    return "same glyphs, styles (SGR escapes) differ"


def _describe_mismatch(name: str, path: Path, expected: str, actual: str) -> str:
    expected_lines = expected.split("\n")
    actual_lines = actual.split("\n")
    header = ""
    expected_geometry = _HEADER_RE.fullmatch(expected_lines[0]) if expected_lines else None
    actual_geometry = _HEADER_RE.fullmatch(actual_lines[0]) if actual_lines else None
    if expected_geometry and actual_geometry:
        if expected_geometry.groups() != actual_geometry.groups():
            header = (
                f"geometry differs: expected cols={expected_geometry.group(1)} "
                f"rows={expected_geometry.group(2)}, actual cols={actual_geometry.group(1)} "
                f"rows={actual_geometry.group(2)}\n"
            )
    elif expected_lines and actual_lines and expected_lines[0] != actual_lines[0]:
        header = f"header differs:\n  expected {expected_lines[0]!r}\n  actual   {actual_lines[0]!r}\n"
    first = next(
        (i for i, (want, got) in enumerate(zip(expected_lines, actual_lines, strict=False)) if want != got),
        min(len(expected_lines), len(actual_lines)),
    )
    if first >= len(expected_lines) or first >= len(actual_lines):
        longer = expected_lines if len(expected_lines) > len(actual_lines) else actual_lines
        return (
            f"golden mismatch: {name!r} ({path}):\n{header}"
            f"row count differs: expected {len(expected_lines)} lines, actual {len(actual_lines)} lines; "
            "first extra line: "
            f"{longer[first]!r} "
            "(golden files are never auto-updated; the recorder owns regeneration)"
        )
    row = first - 1  # line 0 is the header, grid rows are 0-based below it
    want, got = expected_lines[first], actual_lines[first]
    detail = _first_cell_diff(_SGR_RE.sub("", _unescape(want)), _SGR_RE.sub("", _unescape(got)))
    return (
        f"golden mismatch: {name!r} ({path}):\n{header}"
        f"first differing screen row: {row}\n"
        f"  expected {_truncate(want)}\n"
        f"  actual   {_truncate(got)}\n"
        f"  {detail} "
        "(golden files are never auto-updated; the recorder owns regeneration)"
    )


def _truncate(line: str, limit: int = _DIFF_CONTEXT) -> str:
    text = repr(line)
    return text if len(text) <= limit else text[:limit] + "...'"


def assert_frame(name: str, actual: str, *, golden_dir: Path | None = None) -> None:
    """Compare a captured frame against its golden fixture.

    Args:
        name: Fixture stem; reads ``<golden_dir>/<name>.screen``.
        actual: Frame previously returned by :func:`capture_frame`.
        golden_dir: Reference directory; defaults to ``GOLDEN_DIR``.
            Pass ``ANSI16_DIR`` for the unified ANSI oracle.

    Raises:
        AssertionError: The fixture is missing or differs, spelling out the
            first differing row/cell. Never writes the fixture.
    """
    directory = GOLDEN_DIR if golden_dir is None else golden_dir
    path = directory / f"{name}.screen"
    if not path.is_file():
        raise AssertionError(
            f"golden file missing: {path}; the golden recorder owns fixture generation, "
            "this helper never creates or updates golden files"
        )
    expected = path.read_text(encoding="utf-8")
    if expected == actual:
        return None
    raise AssertionError(_describe_mismatch(name, path, expected, actual))


def assert_frame_in(directory: Path, name: str, actual: str) -> None:
    assert_frame(name, actual, golden_dir=directory)
