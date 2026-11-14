from pathlib import Path

import pytest
from test_files_browser import FilesApp, settle
from textual.widgets import OptionList

from tdnext.plugins.files import filesystem as fs
from tdnext.plugins.files.browser import FilesBrowser
from tdnext.plugins.files.browser_widgets import FileTable, PreviewScroll
from tdnext.plugins.files.dialogs import ConfirmDelete
from tdnext.plugins.files.view import FilesView


@pytest.mark.anyio
@pytest.mark.parametrize("shortcut", ["x", "y"])
async def test_folder_focus_does_not_copy_or_cut_file_table_selection(tmp_path: Path, shortcut: str) -> None:
    current = tmp_path / "current"
    current.mkdir()
    target = current / "important.txt"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / "neighbour").mkdir()
    app = FilesApp(current)

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("b")
        assert app.focused is app.query_one(OptionList)
        await pilot.press(shortcut)
        assert app.query_one(FilesView).clipboard is None
        assert any(item.severity == "warning" for item in app._notifications)

    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.anyio
async def test_preview_resize_round_trip_keeps_focus_and_toggle_reachable(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    app = FilesApp(tmp_path)

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        await pilot.resize_terminal(50, 24)
        await pilot.press("v")
        assert app.focused is app.query_one(PreviewScroll)
        await pilot.resize_terminal(130, 30)
        await pilot.press("v")
        assert app.focused is app.query_one(FileTable)
        await pilot.resize_terminal(50, 24)
        browser = app.query_one(FilesBrowser)
        assert app.focused is browser.query_one(PreviewScroll)
        await pilot.press("v")
        assert app.focused is browser.query_one(FileTable)
        assert browser.query_one("#file-main").display


@pytest.mark.anyio
async def test_folder_focus_does_not_delete_file_table_selection(tmp_path: Path) -> None:
    current = tmp_path / "current"
    current.mkdir()
    target = current / "important.txt"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / "neighbour").mkdir()
    app = FilesApp(current)

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("b", "d")
        assert not isinstance(app.screen, ConfirmDelete)
        assert any(item.severity == "warning" for item in app._notifications)

    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.anyio
async def test_immediate_folder_focus_then_delete_cannot_target_table_file(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("keep", encoding="utf-8")
    app = FilesApp(tmp_path)

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        view = app.query_one(FilesView)
        view.action_folders()
        await view.action_delete()
        assert not isinstance(app.screen, ConfirmDelete)
        assert view.clipboard is None

    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.anyio
@pytest.mark.parametrize("shortcut", ["n", "f", "r", "e", "p"])
async def test_folder_focus_blocks_other_file_actions(
    tmp_path: Path, shortcut: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    (current / "selected.txt").touch()
    source = tmp_path / "source.txt"
    source.write_text("keep", encoding="utf-8")
    app = FilesApp(current)
    edits: list[Path] = []

    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        view = app.query_one(FilesView)
        view.clipboard = fs.Clipboard(source, move=False)
        monkeypatch.setattr(view, "edit", edits.append)
        base_screen = app.screen
        await pilot.press("b", shortcut)

        assert app.screen is base_screen
        assert not (current / source.name).exists()
        assert not edits
        assert any(item.severity == "warning" for item in app._notifications)
