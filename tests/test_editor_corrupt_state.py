from pathlib import Path
from unittest.mock import Mock

import pytest
from test_settings_ui import make_app, text_of
from textual.events import AppFocus
from textual.widgets import ContentSwitcher, Select, Static

from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.files.editor_controls import EditorControls
from tdnext.plugins.files.editor_preferences import EditorPreferences
from tdnext.plugins.files.view import FilesView
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


@pytest.mark.anyio
async def test_desktop_remains_usable_when_editor_preferences_are_corrupt(tmp_path: Path) -> None:
    # Given a malformed preference file in an otherwise valid desktop state.
    editor_path = tmp_path / "editor.json"
    corrupt_bytes = b'{"editor": "nvim",\n'
    editor_path.write_bytes(corrupt_bytes)
    app, _manager = make_app(tmp_path)

    # When the desktop starts and the user opens Settings.
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        assert app.query_one(ContentSwitcher).current == "view-files"
        assert app.query_one(FilesView).is_on_screen
        assert app.query_one("#file-table").is_on_screen

        await pilot.press("alt+o")

        # Then Settings reports the editor problem, and navigation still works.
        assert app.query_one(ContentSwitcher).current == "view-settings"
        assert app.query_one(SettingsView).is_on_screen
        visible_text = " ".join(
            line.text for line in app.screen._compositor.render_strips()
        ).casefold()
        assert "editor" in visible_text
        assert any(
            word in visible_text
            for word in ("invalid", "corrupt", "malformed", "error", "cannot", "could not", "failed", "unable")
        ), "the editor preference error must be visible"
        assert any(
            hint in visible_text for hint in ("editor.json", "repair", "fix", "reset", "correct", "check")
        ) or any(
            selector.disabled and selector.is_on_screen for selector in app.query("#editor-choice")
        ), "the error must offer a recovery hint or disable the editor selector"

        await pilot.press("escape")
        assert app.query_one(ContentSwitcher).current == "view-files"
        assert app.query_one(FilesView).is_on_screen

    assert editor_path.read_bytes() == corrupt_bytes


@pytest.mark.anyio
@pytest.mark.parametrize("corrupt_bytes", [b"invalid-json\n", b'{"editor": "not-an-editor"}', b"\xff"])
async def test_native_settings_disables_unknown_editor_without_overwriting_bad_state(
    tmp_path: Path, corrupt_bytes: bytes
) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.write_bytes(corrupt_bytes)
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)

    async with app.run_test(size=(80, 24)) as pilot:
        selector = app.query_one("#editor-choice", Select)
        assert selector.disabled
        assert selector.value not in ("environment", "nvim", "vi")
        feedback = text_of(app.query_one("#plugin-feedback", Static))
        assert str(editor_path) in feedback
        assert "repair" in feedback.casefold()
        await pilot.press("tab", "tab", "tab")
        assert app.focused is not selector
        assert editor_path.read_bytes() == corrupt_bytes


@pytest.mark.anyio
@pytest.mark.parametrize("repair", ["replace", "delete"])
async def test_native_activation_recovers_from_external_repair_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair: str
) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.write_bytes(b"invalid-json\n")
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)

    async with app.run_test(size=(80, 24)) as pilot:
        writes: list[EditorPreferences] = []
        monkeypatch.setattr(app.query_one(EditorControls).store, "write", writes.append)
        selector = app.query_one("#editor-choice", Select)
        assert selector.disabled
        focused = app.focused
        if repair == "replace":
            editor_path.write_text('{"editor":"vi"}', encoding="utf-8")
        else:
            editor_path.unlink()

        app.post_message(AppFocus())
        await pilot.pause()

        assert not selector.disabled
        assert selector.value == ("vi" if repair == "replace" else "environment")
        assert app.focused is focused
        assert writes == []
        assert editor_path.exists() == (repair == "replace")


@pytest.mark.anyio
async def test_focusing_general_recovers_an_externally_repaired_editor(tmp_path: Path) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.write_bytes(b"invalid-json\n")
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)

    async with app.run_test(size=(80, 24)) as pilot:
        editor_path.write_text('{"editor":"nvim"}', encoding="utf-8")
        await pilot.press("tab")
        selector = app.query_one("#editor-choice", Select)
        assert not selector.disabled
        assert selector.value == "nvim"
        assert editor_path.read_text(encoding="utf-8") == '{"editor":"nvim"}'


@pytest.mark.anyio
async def test_returning_to_general_recovers_an_externally_repaired_editor(tmp_path: Path) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.write_bytes(b"invalid-json\n")
    app, _manager = make_app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        editor_path.write_text('{"editor":"vi"}', encoding="utf-8")
        await pilot.click("#section-general")
        selector = app.query_one("#editor-choice", Select)
        assert not selector.disabled
        assert selector.value == "vi"
        assert editor_path.read_text(encoding="utf-8") == '{"editor":"vi"}'


@pytest.mark.anyio
async def test_unreadable_preference_path_keeps_settings_usable(tmp_path: Path) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.mkdir()
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)

    async with app.run_test(size=(80, 24)):
        selector = app.query_one("#editor-choice", Select)
        assert selector.disabled
        assert selector.value not in ("environment", "nvim", "vi")
        assert str(editor_path) in text_of(app.query_one("#plugin-feedback", Static))
        assert editor_path.is_dir()


@pytest.mark.anyio
async def test_corruption_during_native_activation_disables_stale_editor(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    editor_path = tmp_path / "editor.json"

    async with app.run_test(size=(80, 24)) as pilot:
        editor_path.write_bytes(b"invalid-json\n")
        app.post_message(AppFocus())
        await pilot.pause()
        selector = app.query_one("#editor-choice", Select)
        assert selector.disabled
        assert selector.value not in ("environment", "nvim", "vi")
        assert str(editor_path) in text_of(app.query_one("#plugin-feedback", Static))
        assert editor_path.read_bytes() == b"invalid-json\n"


@pytest.mark.anyio
async def test_corruption_before_selecting_a_new_editor_does_not_overwrite_it(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    editor_path = tmp_path / "editor.json"

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.click("#editor-choice-frame", offset=(0, 1))
        editor_path.write_bytes(b"invalid-json\n")
        await pilot.press("down", "enter")
        selector = app.query_one("#editor-choice", Select)
        assert selector.disabled
        assert selector.value not in ("environment", "nvim", "vi")
        assert str(editor_path) in text_of(app.query_one("#plugin-feedback", Static))
        assert editor_path.read_bytes() == b"invalid-json\n"


@pytest.mark.anyio
async def test_finder_reports_corrupt_preferences_instead_of_launching_an_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor_path = tmp_path / "editor.json"
    editor_path.write_bytes(b"invalid-json\n")
    target = tmp_path / "document.txt"
    target.write_text("existing", encoding="utf-8")
    app, _manager = make_app(tmp_path)
    run_editor = Mock()
    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", run_editor)

    async with app.run_test(size=(80, 24)):
        messages: list[str] = []
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: messages.append(str(message)))
        app.query_one(FilesView).edit(target)
        assert len(messages) == 1
        assert str(editor_path) in messages[0]
        assert "Cannot" in messages[0]
        run_editor.assert_not_called()
        assert target.read_text(encoding="utf-8") == "existing"
        assert editor_path.read_bytes() == b"invalid-json\n"
