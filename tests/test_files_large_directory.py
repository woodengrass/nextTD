"""Native large-directory coverage through the real ``tdnext-files`` binary.

The Textual ``DataTable`` harness is retired: ``src/tdnext/plugins/files``
no longer ships ``browser``, ``browser_widgets`` or ``view``, so the old
``row_count``/``selection_pending`` heartbeat observations have no Rust
counterpart. Rust lists a directory atomically on a background thread
(``controller.rs`` ``spawn_list``) while the event thread keeps answering
input. These tests prove, on the real surface at real scale, that the UI
answers input, the first and last entries become reachable, a resize keeps
the listing usable, and navigating away settles on the newer directory.
They do not claim the help key was handled mid-scan, do not claim every
entry was visited, and do not measure incremental row counts. The
deterministic stale-reply ordering (an older scan must not overwrite a
newer navigation while a worker is blocked) is guarded by the controller
suite ``late_listing_cannot_replace_directory_opened_during_scan``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from time import monotonic

import pytest
from files_pty import FilesPty, require_files_binary

ENTRY_COUNT = 20_000


@pytest.fixture(scope="module")
def large_directory(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="tdnext-large-", dir=tmp_path_factory.getbasetemp()) as directory:
        root = Path(directory)
        for index in range(ENTRY_COUNT):
            (root / f"file-{index:05d}.txt").touch()
        yield root


def test_large_real_directory_answers_input_and_reaches_first_and_last_entries(
    large_directory: Path, tmp_path: Path
) -> None:
    """B1: the UI answers input at 20,000-entry scale (help opens).

    B2: the first entry (via ``Home``) and the last entry (via ``End``)
    become visible afterwards. B3: a resize keeps the listing usable.
    """
    binary = require_files_binary()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    first = "file-00000.txt"
    last = f"file-{ENTRY_COUNT - 1:05d}.txt"
    started = monotonic()
    pty = FilesPty(binary, large_directory, state_dir)
    try:
        pty.send("?")
        pty.wait_for("File shortcuts")
        pty.send("Escape")
        pty.wait_for(first, timeout=120.0)
        pty.send("End")
        pty.wait_for(last, timeout=60.0)
        pty.send("Home")
        pty.wait_for(first, timeout=60.0)
        load_seconds = monotonic() - started
        print(f"\nlarge-dir: {ENTRY_COUNT} entries, first/last reachable in {load_seconds:.3f}s")
        assert load_seconds < 120.0, f"large directory took {load_seconds:.3f}s to list"
        pty.resize(80, 24)
        pty.wait_for(first)
        pty.resize(130, 30)
        pty.wait_for(first)
    finally:
        pty.close()


def test_navigation_from_large_directory_settles_on_newer_directory(
    large_directory: Path, tmp_path: Path
) -> None:
    """B1: navigating away from the large directory lands on the newer directory.

    B2: no entry from the older large scan is shown afterwards, and the new
    directory keeps answering input. The scan may already have finished by
    the time navigation completes; the stale-reply race itself is covered
    deterministically by ``late_listing_cannot_replace_directory_opened_during_scan``.
    """
    binary = require_files_binary()
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "current.txt").touch()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pty = FilesPty(binary, large_directory, state_dir)
    try:
        pty.send("g")
        pty.wait_for("Go to folder")
        pty.type_text(str(destination))
        pty.send("Enter")
        frame = pty.wait_for("current.txt", timeout=60.0)
        assert str(destination) in frame
        assert f"file-{ENTRY_COUNT - 1:05d}.txt" not in frame
        pty.send("?")
        pty.wait_for("File shortcuts")
        pty.send("Escape")
        frame = pty.wait_for("current.txt", timeout=30.0)
        assert str(destination) in frame
        assert f"file-{ENTRY_COUNT - 1:05d}.txt" not in frame
    finally:
        pty.close()
