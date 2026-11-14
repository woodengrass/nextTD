import os
import subprocess
import threading
from contextlib import nullcontext
from pathlib import Path

import anyio
import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.pilot import Pilot
from textual.widgets import Input, OptionList, Static

from tdnext.plugins.files import browser as browser_module
from tdnext.plugins.files.browser import FilesBrowser
from tdnext.plugins.files.browser_widgets import FileTable, PreviewPane, PreviewScroll
from tdnext.plugins.files.listing import scan_directory
from tdnext.plugins.files.preview import FilePreview, read_preview
from tdnext.plugins.files.view import FilesView


class FilesApp(App[None]):
    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory

    def compose(self) -> ComposeResult:
        yield FilesView(self.directory)


async def settle(app: FilesApp, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def rendered_text(widget: Static) -> str:
    content = widget.render()
    assert isinstance(content, Content)
    return content.plain


@pytest.mark.anyio
async def test_preview_displays_numbered_real_file_contents(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_text('{\n  "answer": 42\n}\n', encoding="utf-8")
    app = FilesApp(tmp_path)

    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        text = rendered_text(app.query_one("#preview-content", Static))

    assert "1 │ {" in text
    assert '2 │   "answer": 42' in text


@pytest.mark.anyio
async def test_preview_can_scroll_horizontally_to_read_long_lines(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("first " + "x" * 120 + " last", encoding="utf-8")
    app = FilesApp(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await settle(app, pilot)
        await pilot.press("v", "right", "right", "right")
        assert app.query_one(PreviewScroll).scroll_x > 0


@pytest.mark.anyio
async def test_latest_selection_wins_when_first_preview_finishes_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.txt").write_text(f"contents-{name}", encoding="utf-8")
    started, release = threading.Event(), threading.Event()
    requested: list[str] = []

    def delayed_preview(path: Path) -> FilePreview:
        requested.append(path.name)
        if path.name == "a.txt":
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("preview test timed out")
        return read_preview(path)

    monkeypatch.setattr(browser_module, "read_preview", delayed_preview)
    app = FilesApp(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            await pilot.press("down", "down")
            assert app.query_one(FilesBrowser).selected() == tmp_path / "c.txt"
        finally:
            release.set()
        await settle(app, pilot)
        text = rendered_text(app.query_one("#preview-content", Static))

    assert "contents-c" in text
    assert "contents-a" not in text and "contents-b" not in text
    assert requested == ["a.txt", "c.txt"]


@pytest.mark.anyio
async def test_slow_directory_scan_keeps_create_dialog_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()

    def delayed_scan(directory: Path, *, hidden: bool):
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("directory test timed out")
        return scan_directory(directory, hidden=hidden)

    monkeypatch.setattr(browser_module, "scan_directory", delayed_scan)
    app = FilesApp(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            await pilot.press("n")
            assert app.screen.query_one("#name-input")
            await pilot.press("escape")
        finally:
            release.set()
        await settle(app, pilot)


@pytest.mark.anyio
async def test_late_directory_result_cannot_replace_newer_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "old.txt").touch()
    (second / "new.txt").touch()
    started, release = threading.Event(), threading.Event()

    def delayed_scan(directory: Path, *, hidden: bool):
        if directory == first:
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("directory race timed out")
        return scan_directory(directory, hidden=hidden)

    monkeypatch.setattr(browser_module, "scan_directory", delayed_scan)
    app = FilesApp(first)
    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            view = app.query_one(FilesView)
            view.directory = second
            view.reload_directory()
        finally:
            release.set()
        await settle(app, pilot)
        assert [entry.path for entry in app.query_one(FilesBrowser).entries] == [second / "new.txt"]


@pytest.mark.anyio
async def test_resizing_preserves_reachable_preview_and_folder_panes(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    app = FilesApp(tmp_path)
    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        browser = app.query_one(FilesBrowser)
        assert browser.query_one("#file-parent").display
        assert browser.query_one(PreviewPane).display

        await pilot.resize_terminal(80, 24)
        assert not browser.query_one("#file-parent").display
        assert browser.query_one(PreviewPane).display
        assert browser.query_one("#file-main").display

        await pilot.resize_terminal(50, 24)
        assert not browser.query_one(PreviewPane).display
        await pilot.press("v")
        assert browser.query_one(PreviewPane).display
        assert not browser.query_one("#file-main").display
        assert app.focused is browser.query_one(PreviewScroll)
        await pilot.press("v")
        assert browser.query_one("#file-main").display
        assert app.focused is browser.query_one(FileTable)


@pytest.mark.anyio
async def test_parent_navigation_selects_previously_open_folder(tmp_path: Path) -> None:
    folder = tmp_path / "child"
    folder.mkdir()
    app = FilesApp(folder)
    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("h")
        await settle(app, pilot)
        view = app.query_one(FilesView)
        assert view.directory == tmp_path
        assert view.selected() == folder


def test_root_neighbour_list_does_not_invent_a_parent() -> None:
    snapshot = scan_directory(Path("/"), hidden=False)

    assert not snapshot.error
    assert snapshot.neighbours == (Path("/"),)


@pytest.mark.anyio
async def test_hidden_toggle_keeps_selection_and_reveals_dotfiles(tmp_path: Path) -> None:
    visible = tmp_path / "visible.txt"
    visible.touch()
    (tmp_path / ".hidden").touch()
    app = FilesApp(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("period")
        await settle(app, pilot)
        browser = app.query_one(FilesBrowser)
        assert {entry.path.name for entry in browser.entries} == {"visible.txt", ".hidden"}
        assert browser.selected() == visible


@pytest.mark.anyio
async def test_go_to_path_dialog_navigates_to_real_directory(tmp_path: Path) -> None:
    target = tmp_path / "destination"
    target.mkdir()
    app = FilesApp(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("g")
        app.screen.query_one(Input).value = str(target)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.query_one(FilesView).directory == target


@pytest.mark.anyio
async def test_rename_dialog_changes_real_file(tmp_path: Path) -> None:
    source = tmp_path / "before.txt"
    source.write_text("unchanged content", encoding="utf-8")
    app = FilesApp(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("r")
        app.screen.query_one(Input).value = "after.txt"
        await pilot.press("enter")
        await settle(app, pilot)
    assert (tmp_path / "after.txt").read_text(encoding="utf-8") == "unchanged content"
    assert not source.exists()


@pytest.mark.anyio
async def test_editor_uses_existing_command_and_selected_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("existing content", encoding="utf-8")
    commands: list[list[str]] = []

    def run_editor(
        command: list[str], *, check: bool, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert env is not None
        from tdnext.core.local_env import is_bootstrapped, original_path

        present, value = original_path()
        if is_bootstrapped():
            assert env.get("PATH", "<unset>") == (value if present else "<unset>")
        else:
            assert env.get("PATH", "<unset>") == os.environ.get("PATH", "<unset>")
        assert "TDNEXT_PATH_BOOTSTRAPPED" not in env
        assert "TDNEXT_ORIGINAL_PATH" not in env
        assert "TDNEXT_ORIGINAL_PATH_PRESENT" not in env
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("EDITOR", "editor --flag")
    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", run_editor)
    app = FilesApp(tmp_path)
    monkeypatch.setattr(app, "suspend", nullcontext)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        await settle(app, pilot)

    assert commands == [["editor", "--flag", str(source)]]
    assert source.read_text(encoding="utf-8") == "existing content"


@pytest.mark.anyio
async def test_truncated_preview_discloses_limit_in_metadata(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("x" * 1000, encoding="utf-8")
    app = FilesApp(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await settle(app, pilot)
        assert "truncated" in rendered_text(app.query_one("#preview-meta", Static))


@pytest.mark.anyio
async def test_missing_directory_error_is_visible_in_narrow_list(tmp_path: Path) -> None:
    app = FilesApp(tmp_path / "missing")
    async with app.run_test(size=(50, 24)) as pilot:
        await settle(app, pilot)
        status = app.query_one("#file-status", Static)
        assert status.display
        assert "Cannot read directory" in rendered_text(status)


@pytest.mark.anyio
async def test_mouse_opens_neighbour_directory(tmp_path: Path) -> None:
    current = tmp_path / "alpha"
    current.mkdir()
    neighbour = tmp_path / "beta"
    neighbour.mkdir()
    app = FilesApp(current)
    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        browser = app.query_one(FilesBrowser)
        index = browser.neighbours.index(neighbour)
        await pilot.click("#neighbours", offset=(3, index))
        await settle(app, pilot)
        assert app.query_one(FilesView).directory == neighbour


@pytest.mark.anyio
async def test_folder_shortcut_focuses_neighbour_list(tmp_path: Path) -> None:
    app = FilesApp(tmp_path)
    async with app.run_test(size=(130, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("b")
        assert app.focused is app.query_one(OptionList)


@pytest.mark.anyio
@pytest.mark.parametrize("move", [False, True], ids=["copy", "cut"])
async def test_keyboard_copy_or_cut_and_paste_real_file(tmp_path: Path, move: bool) -> None:
    source_dir, destination = tmp_path / "source", tmp_path / "destination"
    source_dir.mkdir()
    destination.mkdir()
    source = source_dir / "notes.txt"
    source.write_text("actual content", encoding="utf-8")
    app = FilesApp(source_dir)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        await pilot.press("x" if move else "y", "h")
        await settle(app, pilot)
        browser = app.query_one(FilesBrowser)
        row = [entry.path for entry in browser.entries].index(destination)
        browser.query_one(FileTable).move_cursor(row=row)
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("p")
        await settle(app, pilot)

    assert (destination / "notes.txt").read_text(encoding="utf-8") == "actual content"
    assert source.exists() is not move
