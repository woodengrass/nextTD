import threading
from pathlib import Path

import anyio
import pytest
from test_ui import desktop
from textual.events import Click, Key
from textual.screen import Screen
from textual.widgets import Button, DataTable, Static

from tdnext.plugins.files import browser as browser_module
from tdnext.plugins.files.dialogs import ConfirmDelete
from tdnext.plugins.files.listing import DirectorySnapshot, scan_directory
from tdnext.plugins.files.pending_delete import PendingDelete
from tdnext.plugins.files.view import FilesView


def hold_scan(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    started, release = threading.Event(), threading.Event()

    def gated(directory: Path, *, hidden: bool) -> DirectorySnapshot:
        started.set()
        if not release.wait(timeout=10):
            raise TimeoutError
        return scan_directory(directory, hidden=hidden)

    monkeypatch.setattr(browser_module, "scan_directory", gated)
    return started, release


@pytest.mark.anyio
async def test_delete_during_slow_scan_offers_named_confirmation_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "remove.txt"
    target.touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown, painted, still_loading = anyio.Event(), anyio.Event(), anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        assert await anyio.to_thread.run_sync(started.wait, 2)
        app.query_one(DataTable).focus()

        def on_screen(screen: Screen) -> None:
            if isinstance(screen, ConfirmDelete):
                shown.set()

        app.screen_change_signal.subscribe(app, on_screen, immediate=True)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pilot.press, "d")
            try:
                with anyio.fail_after(1):
                    await shown.wait()
                    await app.screen._mounted_event.wait()
                app.screen.call_after_refresh(painted.set)
                with anyio.fail_after(1):
                    await painted.wait()
                app.set_timer(0.25, still_loading.set)
                with anyio.fail_after(1):
                    await still_loading.wait()
                assert app.screen.query_one("#delete", Button).disabled
                detail = app.screen.query_one("#pending-delete-detail", Static)
                assert detail.size.height > 0
                assert "Loading selected file" in detail.render_line(0).text
                assert not release.is_set()
                assert target.exists()
            finally:
                release.set()

        assert isinstance(app.screen, ConfirmDelete)
        assert app.screen.target_name == target.name
        assert not app.screen.query_one("#delete", Button).disabled
        await pilot.click("#delete")
        await pilot.pause()
        assert not target.exists()


@pytest.mark.anyio
async def test_escape_cancels_pending_delete_without_waiting_for_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keep.txt"
    target.touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown, returned = anyio.Event(), anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            original_screen = app.screen

            def on_screen(screen: Screen) -> None:
                if isinstance(screen, ConfirmDelete):
                    shown.set()

            app.screen_change_signal.subscribe(app, on_screen, immediate=True)
            app.post_message(Key("d", "d"))
            with anyio.fail_after(1):
                await shown.wait()
            app.post_message(Key("escape", None))
            app.call_later(lambda: returned.set() if app.screen is original_screen else None)
            with anyio.fail_after(1):
                await returned.wait()
            assert not release.is_set()
            assert target.exists()
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen is original_screen
        assert target.exists()


@pytest.mark.anyio
async def test_mouse_cancel_works_before_slow_scan_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "keep.txt"
    target.touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown, painted, returned = anyio.Event(), anyio.Event(), anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            original_screen = app.screen
            def on_screen(screen: Screen) -> None:
                if isinstance(screen, ConfirmDelete):
                    shown.set()
                if screen is original_screen:
                    returned.set()

            app.screen_change_signal.subscribe(app, on_screen, immediate=True)
            app.post_message(Key("d", "d"))
            with anyio.fail_after(1):
                await shown.wait()
                await app.screen._mounted_event.wait()
            assert app.screen.query_one("#delete", Button).disabled
            app.screen.call_after_refresh(painted.set)
            with anyio.fail_after(1):
                await painted.wait()
            cancel = app.screen.query_one("#cancel", Button)
            x, y = cancel.region.x + 1, cancel.region.y + 1
            app.post_message(Click(None, x, y, 0, 0, 1, False, False, False))
            with anyio.fail_after(1):
                await returned.wait()
            assert app.screen is original_screen
            assert not release.is_set()
            worker = app.query_one(browser_module.FilesBrowser)._listing_worker
            assert worker is not None and not worker.is_finished
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert target.exists()


@pytest.mark.anyio
async def test_pending_delete_never_targets_newer_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "old.txt").touch()
    (new / "new.txt").touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(old)
    shown = anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            app.screen_change_signal.subscribe(
                app, lambda screen: shown.set() if isinstance(screen, PendingDelete) else None, immediate=True
            )
            app.post_message(Key("d", "d"))
            with anyio.fail_after(1):
                await shown.wait()
                await app.screen._mounted_event.wait()
            pending = app.screen
            assert isinstance(pending, PendingDelete)
            view = app.query_one(FilesView)
            view.directory = new
            view.reload_directory()
            with anyio.fail_after(1):
                await pending._waiter._mounted_event.wait()
            assert pending.target is None
            assert pending.query_one("#delete", Button).disabled
            assert not release.is_set()
        finally:
            release.set()
        await pilot.press("escape")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert (old / "old.txt").exists()
        assert (new / "new.txt").exists()
