from pathlib import Path

import pytest
from test_settings_ui import make_app
from textual.widgets import Select

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.plugins.desktop.app import DesktopApp


@pytest.mark.anyio
async def test_reopening_settings_displays_preferences_changed_by_another_session(tmp_path: Path) -> None:
    first, manager = make_app(tmp_path)
    manager.enable("icons")
    second = DesktopApp(first.context, first.views)
    async with first.run_test(size=(80, 24)) as first_pilot:
        await first_pilot.press("alt+o", "escape")
        async with second.run_test(size=(80, 24)) as second_pilot:
            await second_pilot.press("alt+o")
            await second_pilot.click("#icon-provider")
            await second_pilot.press("down", "enter")
            assert AppearanceStore(manager.store.path.with_name("appearance.json")).read().provider_id == "icons"
        await first_pilot.press("alt+o")
        assert first.query_one("#icon-provider", Select).value == "icons"


@pytest.mark.anyio
async def test_editing_style_preserves_provider_changed_by_another_open_session(tmp_path: Path) -> None:
    first, manager = make_app(tmp_path)
    manager.enable("icons")
    second = DesktopApp(first.context, first.views)
    async with first.run_test(size=(80, 24)) as first_pilot:
        await first_pilot.press("alt+o")
        await first_pilot.click("#icon-mode")
        async with second.run_test(size=(80, 24)) as second_pilot:
            await second_pilot.press("alt+o")
            await second_pilot.click("#icon-provider")
            await second_pilot.press("down", "enter")
        await first_pilot.press("down", "enter")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == Appearance(
            provider_id="icons", icon_mode="codicons"
        )


@pytest.mark.anyio
async def test_editing_provider_preserves_style_changed_by_another_open_session(tmp_path: Path) -> None:
    first, manager = make_app(tmp_path)
    manager.enable("icons")
    second = DesktopApp(first.context, first.views)
    async with first.run_test(size=(80, 24)) as first_pilot:
        await first_pilot.press("alt+o")
        await first_pilot.click("#icon-provider")
        async with second.run_test(size=(80, 24)) as second_pilot:
            await second_pilot.press("alt+o")
            await second_pilot.click("#icon-mode")
            await second_pilot.press("down", "enter")
        await first_pilot.press("down", "enter")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == Appearance(
            provider_id="icons", icon_mode="codicons"
        )


@pytest.mark.anyio
async def test_focusing_selector_refreshes_the_other_preference(tmp_path: Path) -> None:
    first, manager = make_app(tmp_path)
    manager.enable("icons")
    second = DesktopApp(first.context, first.views)
    async with first.run_test(size=(80, 24)) as first_pilot:
        await first_pilot.press("alt+o")
        async with second.run_test(size=(80, 24)) as second_pilot:
            await second_pilot.press("alt+o")
            await second_pilot.click("#icon-provider")
            await second_pilot.press("down", "enter")
        await first_pilot.click("#icon-mode")
        assert first.query_one("#icon-provider", Select).value == "icons"
