from pathlib import Path

import pytest
from test_settings_ui import make_app, text_of
from textual.containers import VerticalScroll
from textual.events import AppFocus
from textual.widgets import Select, Static
from textual.widgets._select import SelectOverlay

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.files.editor_preferences import EditorChoice, EditorPreferences, EditorPreferencesStore
from tdnext.plugins.settings.editor import EditorControls
from tdnext.plugins.settings.plugin import SettingsPlugin


@pytest.mark.anyio
async def test_default_editor_is_displayed_without_creating_a_preference(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    path = manager.store.path.with_name("editor.json")
    async with app.run_test(size=(80, 24)):
        assert app.focused is app.query_one("#icon-provider", Select)
        assert app.query_one("#editor-choice", Select).value == "environment"
        assert not path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("initial", "keys", "expected"),
    [
        ("environment", ("down",), "nvim"),
        ("environment", ("down", "down"), "vi"),
        ("vi", ("up", "up"), "environment"),
    ],
)
async def test_keyboard_choice_survives_a_new_settings_pane(
    tmp_path: Path, initial: EditorChoice, keys: tuple[str, ...], expected: EditorChoice
) -> None:
    desktop, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    store.write(EditorPreferences(editor=initial))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("tab", "tab", "tab")
        assert app.focused is app.query_one("#editor-choice", Select)
        await pilot.press("enter", *keys, "enter")
        assert store.read().editor == expected
        assert "next file open" in text_of(app.query_one("#plugin-feedback", Static))
    reopened = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with reopened.run_test(size=(80, 24)):
        assert reopened.query_one("#editor-choice", Select).value == expected


@pytest.mark.anyio
@pytest.mark.parametrize("position", ["left", "label", "right", "top", "bottom"])
async def test_editor_frame_opens_with_pointer_and_saves_selected_option(tmp_path: Path, position: str) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(120, 36)) as pilot:
        frame = app.query_one("#editor-choice-frame")
        selector = app.query_one("#editor-choice", Select)
        offsets = {
            "left": (0, 1), "label": (1, 1), "right": (frame.region.width - 1, 1),
            "top": (1, 0), "bottom": (1, 2),
        }
        border = frame.styles.border
        await pilot.click(frame, offset=offsets[position])
        assert selector.expanded
        await pilot.click(selector.query_one(SelectOverlay), offset=(1, 1))
        assert not selector.expanded
        assert app.focused is selector
        assert frame.styles.border == border
        assert EditorPreferencesStore(manager.store.path.with_name("editor.json")).read().editor == "nvim"


@pytest.mark.anyio
async def test_selecting_current_editor_closes_without_a_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(120, 36)) as pilot:
        controls = app.query_one(EditorControls)
        writes: list[EditorPreferences] = []
        monkeypatch.setattr(controls.store, "write", writes.append)
        await pilot.click("#editor-choice-frame", offset=(0, 1))
        selector = app.query_one("#editor-choice", Select)
        await pilot.click(selector.query_one(SelectOverlay), offset=(1, 0))
        assert not selector.expanded
        assert app.focused is selector
        assert writes == []


@pytest.mark.anyio
async def test_tab_traversal_leaves_editor_and_reverses_without_a_focus_loop(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("tab", "tab", "tab", "tab")
        assert app.focused is app.query_one("#settings-back")
        await pilot.press("shift+tab")
        assert app.focused is app.query_one("#editor-choice", Select)
        await pilot.press("shift+tab", "shift+tab")
        assert app.focused is app.query_one("#icon-mode", Select)


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_keyboard_reveals_full_editor_frame_at_each_size(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        await pilot.press("tab", "tab", "tab")
        editor = app.query_one("#editor-choice", Select)
        frame = app.query_one("#editor-choice-frame")
        scroll = app.query_one("#settings-general", VerticalScroll)
        assert app.focused is editor
        assert scroll.content_region.contains_region(frame.region)
        assert all(side[0] != "none" for side in frame.styles.border)
        await pilot.press("enter", "down", "enter")
        assert editor.value == "nvim"
        await pilot.press("shift+tab")
        assert app.focused is app.query_one("#settings-feedback-scroll")


@pytest.mark.anyio
async def test_native_activation_refreshes_without_writes_or_focus_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        controls = app.query_one(EditorControls)
        writes: list[EditorPreferences] = []
        monkeypatch.setattr(controls.store, "write", writes.append)
        focus = app.focused
        store.write(EditorPreferences(editor="vi"))
        app.post_message(AppFocus())
        await pilot.pause()
        assert app.query_one("#editor-choice", Select).value == "vi"
        assert app.focused is focus
        assert writes == []


@pytest.mark.anyio
async def test_focusing_an_appearance_selector_refreshes_editor_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        writes: list[EditorPreferences] = []
        monkeypatch.setattr(app.query_one(EditorControls).store, "write", writes.append)
        store.write(EditorPreferences(editor="nvim"))
        await pilot.press("tab")
        assert app.query_one("#editor-choice", Select).value == "nvim"
        assert writes == []


@pytest.mark.anyio
async def test_returning_to_general_refreshes_editor_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#section-plugins")
        store.write(EditorPreferences(editor="vi"))
        writes: list[EditorPreferences] = []
        monkeypatch.setattr(app.query_one(EditorControls).store, "write", writes.append)
        await pilot.click("#section-general")
        assert app.query_one("#editor-choice", Select).value == "vi"
        assert app.focused is app.query_one("#icon-provider", Select)
        assert writes == []


@pytest.mark.anyio
async def test_failed_save_restores_latest_saved_editor_and_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(120, 36)) as pilot:
        controls = app.query_one(EditorControls)

        def fail_write(_preferences: EditorPreferences) -> None:
            raise PluginError("disk is read-only")

        monkeypatch.setattr(controls.store, "write", fail_write)
        await pilot.click("#editor-choice-frame", offset=(0, 1))
        store.write(EditorPreferences(editor="vi"))
        await pilot.press("down", "enter")
        assert store.read().editor == "vi"
        assert app.query_one("#editor-choice", Select).value == "vi"
        assert "not saved" in text_of(app.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_reopening_desktop_settings_refreshes_the_saved_editor(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o", "escape")
        store.write(EditorPreferences(editor="nvim"))
        await pilot.press("alt+o")
        assert app.query_one("#editor-choice", Select).value == "nvim"
