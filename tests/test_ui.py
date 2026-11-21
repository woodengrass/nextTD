from pathlib import Path

import pytest
from textual.widgets import DataTable

from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.manager import PluginManager
from tdnext.core.state import StateStore
from tdnext.plugins.files.plugin import FilesPane


def desktop(tmp_path: Path) -> FilesPane:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    manager = PluginManager(manifests, StateStore(tmp_path / "state.json"), tmp_path / "bin")
    return FilesPane(PluginContext(tmp_path, manager))


@pytest.mark.anyio
async def test_folder_creation_through_keyboard(tmp_path: Path) -> None:
    app = desktop(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("n")
        await pilot.press("n", "e", "w", "enter")
        await pilot.pause()
        assert (tmp_path / "new").is_dir()


@pytest.mark.anyio
async def test_cancel_delete_preserves_selected_file(tmp_path: Path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("keep", encoding="utf-8")
    app = desktop(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DataTable).focus()
        await pilot.press("d", "escape")
        assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.anyio
async def test_confirm_delete_removes_selected_file(tmp_path: Path) -> None:
    target = tmp_path / "remove.txt"
    target.touch()
    app = desktop(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        app.query_one(DataTable).focus()
        await pilot.press("d")
        await pilot.click("#delete")
        await pilot.pause()
        assert not target.exists()
