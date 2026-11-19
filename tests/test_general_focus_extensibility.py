from pathlib import Path

import pytest
from test_settings_ui import make_app, text_of
from textual.containers import Vertical, VerticalScroll
from textual.events import AppFocus
from textual.widgets import Button, Select, Static

from tdnext.api import SettingsControls
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.files.editor_controls import EditorControls
from tdnext.plugins.files.editor_preferences import EditorPreferences, EditorPreferencesStore
from tdnext.plugins.settings.plugin import SettingsPlugin


class BrokenRefreshControls(SettingsControls):
    def refresh_settings(self) -> None:
        raise RuntimeError("refresh failed")

    def refresh_if_idle(self) -> None:
        raise RuntimeError("idle refresh failed")


@pytest.mark.anyio
@pytest.mark.parametrize("trigger", ["activation", "focus"])
async def test_broken_refresh_reports_control_and_other_controls_refresh(tmp_path: Path, trigger: str) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    store = EditorPreferencesStore(manager.store.path.with_name("editor.json"))
    async with app.run_test(size=(80, 24)) as pilot:
        general = app.query_one("#settings-general", VerticalScroll)
        await general.mount(BrokenRefreshControls(), before=app.query_one(EditorControls))
        store.write(EditorPreferences(editor="vi"))
        if trigger == "activation":
            app.post_message(AppFocus())
            await pilot.pause()
        else:
            await pilot.press("tab")
        assert app.query_one("#editor-choice", Select).value == "vi"
        feedback = text_of(app.query_one("#plugin-feedback", Static))
        assert "BrokenRefreshControls" in feedback
        assert ("idle refresh failed" if trigger == "focus" else "Cannot refresh ") in feedback
        assert "refresh failed" in feedback


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("host", "size"), [("pane", (35, 24)), ("pane", (120, 36)), ("desktop", (80, 24))]
)
async def test_added_general_controls_are_reached_once_in_each_direction(
    tmp_path: Path, host: str, size: tuple[int, int]
) -> None:
    desktop, manager = make_app(tmp_path)
    app = desktop if host == "desktop" else ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        if host == "desktop":
            await pilot.press("alt+o")
        general = app.query_one("#settings-general", VerticalScroll)
        first = Button("First additional setting", id="extra-first")
        second = Button("Second additional setting", id="extra-second")
        group = Vertical(first, second)
        group.styles.height = "auto"
        await general.mount(group)
        await pilot.pause()

        mode = app.query_one("#icon-mode", Select)
        feedback = app.query_one("#settings-feedback-scroll", VerticalScroll)
        editor = app.query_one("#editor-choice", Select)
        chain = app.screen.focus_chain
        external = chain[(chain.index(feedback) + 1) % len(chain)]
        for expected in (mode, feedback, editor, first, second, external):
            await pilot.press("tab")
            assert app.focused is expected
            if expected in (first, second):
                assert general.content_region.contains_region(expected.region)
        for expected in (second, first, editor, feedback, mode):
            await pilot.press("shift+tab")
            assert app.focused is expected
        if size == (35, 24):
            assert general.max_scroll_y > 0
        assert not manager.store.path.with_name("appearance.json").exists()
        assert not manager.store.path.with_name("editor.json").exists()


@pytest.mark.anyio
async def test_general_traversal_skips_disabled_and_hidden_added_controls(tmp_path: Path) -> None:
    desktop, _manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(35, 24)) as pilot:
        general = app.query_one("#settings-general", VerticalScroll)
        disabled = Button("Disabled", disabled=True)
        hidden = Button("Hidden")
        hidden.display = False
        available = Button("Available")
        group = Vertical(disabled, hidden, available)
        group.styles.height = "auto"
        await general.mount(group)
        await pilot.press("tab", "tab", "tab", "tab")
        assert app.focused is available
        await pilot.press("shift+tab")
        assert app.focused is app.query_one("#editor-choice", Select)


@pytest.mark.anyio
async def test_open_selector_keeps_its_escape_and_tab_behavior(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path)
    app = ToolPane("settings", SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        general = app.query_one("#settings-general", VerticalScroll)
        extra = Button("Additional setting")
        await general.mount(extra)
        editor = app.query_one("#editor-choice", Select)
        await pilot.press("tab", "tab", "tab", "enter")
        assert editor.expanded
        await pilot.press("escape")
        assert app.focused is editor
        assert not editor.expanded
        await pilot.press("enter", "tab")
        assert not editor.expanded
        assert app.focused is extra
        assert not manager.store.path.with_name("editor.json").exists()
