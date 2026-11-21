from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, ContentSwitcher, DataTable, Select
from textual.widgets._select import SelectOverlay

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.models import PluginState
from tdnext.plugins.desktop.tool import ToolPane
from tdnext.plugins.settings.dialogs import ConfirmPluginChange
from tdnext.plugins.settings.plugin import SettingsPlugin
from tdnext.plugins.settings.view import SettingsView


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24)])
@pytest.mark.parametrize("field", ["provider", "mode"])
@pytest.mark.parametrize("option", [0, 1])
async def test_mouse_option_selection_restores_focus_and_tab_order(
    tmp_path: Path, size: tuple[int, int], field: str, option: int
) -> None:
    desktop, manager = make_app(tmp_path)
    manager.enable("icons")
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        selector = app.query_one(f"#icon-{field}", Select)
        await pilot.click(f"#icon-{field}-frame", offset=(0, 1))
        assert selector.expanded
        await pilot.click(selector.query_one(SelectOverlay), offset=(1, option))
        assert not selector.expanded
        assert app.focused is selector
        expected = Appearance(provider_id="icons" if option and field == "provider" else None,
                              icon_mode="codicons" if option and field == "mode" else "compatibility")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == expected
        await pilot.press("tab")
        next_id = "#icon-mode" if field == "provider" else "#settings-feedback-scroll"
        assert app.focused is app.query_one(next_id)


@pytest.mark.anyio
async def test_clicking_another_appearance_frame_switches_the_open_selector(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#icon-provider-frame", offset=(0, 1))
        await pilot.click("#icon-mode-frame", offset=(0, 1))
        assert not app.query_one("#icon-provider", Select).expanded
        assert app.query_one("#icon-mode", Select).expanded


@pytest.mark.anyio
async def test_escape_dismisses_the_open_appearance_menu_without_leaving_settings(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        selector = app.query_one("#icon-mode", Select)
        await pilot.click("#icon-mode-frame", offset=(0, 1))
        assert selector.expanded
        await pilot.press("escape")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"
        assert not selector.expanded
        assert app.focused is selector


@pytest.mark.anyio
async def test_disabled_appearance_frame_does_not_open(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    before = AppearanceStore(manager.store.path.with_name("appearance.json")).read()
    async with app.run_test(size=(80, 24)) as pilot:
        selector = app.query_one("#icon-provider", Select)
        selector.disabled = True
        await pilot.click("#icon-provider-frame", offset=(0, 1))
        assert not selector.expanded
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == before


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (35, 24)])
@pytest.mark.parametrize("operation", ["disable", "uninstall"])
@pytest.mark.parametrize("choice", ["cancel", "confirm-plugin-change"])
@pytest.mark.parametrize("position", ["center", "right-edge"])
async def test_confirmation_buttons_fit_and_respond_to_mouse(
    tmp_path: Path, size: tuple[int, int], operation: str, choice: str, position: str
) -> None:
    desktop, manager = make_app(tmp_path, monitor=True)
    if operation == "disable":
        manifests = dict(manager.manifests)
        manifests["extra"] = manifests["files"].model_copy(
            update={"id": "extra", "name": "Extra", "requires_plugins": ("monitor",)}
        )
        manager.manifests = manifests
        state = manager.store.read()
        manager.store.write(PluginState(installed=state.installed | {"extra"}, enabled=state.enabled | {"extra"}))
    before = manager.store.read()
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("d" if operation == "disable" else "u")
        dialog = app.screen
        assert isinstance(dialog, ConfirmPluginChange)
        cancel = dialog.query_one("#cancel", Button)
        confirm = dialog.query_one("#confirm-plugin-change", Button)
        actions = dialog.query_one(Horizontal)
        assert app.focused is cancel
        assert manager.store.read() == before
        for button in (cancel, confirm):
            assert actions.content_region.contains_region(button.region)
            assert dialog.region.contains_region(button.region)
        button = dialog.query_one(f"#{choice}", Button)
        x = button.region.width // 2 if position == "center" else button.region.width - 1
        await pilot.click(button, offset=(x, button.region.height // 2))
        await app.workers.wait_for_complete()
        if choice == "cancel":
            assert manager.store.read() == before
        elif operation == "disable":
            assert not ({"monitor", "extra"} & manager.store.read().enabled)
        else:
            assert "monitor" not in manager.store.read().installed


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24)])
@pytest.mark.parametrize(("keys", "removed"), [(('enter',), False), (('escape',), False), (('tab', 'enter'), True)])
async def test_confirmation_keyboard_is_safe_and_reachable(
    tmp_path: Path, size: tuple[int, int], keys: tuple[str, ...], removed: bool
) -> None:
    desktop, manager = make_app(tmp_path, monitor=True)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("u")
        assert app.focused is app.screen.query_one("#cancel", Button)
        await pilot.press(*keys)
        await app.workers.wait_for_complete()
        assert ("monitor" not in manager.store.read().installed) is removed


@pytest.mark.anyio
async def test_open_confirmation_reflows_on_resize_without_losing_focus(tmp_path: Path) -> None:
    desktop, manager = make_app(tmp_path, monitor=True)
    before = manager.store.read()
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("u")
        cancel = app.screen.query_one("#cancel", Button)
        confirm = app.screen.query_one("#confirm-plugin-change", Button)
        actions = app.screen.query_one(Horizontal)
        for width in (35, 80):
            await pilot.resize_terminal(width, 24)
            await pilot.pause()
            assert app.focused is cancel
            assert actions.content_region.contains_region(cancel.region)
            assert actions.content_region.contains_region(confirm.region)
        await pilot.press("escape")
        assert manager.store.read() == before


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24)])
async def test_long_cascade_keeps_actions_visible_and_details_scrollable(tmp_path: Path, size: tuple[int, int]) -> None:
    desktop, manager = make_app(tmp_path, monitor=True)
    manifests = dict(manager.manifests)
    dependants = {f"dep{index:03}" for index in range(200)}
    for plugin_id in dependants:
        manifests[plugin_id] = manifests["files"].model_copy(
            update={"id": plugin_id, "requires_plugins": ("monitor",)}
        )
    manager.manifests = manifests
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | dependants, enabled=state.enabled | dependants))
    before = manager.store.read()
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("d")
        cancel = app.screen.query_one("#cancel", Button)
        confirm = app.screen.query_one("#confirm-plugin-change", Button)
        assert app.focused is cancel
        assert app.screen.region.contains_region(cancel.region)
        assert app.screen.region.contains_region(confirm.region)
        details = app.screen.query_one("#plugin-confirmation-message", VerticalScroll)
        assert details.max_scroll_y > 0
        await pilot.press("shift+tab", "end")
        assert app.focused is details
        assert details.scroll_y > 0
        await pilot.press("escape")
        assert manager.store.read() == before


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(35, 24), (80, 24)])
async def test_confirmation_button_frames_do_not_change_with_focus_or_hover(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    desktop, _manager = make_app(tmp_path, monitor=True)
    app = ToolPane(SettingsPlugin(), desktop.context)
    async with app.run_test(size=size) as pilot:
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("u")
        cancel = app.screen.query_one("#cancel", Button)
        confirm = app.screen.query_one("#confirm-plugin-change", Button)
        cancel_border, confirm_border = cancel.styles.border, confirm.styles.border
        await pilot.press("tab")
        assert app.focused is confirm
        assert cancel.styles.border == cancel_border
        assert confirm.styles.border == confirm_border
        await pilot.hover(confirm, offset=(0, 1))
        assert confirm.styles.border == confirm_border
