"""Finder file-open integration for the persistent editor preference."""

from __future__ import annotations

import os
import subprocess
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from tdnext.api import PluginContext
from tdnext.core.manager import PluginManager
from tdnext.core.state import StateStore
from tdnext.plugins.files.editor_preferences import EditorPreferences, EditorPreferencesStore
from tdnext.plugins.files.plugin import FilesPane, FilesPlugin
from tdnext.plugins.files.view import FilesView


def make_manager(tmp_path: Path) -> PluginManager:
    state_dir = tmp_path / "state"
    bin_dir = state_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return PluginManager({}, StateStore(state_dir / "plugins.json"), bin_dir)


def make_context(tmp_path: Path, directory: Path, manager: PluginManager | None = None) -> PluginContext:
    return PluginContext(directory, manager if manager is not None else make_manager(tmp_path))


def editor_store_for(context: PluginContext) -> EditorPreferencesStore:
    return EditorPreferencesStore(context.manager.store.path.with_name("editor.json"))


def install_local_executable(manager: PluginManager, name: str) -> Path:
    binary = manager.local_bin / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def fake_app() -> mock.Mock:
    app = mock.Mock()
    app.suspend.return_value = nullcontext()
    app.notify = mock.Mock()
    return app


def run_edit(
    view: FilesView, target: Path, app: mock.Mock, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[list[str]], list[dict]]:
    commands: list[list[str]] = []
    seen_kwargs: list[dict] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("check") is False
        assert "shell" not in kwargs or kwargs["shell"] is False
        commands.append(list(command))
        seen_kwargs.append(dict(kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("tdnext.plugins.files.view.subprocess.run", fake_run)
    with mock.patch.object(FilesView, "app", new_callable=mock.PropertyMock) as app_prop:
        app_prop.return_value = app
        monkeypatch.setattr(view, "reload_directory", lambda *args, **kwargs: None)
        view.edit(target)
    return commands, seen_kwargs


def test_environment_default_preserves_editor_args_and_restored_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    context = make_context(tmp_path, tmp_path, manager)
    view = FilesView(tmp_path, context)
    app = fake_app()
    monkeypatch.setenv("EDITOR", "editor --flag")

    commands, seen = run_edit(view, target, app, monkeypatch)

    assert commands == [["editor", "--flag", str(target)]]
    from tdnext.core.local_env import restored_env

    assert seen[0]["env"] == restored_env()
    assert "TDNEXT_PATH_BOOTSTRAPPED" not in seen[0]["env"]
    app.notify.assert_not_called()


def test_environment_empty_editor_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    context = make_context(tmp_path, tmp_path, make_manager(tmp_path))
    view = FilesView(tmp_path, context)
    app = fake_app()
    monkeypatch.setenv("EDITOR", "")

    commands, _ = run_edit(view, target, app, monkeypatch)

    assert commands == []
    app.notify.assert_called_once()
    assert "Cannot start editor" in str(app.notify.call_args[0][0])
    assert "EDITOR is empty" in str(app.notify.call_args[0][0])


def test_explicit_nvim_prefers_local_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    local_nvim = install_local_executable(manager, "nvim")
    context = make_context(tmp_path, tmp_path, manager)
    editor_store_for(context).write(EditorPreferences(editor="nvim"))
    view = FilesView(tmp_path, context)
    app = fake_app()

    commands, _ = run_edit(view, target, app, monkeypatch)

    assert commands == [[str(local_nvim), str(target)]]
    assert Path(commands[0][0]).is_absolute()


def test_explicit_vi_resolves_via_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path_dir = tmp_path / "pathbin"
    path_dir.mkdir()
    vi_bin = path_dir / "vi"
    vi_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    vi_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    context = make_context(tmp_path, tmp_path, manager)
    editor_store_for(context).write(EditorPreferences(editor="vi"))
    view = FilesView(tmp_path, context)
    app = fake_app()

    commands, seen = run_edit(view, target, app, monkeypatch)

    assert commands == [[str(vi_bin), str(target)]]
    assert seen[0]["env"]["PATH"] == str(path_dir)


def test_missing_explicit_editor_yields_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    context = make_context(tmp_path, tmp_path, manager)
    editor_store_for(context).write(EditorPreferences(editor="nvim"))
    monkeypatch.setenv("PATH", str(tmp_path))
    assert manager.executable("nvim") is None
    view = FilesView(tmp_path, context)
    app = fake_app()

    commands, _ = run_edit(view, target, app, monkeypatch)

    assert commands == []
    app.notify.assert_called_once()
    message = str(app.notify.call_args[0][0])
    assert "Cannot start editor" in message
    assert "nvim" in message


def test_running_finder_reads_current_preference_on_each_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    local_nvim = install_local_executable(manager, "nvim")
    context = make_context(tmp_path, tmp_path, manager)
    store = editor_store_for(context)
    view = FilesView(tmp_path, context)
    app = fake_app()
    monkeypatch.setenv("EDITOR", "editor --flag")

    store.write(EditorPreferences(editor="environment"))
    first, _ = run_edit(view, target, app, monkeypatch)

    store.write(EditorPreferences(editor="nvim"))
    second, _ = run_edit(view, target, app, monkeypatch)

    assert first == [["editor", "--flag", str(target)]]
    assert second == [[str(local_nvim), str(target)]]


def test_file_argument_is_absolute_and_single_arg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "my notes -draft.txt"
    target.write_text("content", encoding="utf-8")
    dash = tmp_path / "-leading.txt"
    dash.write_text("content", encoding="utf-8")
    manager = make_manager(tmp_path)
    local_vi = install_local_executable(manager, "vi")
    assert os.access(local_vi, os.X_OK)
    context = make_context(tmp_path, tmp_path, manager)
    editor_store_for(context).write(EditorPreferences(editor="vi"))
    view = FilesView(tmp_path, context)
    app = fake_app()

    commands, _ = run_edit(view, target, app, monkeypatch)
    commands_dash, _ = run_edit(view, dash, app, monkeypatch)

    assert commands == [[str(local_vi), str(target)]]
    assert commands_dash == [[str(local_vi), str(dash)]]
    assert Path(commands[0][1]).is_absolute()
    assert len(commands[0]) == 2


def test_no_shell_expansion_for_environment_editor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("content", encoding="utf-8")
    context = make_context(tmp_path, tmp_path, make_manager(tmp_path))
    view = FilesView(tmp_path, context)
    app = fake_app()
    monkeypatch.setenv("EDITOR", "editor --flag; touch pwned")

    commands, seen = run_edit(view, target, app, monkeypatch)

    assert commands == [["editor", "--flag;", "touch", "pwned", str(target)]]
    assert "shell" not in seen[0]


def test_directory_selection_warns_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    context = make_context(tmp_path, tmp_path, make_manager(tmp_path))
    view = FilesView(tmp_path, context)
    app = fake_app()

    commands, _ = run_edit(view, folder, app, monkeypatch)

    assert commands == []
    app.notify.assert_called_once_with("Select a regular file to edit.", severity="warning")


def test_context_flows_through_make_view_and_pane(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    context = make_context(tmp_path, tmp_path, manager)
    plugin = FilesPlugin()

    view = plugin.make_view(context)
    pane = FilesPane(context)

    assert isinstance(view, FilesView)
    assert view._plugin_context is context
    assert pane.context is context


@pytest.mark.anyio
async def test_e_action_delegates_to_preference_aware_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``e`` binding funnels through ``FilesView.edit`` (native and fallback share it)."""

    target = tmp_path / "notes.txt"
    target.write_text("existing content", encoding="utf-8")
    context = make_context(tmp_path, tmp_path, make_manager(tmp_path))
    view = FilesView(tmp_path, context)
    captured: list[Path] = []
    monkeypatch.setattr(view, "edit", lambda item: captured.append(item))
    browser = mock.Mock()

    async def fake_selected() -> Path | None:
        return target

    monkeypatch.setattr(browser, "selected_for_action", fake_selected)
    monkeypatch.setattr(view, "query_one", lambda *args, **kwargs: browser)

    await view.action_edit()

    assert captured == [target]
    assert target.read_text(encoding="utf-8") == "existing content"


def test_enter_row_selection_delegates_to_preference_aware_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Enter`` on a regular file funnels through ``FilesView.edit`` (native and fallback share it)."""
    from tdnext.plugins.files.listing import FileEntry

    target = tmp_path / "notes.txt"
    target.write_text("existing content", encoding="utf-8")
    context = make_context(tmp_path, tmp_path, make_manager(tmp_path))
    view = FilesView(tmp_path, context)
    captured: list[Path] = []
    monkeypatch.setattr(view, "edit", lambda item: captured.append(item))
    table = mock.Mock()
    table.entry_for_key.return_value = FileEntry(target, False, "1 B", "now")
    monkeypatch.setattr(view, "query_one", lambda *args, **kwargs: table)
    event = mock.Mock()
    event.stop = mock.Mock()
    event.data_table = table
    event.row_key = mock.Mock()

    view.on_data_table_row_selected(event)

    assert captured == [target]
