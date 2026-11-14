import concurrent.futures
import os
from pathlib import Path

import pytest

from tdnext.plugins.files.preview import MAX_BYTES, MAX_LINE_CHARS, MAX_LINES, read_preview


def test_preview_reads_real_utf8_lines_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("first\n第二行\nlast\n", encoding="utf-8")

    result = read_preview(source)

    assert result.kind == "Text file"
    assert result.lines == ("first", "第二行", "last")
    assert result.size == source.stat().st_size
    assert not result.truncated


@pytest.mark.parametrize("data", [b"text\x00binary", b"\xff\xfe", b"hello\x1b[31m"])
def test_preview_rejects_binary_and_terminal_control_bytes(tmp_path: Path, data: bytes) -> None:
    source = tmp_path / "binary"
    source.write_bytes(data)

    result = read_preview(source)

    assert result.kind == "Binary file"
    assert result.lines == ()


def test_preview_limits_bytes_without_breaking_utf8_boundary(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b"a" * (MAX_BYTES - 1) + "界".encode() + b"more")

    result = read_preview(source)

    assert result.kind == "Text file"
    assert result.truncated
    assert len(result.lines[0]) <= MAX_LINE_CHARS


def test_preview_limits_number_of_lines(tmp_path: Path) -> None:
    source = tmp_path / "many.txt"
    source.write_text("line\n" * (MAX_LINES + 1), encoding="utf-8")

    result = read_preview(source)

    assert len(result.lines) == MAX_LINES
    assert result.truncated


def test_preview_limits_long_lines_and_reports_truncation(tmp_path: Path) -> None:
    source = tmp_path / "long.txt"
    source.write_text("x" * (MAX_LINE_CHARS + 1), encoding="utf-8")

    result = read_preview(source)

    assert len(result.lines[0]) == MAX_LINE_CHARS
    assert result.lines[0].endswith("…")
    assert result.truncated


def test_preview_reports_symlink_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text("not shown through a link", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    result = read_preview(link)

    assert result.kind == "Link"
    assert str(target) in result.message
    assert not result.lines


def test_preview_fifo_returns_without_opening_or_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(read_preview, fifo).result(timeout=2)

    assert result.kind == "Special file"
    assert not result.lines


def test_preview_rejects_file_swapped_to_fifo_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "changing"
    source.write_text("ordinary", encoding="utf-8")
    real_open = os.open

    def swap_to_fifo(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if os.fspath(path) == os.fspath(source):
            source.unlink()
            os.mkfifo(source)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", swap_to_fifo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(read_preview, source).result(timeout=2)

    assert result.kind == "Unavailable"
    assert not result.lines


def test_preview_reports_missing_file(tmp_path: Path) -> None:
    result = read_preview(tmp_path / "missing")

    assert result.kind == "Unavailable"
    assert result.message.startswith("Cannot preview:")
