import threading
from pathlib import Path

import pytest
from test_settings_ui import make_app, text_of
from textual.containers import VerticalScroll
from textual.widgets import Button, ContentSwitcher, DataTable, Static

from tdnext.core.models import PluginState
from tdnext.plugins.settings.view import SettingsView


@pytest.mark.anyio
async def test_plugin_shortcuts_do_nothing_in_general(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.press("i", "e", "d", "u")
        await app.workers.wait_for_complete()
        assert manager.store.read() == before
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-general"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "initial", "expected"),
    [("i", "available", "installed"), ("e", "available", "enabled"),
     ("d", "enabled", "installed"), ("u", "installed", "available")],
)
async def test_plugin_shortcut_operates_on_selected_row(
    tmp_path: Path, key: str, initial: str, expected: str
) -> None:
    app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(
        installed=state.installed | ({"monitor"} if initial != "available" else set()),
        enabled=state.enabled | ({"monitor"} if initial == "enabled" else set()),
    ))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press(key)
        if key == "u":
            await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert ("monitor" in manager.store.read().installed) == (expected != "available")
        assert ("monitor" in manager.store.read().enabled) == (expected == "enabled")
        assert "Restart desktop" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_uninstall_confirmation_escape_stays_in_plugins(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"monitor"}, enabled=state.enabled))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("u", "escape")
        assert app.query_one("#settings-pages", ContentSwitcher).current == "settings-plugins"
        assert "monitor" in manager.store.read().installed


@pytest.mark.anyio
async def test_plugin_error_is_retained_across_section_changes(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": ("tdnext-unavailable-for-test",)}
    )
    manager.manifests = manifests
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("e")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.click("#section-general")
        await pilot.click("#section-plugins")
        assert manager.store.read() == before
        assert "Operation failed" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_long_plugin_error_is_keyboard_scrollable_at_80_by_24(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manifests = dict(manager.manifests)
    manifests["monitor"] = manifests["monitor"].model_copy(
        update={"requires_executables": ("tdnext-" + "unavailable-" * 20,)}
    )
    manager.manifests = manifests
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("e")
        await app.workers.wait_for_complete()
        await pilot.pause()
        feedback = view.query_one("#settings-feedback-scroll", VerticalScroll)
        assert feedback.region.height <= 4
        assert feedback.max_scroll_y > 0
        await pilot.press("tab", "tab", "tab")
        assert app.focused is feedback
        await pilot.press("end")
        assert feedback.scroll_y > 0


@pytest.mark.anyio
async def test_disable_confirms_dependent_cascade_in_plugins(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path, monitor=True)
    manifests = dict(manager.manifests)
    manifests["extra"] = manifests["files"].model_copy(
        update={"id": "extra", "name": "Extra", "requires_plugins": ("monitor",)}
    )
    manager.manifests = manifests
    state = manager.store.read()
    manager.store.write(PluginState(installed=state.installed | {"extra"}, enabled=state.enabled | {"extra"}))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
        await pilot.pause()
        await pilot.press("d")
        assert any("extra" in text_of(item) for item in app.screen.query(Static))
        await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not ({"monitor", "extra"} & manager.store.read().enabled)
        assert "extra" in text_of(view.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["desktop", "settings"])
async def test_running_components_remain_protected_in_plugins(tmp_path: Path, target: str) -> None:
    app, manager = make_app(tmp_path)
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index(target))
        await pilot.pause()
        assert view.query_one("#plugin-disable", Button).disabled
        assert view.query_one("#plugin-uninstall", Button).disabled
        await pilot.press("d", "u")
        assert manager.store.read() == before


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["disable", "uninstall"])
async def test_missing_installed_plugin_can_be_removed_from_plugins(tmp_path: Path, operation: str) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    manager.manifests = {key: value for key, value in manager.manifests.items() if key != "icons"}
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o")
        await pilot.click("#section-plugins")
        view = app.query_one(SettingsView)
        view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("icons"))
        await pilot.pause()
        await pilot.click(f"#plugin-{operation}")
        if operation == "uninstall":
            await pilot.click("#confirm-plugin-change")
        await app.workers.wait_for_complete()
        assert "icons" not in manager.store.read().enabled
        assert ("icons" in manager.store.read().installed) == (operation == "disable")


@pytest.mark.anyio
async def test_busy_operation_survives_section_switch_and_blocks_more_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, manager = make_app(tmp_path)
    started, release = threading.Event(), threading.Event()
    original = manager.enable

    def slow_enable(plugin_id: str, *, approved: tuple[tuple[str, str], ...] = ()) -> tuple[str, ...]:
        started.set()
        assert release.wait(timeout=10), "test operation timed out"
        return original(plugin_id, approved=approved)

    monkeypatch.setattr(manager, "enable", slow_enable)
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.press("alt+o")
            await pilot.click("#section-plugins")
            view = app.query_one(SettingsView)
            view.query_one(DataTable).move_cursor(row=view.plugin_ids.index("monitor"))
            await pilot.pause()
            await pilot.press("e")
            assert started.wait(timeout=1)
            await pilot.click("#section-general")
            await pilot.click("#icon-mode")
            await pilot.press("down", "enter")
            await pilot.click("#section-plugins")
            assert all(button.disabled for button in view.query("#plugin-actions Button"))
            await pilot.press("i", "e", "d", "u")
            assert "monitor" not in manager.store.read().installed
            assert "Enabling monitor" in text_of(view.query_one("#plugin-feedback", Static))
            await pilot.press("escape")
            assert app.query_one(ContentSwitcher).current == "view-files"
            await pilot.press("alt+o")
            await pilot.click("#section-plugins")
            assert all(button.disabled for button in view.query("#plugin-actions Button"))
        finally:
            release.set()
        await app.workers.wait_for_complete()
        assert "monitor" in manager.store.read().enabled


@pytest.mark.anyio
async def test_back_and_f6_preserve_desktop_navigation(tmp_path: Path) -> None:
    app, _manager = make_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("alt+o", "f6")
        assert app.focused is app.query_one("#destinations")
        await pilot.press("alt+o", "escape")
        assert app.query_one(ContentSwitcher).current == "view-files"
