import os
from datetime import datetime
from pathlib import Path

import pytest
from rich.cells import cell_len
from test_files_browser import FilesApp, settle
from textual.widget import Widget

from tdnext.plugins.files.browser import FilesBrowser
from tdnext.plugins.files.browser_widgets import FileTable


def screen_line(app: FilesApp, widget: Widget, offset: int) -> str:
    strip = app.screen._compositor.render_strips()[widget.region.y + offset]
    return strip.crop(widget.region.x, widget.region.right).text


@pytest.mark.anyio
@pytest.mark.parametrize("name", [
    "a-very-long-filename-with-room-to-spare.txt",
    "資料報告書-完整的檔案名稱-2026.txt",
])
async def test_long_names_and_complete_metadata_render_without_overlap(tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    target.write_bytes(b"x" * 12_345)
    timestamp = 1_700_000_000
    os.utime(target, (timestamp, timestamp))
    modified = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    app = FilesApp(tmp_path)

    async with app.run_test(size=(200, 30)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        header = screen_line(app, table, 0)
        row = screen_line(app, table, 1)

        assert name in row
        assert "12,345 B" in row
        assert modified in row
        assert cell_len(row[:row.index("12,345 B")]) == cell_len(header[:header.index("Size")])
        assert cell_len(row[:row.index(modified)]) == cell_len(header[:header.index("Modified")])
        assert cell_len(header[header.index("Size"):header.index("Modified")]) == 11
        assert row.index(name) + len(name) < row.index("12,345 B") < row.index(modified)


@pytest.mark.anyio
@pytest.mark.parametrize("width,name", [
    (30, "a" * 22),
    (50, "b" * 27),
])
async def test_compact_table_gives_remaining_cells_to_filename(tmp_path: Path, width: int, name: str) -> None:
    (tmp_path / name).write_bytes(b"12345")
    app = FilesApp(tmp_path)

    async with app.run_test(size=(width, 24)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        header = screen_line(app, table, 0)
        row = screen_line(app, table, 1)

        assert name in row
        assert "Modified" not in header
        if "Size" in header:
            assert "5 B" in row


@pytest.mark.anyio
async def test_resize_moves_date_right_without_losing_selected_row(tmp_path: Path) -> None:
    for name in ("a-first.txt", "b-second-longer-than-twenty.txt"):
        (tmp_path / name).touch()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(160, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("down")
        table = app.query_one(FileTable)
        browser = app.query_one(FilesBrowser)
        selected = browser.selected()
        before_header = screen_line(app, table, 0)
        before_date = cell_len(before_header[:before_header.index("Modified")])
        before_row = screen_line(app, table, 2)

        await pilot.resize_terminal(200, 30)
        after_header = screen_line(app, table, 0)
        after_row = screen_line(app, table, 2)
        after_date = cell_len(after_header[:after_header.index("Modified")])

        assert selected == tmp_path / "b-second-longer-than-twenty.txt"
        assert browser.selected() == selected
        assert after_date > before_date
        assert "b-second-longer-than-twenty.txt" in after_row
        assert before_row != after_row
        x = table.region.x + 2
        assert app.screen.get_style_at(x, table.region.y + 1) != app.screen.get_style_at(x, table.region.y + 2)


@pytest.mark.anyio
async def test_scrollbar_does_not_clip_complete_date(tmp_path: Path) -> None:
    for index in range(40):
        (tmp_path / f"file-{index:02}.txt").touch()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(180, 24)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        row = screen_line(app, table, 1)
        modified = app.query_one(FilesBrowser).entries[0].modified

        assert table.show_vertical_scrollbar
        assert modified in row
        assert table.virtual_size.width <= table.scrollable_content_region.width


@pytest.mark.anyio
async def test_scrollbar_appearance_and_disappearance_reallocate_name_space(tmp_path: Path) -> None:
    for index in range(40):
        (tmp_path / f"file-{index:02}.txt").touch()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(180, 80)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        await pilot.press("down")
        selected = app.query_one(FilesBrowser).selected()
        expanded_header = screen_line(app, table, 0)
        expanded_date = cell_len(expanded_header[:expanded_header.index("Modified")])
        assert not table.show_vertical_scrollbar

        await pilot.resize_terminal(180, 24)
        compact_header = screen_line(app, table, 0)
        compact_date = cell_len(compact_header[:compact_header.index("Modified")])
        assert table.show_vertical_scrollbar
        assert not table.show_horizontal_scrollbar
        assert compact_date == expanded_date - table.scrollbar_size_vertical
        assert app.query_one(FilesBrowser).entries[1].modified in screen_line(app, table, 2)
        assert app.query_one(FilesBrowser).selected() == selected

        await pilot.resize_terminal(180, 80)
        assert not table.show_vertical_scrollbar
        assert screen_line(app, table, 0) == expanded_header
        assert app.query_one(FilesBrowser).selected() == selected


@pytest.mark.anyio
@pytest.mark.parametrize("size,visible", [
    (999_999, "999,999 B"),
    (1_000_000, "977 KiB"),
    (1_234_567, "1.2 MiB"),
])
async def test_size_metadata_fits_without_cutting_off_its_unit(tmp_path: Path, size: int, visible: str) -> None:
    target = tmp_path / "sparse-file"
    with target.open("wb") as stream:
        stream.truncate(size)
    app = FilesApp(tmp_path)

    async with app.run_test(size=(180, 24)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        row = screen_line(app, table, 1)

        assert visible in row
        assert app.query_one(FilesBrowser).entries[0].modified in row


@pytest.mark.anyio
@pytest.mark.parametrize("width", [50, 180], ids=["compact", "wide"])
async def test_section_separator_lines_stay_static_when_focus_moves(tmp_path: Path, width: int) -> None:
    (tmp_path / "file.txt").touch()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(width, 24)) as pilot:
        await settle(app, pilot)
        heading = app.query_one("#files-heading")
        before = screen_line(app, heading, 1)
        before_style = app.screen.get_style_at(heading.region.x + 2, heading.region.y + 1)
        await pilot.press("v")
        if width >= 96:
            await pilot.press("b")
        else:
            await pilot.press("v")
        after = screen_line(app, heading, 1)

        assert "─" * 8 in before
        assert after == before
        assert app.screen.get_style_at(heading.region.x + 2, heading.region.y + 1) == before_style
        if width >= 96:
            for selector in ("#parent-heading", "#preview-heading"):
                assert "─" * 8 in screen_line(app, app.query_one(selector), 1)
