"""Finder stays usable when the editor-preference error contains Textual markup.

Regression for the second crash: opening a file with an invalid editor.json
makes ``FilesView.edit`` notify ``Cannot start editor: ... [type=... ...]``.
With the default ``markup=True`` the Pydantic brackets raise
``textual.markup.MarkupError`` inside the Toast render and terminate the app.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from test_editor_launch import editor_store_for, make_context, make_manager
from textual.pilot import Pilot
from textual.widgets._toast import Toast

from tdnext.plugins.files.plugin import FilesPane
from tdnext.plugins.files.view import FilesView


async def settle(app: FilesPane, pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.anyio
async def test_corrupt_editor_preference_toast_shows_literal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "notes.txt"
    target.write_text("existing content", encoding="utf-8")
    manager = make_manager(tmp_path)
    context = make_context(tmp_path, workdir, manager)
    editor_path = editor_store_for(context).path
    corrupt_bytes = b"invalid-json\n"
    editor_path.write_bytes(corrupt_bytes)

    launched: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool, env: dict[str, str]) -> NoReturn:
        launched.append(list(command))
        raise AssertionError(f"editor must not launch when preferences are corrupt: check={check}, env={bool(env)}")

    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", fake_run)

    app = FilesPane(context)
    async with app.run_test(size=(100, 30), notifications=True) as pilot:
        await settle(app, pilot)
        assert app.query_one(FilesView).is_on_screen

        await pilot.press("e")
        await settle(app, pilot)
        await pilot.pause()

        # The app must still be alive with the Finder visible.
        assert app.is_running
        assert app.query_one(FilesView).is_on_screen

        # A real error notification was issued for the corrupt preference.
        assert len(app._notifications) == 1
        notification = next(iter(app._notifications))
        assert notification.severity == "error"
        assert "Cannot start editor" in notification.message
        assert str(editor_path) in notification.message
        # The Pydantic detail carries markup-significant brackets; it must be
        # preserved literally for the user to act on.
        assert "[type=" in notification.message
        assert "invalid-json" in notification.message

        # Waiting for the actual Toast render is the regression: with
        # markup=True this raises textual.markup.MarkupError and kills the app.
        toasts = app.query(Toast)
        assert len(toasts) >= 1
        rendered = toasts.first().render()
        assert "Cannot start editor" in rendered.plain
        assert "[type=" in rendered.plain
        assert "invalid-json" in rendered.plain

        visible = " ".join(line.text for line in app.screen._compositor.render_strips())
        assert "Cannot start editor" in visible
        assert "[type=" in visible or "invalid-json" in visible

    assert launched == []
    assert target.read_text(encoding="utf-8") == "existing content"
    assert editor_path.read_bytes() == corrupt_bytes
