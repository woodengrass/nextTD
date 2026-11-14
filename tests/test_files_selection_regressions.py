from pathlib import Path

import pytest
from test_files_browser import FilesApp, settle
from textual.coordinate import Coordinate
from textual.widgets import DataTable, OptionList

from tdnext.plugins.files.browser_widgets import FileTable
from tdnext.plugins.files.view import FilesView


@pytest.mark.anyio
async def test_queued_enter_opens_the_row_selected_before_cursor_moves(tmp_path: Path) -> None:
    first, second = tmp_path / "a-first", tmp_path / "b-second"
    first.mkdir()
    second.mkdir()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        table.action_select_cursor()
        table.move_cursor(row=1)
        await settle(app, pilot)

        assert app.query_one(FilesView).directory == first


@pytest.mark.anyio
async def test_buffered_enter_then_down_opens_original_directory(tmp_path: Path) -> None:
    first, second = tmp_path / "a-first", tmp_path / "b-second"
    first.mkdir()
    second.mkdir()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("enter", "down")
        await settle(app, pilot)

        assert app.query_one(FilesView).directory == first


@pytest.mark.anyio
async def test_selection_event_from_previous_listing_is_ignored(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "old-folder").mkdir()
    (second / "new-folder").mkdir()
    app = FilesApp(first)

    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        old_key = table.coordinate_to_cell_key(Coordinate(0, 0)).row_key
        old_event = DataTable.RowSelected(table, 0, old_key)
        view = app.query_one(FilesView)
        view.directory = second
        view.reload_directory()
        await settle(app, pilot)
        table.post_message(old_event)
        await settle(app, pilot)

        assert view.directory == second


@pytest.mark.anyio
async def test_neighbour_event_from_previous_listing_is_ignored(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    app = FilesApp(first)

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        options = app.query_one(OptionList)
        stale = OptionList.OptionSelected(options, options.options[0], 0)
        view = app.query_one(FilesView)
        view.directory = second
        view.reload_directory()
        await settle(app, pilot)
        options.post_message(stale)
        await settle(app, pilot)

        assert view.directory == second


@pytest.mark.anyio
async def test_filename_with_markup_is_shown_literally(tmp_path: Path) -> None:
    filename = "[bold]literal.txt"
    (tmp_path / filename).touch()
    app = FilesApp(tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        table = app.query_one(FileTable)
        assert filename in table.render_line(1).text
