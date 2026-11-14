import tempfile
from collections.abc import Iterator
from pathlib import Path
from time import monotonic

import anyio
import pytest
from test_files_browser import FilesApp

from tdnext.plugins.files.browser import FilesBrowser
from tdnext.plugins.files.browser_widgets import FileTable
from tdnext.plugins.files.view import FilesView

ENTRY_COUNT = 100_000


@pytest.fixture(scope="module")
def large_directory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tdnext-large-", dir=tmp_path_factory.getbasetemp()) as directory:
        root = Path(directory)
        for index in range(ENTRY_COUNT):
            (root / f"file-{index:06}.txt").touch()
        yield root


@pytest.mark.anyio
async def test_large_real_directory_keeps_heartbeat_during_population_and_resize(large_directory: Path) -> None:
    app = FilesApp(large_directory)
    samples: list[tuple[float, int]] = []
    complete = anyio.Event()
    after_resize = anyio.Event()
    resizing_done = False

    async with app.run_test(size=(130, 30)) as pilot:
        table = app.query_one(FileTable)

        def heartbeat() -> None:
            samples.append((monotonic(), table.row_count))
            if table.row_count == ENTRY_COUNT:
                complete.set()
            if resizing_done:
                after_resize.set()

        timer = app.set_interval(0.02, heartbeat)
        with anyio.fail_after(120):
            await complete.wait()
        load_samples = list(samples)
        table.move_cursor(row=ENTRY_COUNT - 1, scroll=False)
        selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        started = monotonic()
        await pilot.resize_terminal(50, 24)
        resize_seconds = monotonic() - started
        resizing_done = True
        with anyio.fail_after(10):
            await after_resize.wait()
        timer.stop()

        assert table.row_count == ENTRY_COUNT
        assert table.entry_for_key(selected_key) == table.entries[-1]
        assert table.coordinate_to_cell_key(table.cursor_coordinate).row_key == selected_key

    gaps = [
        (later[0] - earlier[0], earlier[1], later[1])
        for earlier, later in zip(load_samples, load_samples[1:], strict=False)
    ]
    max_gap, before_count, after_count = max(gaps, default=(0.0, 0, 0))
    load_seconds = load_samples[-1][0] - load_samples[0][0]
    print(
        f"100k heartbeat: load={load_seconds:.3f}s samples={len(load_samples)} "
        f"max_gap={max_gap:.3f}s rows={before_count}->{after_count} resize={resize_seconds:.3f}s"
    )
    assert any(0 < count < ENTRY_COUNT for _, count in load_samples), "population never yielded to heartbeat"
    assert max_gap < 0.75, f"population blocked the heartbeat for {max_gap:.3f}s"
    assert resize_seconds < 0.75, f"resize blocked the UI for {resize_seconds:.3f}s"


@pytest.mark.anyio
async def test_navigation_cancels_incomplete_large_listing(large_directory: Path, tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    expected = destination / "current.txt"
    expected.touch()
    app = FilesApp(large_directory)
    partial = anyio.Event()

    async with app.run_test(size=(130, 30)) as pilot:
        table = app.query_one(FileTable)

        def observe_population() -> None:
            if 0 < table.row_count < ENTRY_COUNT:
                partial.set()

        timer = app.set_interval(0.01, observe_population)
        with anyio.fail_after(30):
            await partial.wait()
        timer.stop()
        view = app.query_one(FilesView)
        view.directory = destination
        view.reload_directory()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        browser = app.query_one(FilesBrowser)
        assert view.directory == destination
        assert table.row_count == 1
        assert browser.selected() == expected


@pytest.mark.anyio
async def test_pending_selection_never_targets_first_partially_loaded_row(large_directory: Path) -> None:
    app = FilesApp(large_directory)
    waiting = anyio.Event()

    async with app.run_test(size=(130, 30)) as pilot:
        view = app.query_one(FilesView)
        view.reload_directory(large_directory / "file-090000.txt")
        table = app.query_one(FileTable)

        def observe_selection() -> None:
            if table.selection_pending and 0 < table.row_count < 90_000:
                waiting.set()

        timer = app.set_interval(0.01, observe_selection)
        with anyio.fail_after(30):
            await waiting.wait()
        timer.stop()
        assert app.query_one(FilesBrowser).selected() is None
        await pilot.press("x")
        assert view.clipboard is None
        await pilot.press("down", "x")
        assert view.clipboard is not None
        assert view.clipboard.path == large_directory / "file-000001.txt"
