from pathlib import Path

import anyio
import pytest
from test_files_browser import FilesApp, settle
from test_files_delete_timing import hold_scan
from test_ui import desktop
from textual.events import Key
from textual.message import Message
from textual.notifications import Notify
from textual.widgets import Button

from tdnext.plugins.files import browser as browser_module
from tdnext.plugins.files.pending_delete import PendingDelete
from tdnext.plugins.files.view import FilesView


@pytest.mark.anyio
async def test_empty_folder_never_enables_pending_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown = anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        assert await anyio.to_thread.run_sync(started.wait, 2)
        app.screen_change_signal.subscribe(
            app, lambda screen: shown.set() if isinstance(screen, PendingDelete) else None, immediate=True
        )
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pilot.press, "d")
            try:
                with anyio.fail_after(1):
                    await shown.wait()
                    await app.screen._mounted_event.wait()
                assert app.screen.query_one("#delete", Button).disabled
            finally:
                release.set()
        pending = app.screen
        assert isinstance(pending, PendingDelete)
        assert pending.target is None
        assert pending.query_one("#delete", Button).disabled
        await pilot.press("escape")


@pytest.mark.anyio
async def test_cut_during_slow_scan_returns_without_waiting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "keep.txt").touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    processed = anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)

            def on_message(message: Message) -> None:
                if isinstance(message, Notify) and message.notification.severity == "warning":
                    processed.set()

            app.message_signal.subscribe(app, on_message, immediate=True)
            app.post_message(Key("x", "x"))
            with anyio.fail_after(1):
                await processed.wait()
            assert not release.is_set()
            assert app.query_one(FilesView).clipboard is None
            assert any(item.severity == "warning" for item in app._notifications)
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()


@pytest.mark.anyio
async def test_quit_during_pending_delete_does_not_wait_for_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "keep.txt").touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown = anyio.Event()

    try:
        with anyio.fail_after(2):
            async with app.run_test(size=(100, 30)) as pilot:
                assert await anyio.to_thread.run_sync(started.wait, 2)
                app.screen_change_signal.subscribe(
                    app, lambda screen: shown.set() if isinstance(screen, PendingDelete) else None, immediate=True
                )
                app.post_message(Key("d", "d"))
                await shown.wait()
                await app.screen._mounted_event.wait()
                await pilot.press("ctrl+q")
                assert not app.is_running
            assert not release.is_set()
    finally:
        release.set()


@pytest.mark.anyio
async def test_external_screen_pop_cancels_waiter_without_waiting_for_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "keep.txt"
    target.touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(tmp_path)
    shown, processed = anyio.Event(), anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        try:
            assert await anyio.to_thread.run_sync(started.wait, 2)
            original_screen = app.screen
            app.screen_change_signal.subscribe(
                app, lambda screen: shown.set() if isinstance(screen, PendingDelete) else None, immediate=True
            )
            app.post_message(Key("d", "d"))
            with anyio.fail_after(1):
                await shown.wait()
                await app.screen._mounted_event.wait()
            app.pop_screen()
            app.call_later(processed.set)
            with anyio.fail_after(1):
                await processed.wait()
            assert app.screen is original_screen
            assert not release.is_set()
        finally:
            release.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert target.exists()


@pytest.mark.anyio
async def test_empty_listing_with_missing_restore_target_finishes_loading(tmp_path: Path) -> None:
    app = FilesApp(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one(FilesView)
        view.reload_directory(tmp_path / "already-gone.txt")
        await settle(app, pilot)
        browser = app.query_one(browser_module.FilesBrowser)

        assert not browser.selection_loading
        with anyio.fail_after(1):
            assert await browser.wait_for_selection(browser.selection_revision) is None


@pytest.mark.anyio
async def test_resolved_pending_confirmation_cannot_delete_after_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    old_file, new_file = old / "old.txt", new / "new.txt"
    old_file.touch()
    new_file.touch()
    started, release = hold_scan(monkeypatch)
    app = desktop(old)
    shown = anyio.Event()

    async with app.run_test(size=(100, 30)) as pilot:
        assert await anyio.to_thread.run_sync(started.wait, 2)
        app.screen_change_signal.subscribe(
            app, lambda screen: shown.set() if isinstance(screen, PendingDelete) else None, immediate=True
        )
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pilot.press, "d")
            try:
                with anyio.fail_after(1):
                    await shown.wait()
            finally:
                release.set()
        pending = app.screen
        assert isinstance(pending, PendingDelete)
        assert pending.target == old_file
        view = app.query_one(FilesView)
        view.directory = new
        view.reload_directory()
        await app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.click("#delete")
        await pilot.pause()
        assert old_file.exists()
        assert new_file.exists()
