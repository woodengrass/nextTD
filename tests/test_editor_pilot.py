"""Pilot-mounted finder coverage for the persistent editor preference."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from test_editor_launch import editor_store_for, install_local_executable, make_context, make_manager
from textual.app import App, ComposeResult
from textual.pilot import Pilot

from tdnext.plugins.files.editor_preferences import EditorPreferences
from tdnext.plugins.files.plugin import FilesPane, FilesPlugin
from tdnext.plugins.files.view import FilesView


class MountHost(App[None]):
    """Minimal fallback host that mounts a plugin-built view."""

    def __init__(self, view: FilesView) -> None:
        super().__init__()
        self.mounted_view = view

    def compose(self) -> ComposeResult:
        yield self.mounted_view


async def settle_pilot(app: App[None], pilot: Pilot[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.anyio
async def test_native_pane_mounts_preference_aware_finder(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    context = make_context(tmp_path, workdir, make_manager(tmp_path))
    pane = FilesPane(context)
    async with pane.run_test(size=(100, 30)) as pilot:
        await settle_pilot(pane, pilot)
        assert pane.query_one(FilesView)._plugin_context is context


@pytest.mark.anyio
async def test_fallback_view_mounts_with_plugin_context(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    context = make_context(tmp_path, workdir, make_manager(tmp_path))
    view = FilesPlugin().make_view(context)
    assert isinstance(view, FilesView)
    host = MountHost(view)
    async with host.run_test(size=(100, 30)) as pilot:
        await settle_pilot(host, pilot)
        assert host.query_one(FilesView) is view
        assert view._plugin_context is context


@pytest.mark.anyio
async def test_pilot_pressing_e_opens_with_explicit_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "notes.txt"
    target.write_text("existing content", encoding="utf-8")
    manager = make_manager(tmp_path)
    local_nvim = install_local_executable(manager, "nvim")
    context = make_context(tmp_path, workdir, manager)
    editor_store_for(context).write(EditorPreferences(editor="nvim"))
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert "TDNEXT_PATH_BOOTSTRAPPED" not in env
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", fake_run)
    view = FilesPlugin().make_view(context)
    assert isinstance(view, FilesView)
    app = MountHost(view)
    assert isinstance(app.mounted_view, FilesView)
    monkeypatch.setattr(app, "suspend", nullcontext)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_pilot(app, pilot)
        await pilot.press("e")
        await settle_pilot(app, pilot)

    assert commands == [[str(local_nvim), str(target)]]
    assert target.read_text(encoding="utf-8") == "existing content"


@pytest.mark.anyio
async def test_pilot_pressing_enter_opens_with_environment_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "notes.txt"
    target.write_text("existing content", encoding="utf-8")
    context = make_context(tmp_path, workdir, make_manager(tmp_path))
    editor_store_for(context).write(EditorPreferences(editor="environment"))
    monkeypatch.setenv("EDITOR", "editor --flag")
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert "TDNEXT_PATH_BOOTSTRAPPED" not in env
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", fake_run)
    view = FilesPlugin().make_view(context)
    assert isinstance(view, FilesView)
    app = MountHost(view)
    monkeypatch.setattr(app, "suspend", nullcontext)
    async with app.run_test(size=(100, 30)) as pilot:
        await settle_pilot(app, pilot)
        await pilot.press("enter")
        await settle_pilot(app, pilot)

    assert commands == [["editor", "--flag", str(target)]]
    assert target.read_text(encoding="utf-8") == "existing content"
